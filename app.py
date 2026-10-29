# -*- coding: utf-8 -*-
"""
Dado Delivery - Multi-product E-commerce Platform
www.dadodelivery.com
Languages: Amharic & English
"""

import os
import re
import json
import math
import time
import hmac
import shutil
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlencode
import urllib.request
import base64
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, send_from_directory, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_babel import Babel, gettext as _, lazy_gettext as _l
from sqlalchemy import or_, inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import Markup
try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 50_000_000  # reject decompression bombs
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------
# Orders with a total in [PREPAY_MIN, PREPAY_MAX] ETB must be pre-paid by bank
# deposit. Orders above PREPAY_MAX are paid on delivery (cash on delivery) and
# the transporter must send a receipt to the admin.
PREPAY_MIN = 100
PREPAY_MAX = 100000
# Legacy default sizes (kg). Prefer UNIT_PRESETS by product.unit.
WEIGHTS = [10, 15, 25, 50, 100]

# Size options per unit type. Admin picks a unit for each product; the form
# then shows these sizes. Oil → L, grains → kg, packaged goods → pcs/pack.
UNIT_PRESETS = {
    'kg':   [10, 15, 25, 50, 100],
    'L':    [1, 3, 5, 10, 20],
    'pcs':  [1, 6, 12, 24],
    'pack': [1, 5, 10, 20],
    'bag':  [1, 5, 10, 25],
}
UNIT_LABELS = {
    'kg':   {'en': 'kg',   'am': 'ኪ.ግ'},
    'L':    {'en': 'L',    'am': 'ሊትር'},
    'pcs':  {'en': 'pcs',  'am': 'ብዛት'},
    'pack': {'en': 'pack', 'am': 'ጥቅል'},
    'bag':  {'en': 'bag',  'am': 'ከረጢት'},
}
ALLOWED_UNITS = list(UNIT_PRESETS.keys())


def unit_label(unit, lang='en'):
    """Human-readable unit short label."""
    u = (unit or 'kg').strip()
    labels = UNIT_LABELS.get(u) or UNIT_LABELS['kg']
    return labels.get(lang) or labels['en']


def sizes_for_unit(unit):
    """List of size values for a product unit."""
    return list(UNIT_PRESETS.get((unit or 'kg').strip(), WEIGHTS))


def product_vat_label(vat_percent, lang='en'):
    """Badge for a product price: exempt or inclusive with rate %."""
    try:
        pct = float(vat_percent or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if pct <= 0:
        return 'ከቫት ነፃ / VAT Exempt' if lang == 'am' else 'VAT Exempt'
    if lang == 'am':
        return f'ቫት ያካተተ ({pct:g}%) / VAT Inclusive'
    return f'VAT Inclusive ({pct:g}%)'


def vat_label(mode_or_pct, lang='en'):
    """Compat: accepts percent number or legacy mode string."""
    if isinstance(mode_or_pct, (int, float)):
        return product_vat_label(mode_or_pct, lang)
    m = (str(mode_or_pct or '')).strip().lower()
    if m in ('exempt', 'vat_exempt', 'free', '0', ''):
        return product_vat_label(0, lang)
    try:
        return product_vat_label(float(m), lang)
    except (TypeError, ValueError):
        return product_vat_label(15, lang)


def normalize_vat_mode(raw):
    m = (raw or 'inclusive').strip().lower()
    if m in ('exempt', 'vat_exempt', 'free'):
        return 'exempt'
    return 'inclusive'


# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
os.makedirs(app.instance_path, exist_ok=True)


def _load_secret_key():
    """SECRET_KEY from env, else a key persisted in instance/.secret_key.

    Persisting it guarantees every gunicorn worker (and every restart) uses the
    SAME key, so sessions, logins and CSRF tokens stay valid.
    """
    env_key = os.environ.get('SECRET_KEY', '').strip()
    if env_key:
        return env_key
    path = os.path.join(app.instance_path, '.secret_key')

    def _read():
        try:
            with open(path, 'r') as fh:
                return fh.read().strip()
        except OSError:
            return ''

    key = _read()
    if key:
        return key
    key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(key)
        return key
    except FileExistsError:
        # Another worker created it first - wait until it has been written.
        for _i in range(40):
            existing = _read()
            if existing:
                return existing
            time.sleep(0.05)
    except OSError:
        pass
    print('WARNING: could not persist a secret key. Set the SECRET_KEY environment variable.')
    return key


_db_url = os.environ.get('DATABASE_URL', '').strip()
if _db_url.startswith('postgres://'):
    _db_url = 'postgresql://' + _db_url[len('postgres://'):]

app.config['SECRET_KEY'] = _load_secret_key()
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url or ('sqlite:///' + os.path.join(app.root_path, 'dado.db'))
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'timeout': 30}}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
# Receipts are financial documents: kept OUTSIDE /static and served only to authorised users.
PRIVATE_UPLOAD_FOLDER = os.path.join(app.instance_path, 'private_uploads')
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 MB
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)
app.config['BABEL_DEFAULT_LOCALE'] = 'am'
app.config['BABEL_SUPPORTED_LOCALES'] = ['am', 'en']
app.config['BABEL_TRANSLATION_DIRECTORIES'] = os.path.join(app.root_path, 'translations')
app.config['LANGUAGES'] = {
    'am': 'አማርኛ',
    'en': 'English'
}

if os.environ.get('TRUST_PROXY', '').lower() in ('1', 'true', 'yes'):
    # Running behind nginx / a load balancer: use the real client IP and scheme.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
else:
    # Without TRUST_PROXY every visitor shares the proxy IP → rate limits hit everyone.
    import sys
    print('WARNING: TRUST_PROXY is not set. Behind nginx/Render all clients share one IP '
          'and rate-limits will block everyone. Set TRUST_PROXY=1 in production.', file=sys.stderr)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(PRIVATE_UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'
login_manager.login_message = _l('Please log in to access the admin panel.')

babel = Babel()


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC for SQLite compat


# Ethiopia is UTC+3 year-round (no DST)
ADDIS_OFFSET = timedelta(hours=3)


def to_addis(dt):
    """Convert a naive UTC datetime to Africa/Addis_Ababa (UTC+3)."""
    if dt is None:
        return None
    return dt + ADDIS_OFFSET


@app.template_filter('addis')
def addis_filter(dt, fmt='%Y-%m-%d %H:%M'):
    """Jinja filter: {{ order.created_at|addis }} or {{ order.created_at|addis('%H:%M') }}"""
    local = to_addis(dt)
    if local is None:
        return ''
    return local.strftime(fmt)


def safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def safe_float(val, default=0.0):
    try:
        if val is None or val == '':
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def form_int(name, default=0, lo=None, hi=None):
    """Read an integer form field safely (never raises, always clamped)."""
    v = safe_int((request.form.get(name) or '').strip(), default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def form_float(name, default=0.0, lo=0.0, hi=1e9):
    v = safe_float((request.form.get(name) or '').strip(), default)
    if not math.isfinite(v):
        v = default
    return min(hi, max(lo, v))


def form_float_or_none(name, lo=0.0, hi=1e9):
    raw = (request.form.get(name) or '').strip()
    if not raw:
        return None
    v = safe_float(raw, None)
    if v is None or not math.isfinite(v):
        return None
    return min(hi, max(lo, v))


def parse_coord(val, limit):
    try:
        if val in (None, ''):
            return None
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or abs(f) > limit:
        return None
    return f


PHONE_RE = re.compile(r'^\+?[0-9][0-9 \-()]{5,19}$')


def normalize_phone(phone):
    """Normalize Ethiopian phone numbers to E.164 (+2519xxxxxxxx).

    Accepts common local formats:
      0911 234 567, 0911234567, 911234567, +251911234567, 251911234567
    Returns '+2519xxxxxxxx' or None if invalid.
    """
    if not phone:
        return None
    # Keep only digits and leading +
    cleaned = re.sub(r'[^\d+]', '', phone.strip())
    if cleaned.startswith('+'):
        digits = cleaned[1:]
    else:
        digits = cleaned

    # Ethiopian mobile: 9 digits starting with 9, or with country code 251
    if digits.startswith('251') and len(digits) == 12 and digits[3] == '9':
        return '+' + digits
    if digits.startswith('0') and len(digits) == 10 and digits[1] == '9':
        return '+251' + digits[1:]
    if len(digits) == 9 and digits[0] == '9':
        return '+251' + digits
    # Already in good shape
    if digits.startswith('2519') and len(digits) == 12:
        return '+' + digits
    return None


def valid_phone(phone):
    """Accept any reasonable phone string, but prefer normalized Ethiopian numbers."""
    if not phone:
        return False
    if normalize_phone(phone):
        return True
    # Fallback for non-Ethiopian numbers (rare)
    return bool(PHONE_RE.match(phone.strip()))


# ---------------------------------------------------------------------------
# SMS / OTP
# ---------------------------------------------------------------------------
# Configure one of:
#   SMS_PROVIDER=twilio          TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM
#   SMS_PROVIDER=africastalking  AT_USERNAME, AT_API_KEY, AT_FROM (sender ID)
#   SMS_PROVIDER=http            SMS_HTTP_URL, SMS_HTTP_TOKEN (optional Bearer)
#   SMS_PROVIDER=console         logs the code (dev / free tier testing only)
# OTP_ENABLED=0 disables the whole flow (register works without SMS).
OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_RESEND_SECONDS = 60
OTP_MAX_PER_PHONE_HOUR = 5


def sms_provider_name():
    return (os.environ.get('SMS_PROVIDER') or '').strip().lower()


def otp_enabled():
    flag = (os.environ.get('OTP_ENABLED') or '').strip().lower()
    if flag in ('0', 'false', 'no', 'off'):
        return False
    if flag in ('1', 'true', 'yes', 'on'):
        return True
    # Auto-enable when a real provider is configured
    return sms_provider_name() in ('twilio', 'africastalking', 'http', 'console')


def send_sms(phone, message):
    """Send SMS. Returns (ok: bool, error: str|None)."""
    provider = sms_provider_name() or 'console'
    phone = phone if phone.startswith('+') else (normalize_phone(phone) or phone)

    if provider == 'console':
        app.logger.warning('SMS [console] to %s: %s', phone, message)
        print(f'[SMS console] to {phone}: {message}', flush=True)
        return True, None

    if provider == 'twilio':
        sid = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
        token = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
        from_num = os.environ.get('TWILIO_FROM', '').strip()
        if not (sid and token and from_num):
            return False, 'Twilio is not configured (TWILIO_ACCOUNT_SID / AUTH_TOKEN / FROM)'
        try:
            data = urlencode({
                'To': phone,
                'From': from_num,
                'Body': message,
            }).encode()
            req = urllib.request.Request(
                f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
                data=data, method='POST',
            )
            creds = base64.b64encode(f'{sid}:{token}'.encode()).decode()
            req.add_header('Authorization', f'Basic {creds}')
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status not in (200, 201):
                    return False, f'Twilio HTTP {resp.status}'
            return True, None
        except Exception as e:
            app.logger.exception('Twilio SMS failed')
            return False, str(e)[:200]

    if provider == 'africastalking':
        username = os.environ.get('AT_USERNAME', '').strip()
        api_key = os.environ.get('AT_API_KEY', '').strip()
        sender = os.environ.get('AT_FROM', '').strip()  # optional shortcode / sender ID
        if not (username and api_key):
            return False, 'Africa\'s Talking is not configured (AT_USERNAME / AT_API_KEY)'
        try:
            form = {
                'username': username,
                'to': phone,
                'message': message,
            }
            if sender:
                form['from'] = sender
            data = urlencode(form).encode()
            req = urllib.request.Request(
                'https://api.africastalking.com/version1/messaging',
                data=data, method='POST',
            )
            req.add_header('apiKey', api_key)
            req.add_header('Accept', 'application/json')
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode('utf-8', errors='replace')
                if resp.status not in (200, 201):
                    return False, f'AT HTTP {resp.status}: {body[:120]}'
            return True, None
        except Exception as e:
            app.logger.exception('Africa\'s Talking SMS failed')
            return False, str(e)[:200]

    if provider == 'http':
        url = os.environ.get('SMS_HTTP_URL', '').strip()
        token = os.environ.get('SMS_HTTP_TOKEN', '').strip()
        if not url:
            return False, 'SMS_HTTP_URL is not set'
        try:
            payload = json.dumps({'to': phone, 'message': message, 'phone': phone, 'text': message}).encode()
            req = urllib.request.Request(url, data=payload, method='POST')
            req.add_header('Content-Type', 'application/json')
            if token:
                req.add_header('Authorization', f'Bearer {token}')
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status not in (200, 201, 202):
                    return False, f'SMS HTTP {resp.status}'
            return True, None
        except Exception as e:
            app.logger.exception('HTTP SMS failed')
            return False, str(e)[:200]

    return False, f'Unknown SMS_PROVIDER={provider}'


def _otp_code():
    # 6-digit numeric, no leading-zero ambiguity for users
    return f'{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}'


def create_and_send_otp(phone, purpose='register'):
    """Create a PhoneOtp row and send the code. Returns (ok, error_message)."""
    phone = normalize_phone(phone) or phone
    # Rate limit: max N codes per phone per hour
    since = utcnow() - timedelta(hours=1)
    recent = PhoneOtp.query.filter(
        PhoneOtp.phone == phone,
        PhoneOtp.purpose == purpose,
        PhoneOtp.created_at >= since,
    ).count()
    if recent >= OTP_MAX_PER_PHONE_HOUR:
        return False, _('Too many codes sent to this number. Please try again in an hour.')

    # Limit SMS cost: max 20 OTP sends per IP per hour across all numbers
    if rate_exceeded('otp_ip', 20, 3600):
        return False, _('Too many verification requests. Please try again later.')

    # Resend cooldown: last code must be older than OTP_RESEND_SECONDS
    last = (PhoneOtp.query.filter_by(phone=phone, purpose=purpose)
            .order_by(PhoneOtp.created_at.desc()).first())
    if last and last.created_at and (utcnow() - last.created_at).total_seconds() < OTP_RESEND_SECONDS:
        wait = int(OTP_RESEND_SECONDS - (utcnow() - last.created_at).total_seconds())
        return False, _('Please wait %(n)s seconds before requesting a new code.', n=max(1, wait))

    code = _otp_code()
    row = PhoneOtp(
        phone=phone,
        purpose=purpose,
        code_hash=generate_password_hash(code),
        expires_at=utcnow() + timedelta(minutes=OTP_TTL_MINUTES),
    )
    db.session.add(row)
    db.session.commit()

    site = get_settings().site_name or 'Dado Delivery'
    if purpose == 'reset':
        msg = f'{site}: Your password reset code is {code}. Valid for {OTP_TTL_MINUTES} minutes.'
    else:
        msg = f'{site}: Your verification code is {code}. Valid for {OTP_TTL_MINUTES} minutes.'

    ok, err = send_sms(phone, msg)
    if not ok:
        # Keep the row so a later resend with working SMS still works; surface error to user
        app.logger.error('Failed to send OTP to %s: %s', phone, err)
        return False, _('Could not send SMS. Please try again later or contact support.')
    return True, None


def verify_otp(phone, code, purpose='register'):
    """Validate code. Returns (ok, error_message). Consumes the code on success."""
    phone = normalize_phone(phone) or phone
    code = (code or '').strip()
    if not code or not code.isdigit() or len(code) != OTP_LENGTH:
        return False, _('Please enter the %(n)s-digit code we sent you.', n=OTP_LENGTH)

    row = (PhoneOtp.query.filter_by(phone=phone, purpose=purpose, consumed_at=None)
           .order_by(PhoneOtp.created_at.desc()).first())
    if not row:
        return False, _('Invalid or expired code. Please try again or request a new one.')
    if row.expires_at and row.expires_at < utcnow():
        return False, _('This code has expired. Please request a new one.')
    if (row.attempts or 0) >= (row.max_attempts or 5):
        return False, _('Too many wrong attempts. Please request a new code.')

    if not check_password_hash(row.code_hash, code):
        row.attempts = (row.attempts or 0) + 1
        db.session.commit()
        left = (row.max_attempts or 5) - row.attempts
        return False, _('Invalid or expired code. Please try again or request a new one.')

    row.consumed_at = utcnow()
    db.session.commit()
    return True, None


def valid_email(email):
    if not email or len(email) > 120:
        return False
    try:
        from email_validator import validate_email, EmailNotValidError
        validate_email(email, check_deliverability=False)
        return True
    except ImportError:
        return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email))
    except EmailNotValidError:
        return False


HEX_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
FONT_RE = re.compile(r"^[A-Za-z0-9 ,'\"\-]{1,80}$")


def clean_color(val, default):
    val = (val or '').strip()
    return val if HEX_COLOR_RE.match(val) else default


def clean_font(val, default):
    val = (val or '').strip()
    return val if FONT_RE.match(val) else default


@app.template_filter('css_font')
def css_font_filter(val):
    """Safe font-family for inline CSS (bypass HTML-escape of quotes)."""
    return Markup(clean_font(val, 'sans-serif'))


# ---------------------------------------------------------------------------
# Safe redirects
# ---------------------------------------------------------------------------
def is_safe_path(target):
    """True only for same-site relative paths (blocks //host, /\\host, schemes)."""
    if not target or not isinstance(target, str):
        return False
    if not target.startswith('/') or target.startswith('//') or '\\' in target:
        return False
    if any(ord(c) < 32 for c in target):
        return False
    p = urlparse(target)
    return not p.scheme and not p.netloc


def safe_referrer():
    ref = request.referrer
    if not ref:
        return None
    p = urlparse(ref)
    if p.netloc and p.netloc != request.host:
        return None
    path = p.path or '/'
    if p.query:
        path += '?' + p.query
    return path if is_safe_path(path) else None


# ---------------------------------------------------------------------------
# CSRF protection (enforced for every state-changing request)
# ---------------------------------------------------------------------------
def get_csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


@app.context_processor
def inject_csrf():
    return {'csrf_token': get_csrf_token}


@app.before_request
def csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token') or ''
    expected = session.get('_csrf', '')
    if expected and token and hmac.compare_digest(token.encode('utf-8'), expected.encode('utf-8')):
        return None
    if request.path.startswith('/api/') or request.path == '/checkout' or request.is_json:
        return jsonify({'success': False,
                        'error': _('Your session expired. Please reload the page and try again.')}), 400
    flash(_('Invalid security token. Please try again.'), 'error')
    fallback = url_for('admin_login') if request.path.startswith('/admin') else url_for('index')
    return redirect(safe_referrer() or fallback)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    # strict-origin-when-cross-origin keeps Referer for same-origin and sends origin
    # only on HTTPS→HTTPS cross-origin (needed by some tile servers like OSM).
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # HSTS only on HTTPS responses
    if request.is_secure or request.headers.get('X-Forwarded-Proto', '').lower() == 'https':
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    # Baseline CSP (allows Leaflet CDN + map tile providers + inline styles)
    resp.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "img-src 'self' data: blob: "
        "https://*.arcgisonline.com https://server.arcgisonline.com "
        "https://*.tile.openstreetmap.org https://*.openstreetmap.org "
        "https://api.maptiler.com https://*.maptiler.com "
        "https://*.mapbox.com https://api.mapbox.com "
        "https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "connect-src 'self' "
        "https://api.maptiler.com https://*.maptiler.com "
        "https://*.arcgisonline.com https://server.arcgisonline.com "
        "https://*.tile.openstreetmap.org "
        "https://*.mapbox.com https://api.mapbox.com; "
        "frame-ancestors 'self'"
    )
    return resp


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Admin(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class SiteSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    site_name = db.Column(db.String(120), default='Dado Delivery')
    logo_path = db.Column(db.String(255), default='')
    primary_color = db.Column(db.String(20), default='#2E7D32')
    secondary_color = db.Column(db.String(20), default='#FF6F00')
    font_family = db.Column(db.String(80), default='Noto Sans Ethiopic, sans-serif')
    delivery_min_hours = db.Column(db.Integer, default=2)  # global default fallback
    delivery_max_hours = db.Column(db.Integer, default=6)
    help_phone = db.Column(db.String(30), default='+251 900 000 000')
    help_email = db.Column(db.String(120), default='help@dadodelivery.com')
    help_telegram = db.Column(db.String(80), default='+251900000000')  # phone or @username
    footer_text_am = db.Column(db.Text, default='በDado Delivery ይደሰቱ')
    footer_text_en = db.Column(db.Text, default='Enjoy with Dado Delivery')
    # When True, chat AI replies if admin has not answered recently. Default off.
    ai_chat_enabled = db.Column(db.Boolean, default=False)
    # Flat delivery / transport fee (ETB). Managed under Admin → VAT & Delivery.
    delivery_fee = db.Column(db.Float, default=0.0)
    # Optional default VAT % suggested when creating new products (0 = exempt)
    default_vat_percent = db.Column(db.Float, default=15.0)
    # Auth page appearance
    auth_logo_path = db.Column(db.String(255), default='')
    auth_primary_color = db.Column(db.String(20), default='#2E7D32')
    auth_font_family = db.Column(db.String(80), default='Noto Sans Ethiopic, sans-serif')
    logo_display_height = db.Column(db.Integer, default=80)  # px height on site/auth
    transporter_logo_path = db.Column(db.String(255), default='')
    transporter_logo_height = db.Column(db.Integer, default=80)
    auth_logo_height = db.Column(db.Integer, default=80)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class BankAccount(db.Model):
    """Admin-managed bank accounts for customer deposits."""
    id = db.Column(db.Integer, primary_key=True)
    bank_name = db.Column(db.String(120), nullable=False, default='')
    account_name = db.Column(db.String(120), nullable=False, default='')
    account_number = db.Column(db.String(80), nullable=False, default='')
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name_am = db.Column(db.String(120), nullable=False, default='')
    name_en = db.Column(db.String(120), nullable=False, default='')
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    products = db.relationship('Product', backref='category', lazy=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    name_am = db.Column(db.String(120), nullable=False)
    name_en = db.Column(db.String(120), nullable=False)
    description_am = db.Column(db.Text, default='')
    description_en = db.Column(db.Text, default='')
    image_path = db.Column(db.String(255), default='')
    # Measurement unit for variant sizes: kg | L | pcs | pack | bag
    unit = db.Column(db.String(10), default='kg', nullable=False)
    # VAT rate % included in listed prices. 0 = VAT Exempt. e.g. 15 = VAT Inclusive 15%.
    vat_percent = db.Column(db.Float, default=0.0)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    delivery_min_hours = db.Column(db.Integer, default=2)
    delivery_max_hours = db.Column(db.Integer, default=6)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    variants = db.relationship('ProductVariant', backref='product', lazy=True, cascade='all, delete-orphan')


class ProductVariant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    # Numeric size value (kg, liters, pieces, …). Column name kept for DB compatibility.
    weight_kg = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)  # unit price (1–bulk_min-1)
    bulk_min_qty = db.Column(db.Integer, default=10)  # qty at which bulk price applies
    bulk_price = db.Column(db.Float, nullable=True)  # unit price when qty >= bulk_min_qty
    stock = db.Column(db.Integer, default=100)
    is_available = db.Column(db.Boolean, default=True)

    def unit_price_for(self, qty):
        qty = int(qty or 1)
        if self.bulk_price is not None and self.bulk_min_qty and qty >= self.bulk_min_qty:
            return float(self.bulk_price)
        return float(self.price)


class Customer(db.Model):
    """Registered store customer (phone login)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), default='')
    # email kept for DB compatibility; login is by phone only
    email = db.Column(db.String(120), unique=True, nullable=True, index=True)
    phone = db.Column(db.String(30), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Transporter(db.Model):
    """Delivery rider / vehicle driver portal account."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), default='')
    phone = db.Column(db.String(30), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    vehicle_info = db.Column(db.String(120), default='')  # motorcycle / car / plate
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(40), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    customer_name = db.Column(db.String(120), default='')
    phone = db.Column(db.String(30), nullable=False)
    address_text = db.Column(db.Text, default='')
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    location_method = db.Column(db.String(20), default='manual')  # gps or manual
    location_accuracy_m = db.Column(db.Float, nullable=True)  # GPS accuracy in meters if provided
    status = db.Column(db.String(30), default='pending')  # pending, confirmed, preparing, out_for_delivery, delivered, cancelled
    notes = db.Column(db.Text, default='')
    # items_subtotal = sum of line items; delivery_fee separate; total = both
    items_subtotal = db.Column(db.Float, default=0.0)
    delivery_fee = db.Column(db.Float, default=0.0)
    total_amount = db.Column(db.Float, default=0.0)
    vat_mode = db.Column(db.String(20), default='inclusive')  # snapshot at order time
    estimated_delivery = db.Column(db.String(80), default='')
    # Payment (for totals 100–100000 ETB)
    payment_required = db.Column(db.Boolean, default=False)
    payment_status = db.Column(db.String(30), default='not_required')  # not_required, pending, confirmed, rejected
    transaction_id = db.Column(db.String(120), default='')
    receipt_path = db.Column(db.String(255), default='')
    bank_account_id = db.Column(db.Integer, db.ForeignKey('bank_account.id'), nullable=True)
    # Snapshot of bank details at order time (so later edits don't rewrite history)
    bank_name_snapshot = db.Column(db.String(120), default='')
    bank_account_name_snapshot = db.Column(db.String(120), default='')
    bank_account_number_snapshot = db.Column(db.String(80), default='')
    # COD over 100000: transporter sends delivery receipt to admin
    delivery_receipt_path = db.Column(db.String(255), default='')
    cod_amount_collected = db.Column(db.Float, nullable=True)
    delivery_receipt_note = db.Column(db.String(255), default='')
    delivery_receipt_at = db.Column(db.DateTime, nullable=True)
    payment_note = db.Column(db.Text, default='')  # admin note to customer
    stock_released = db.Column(db.Boolean, default=False)  # True once cancelled-order stock was returned
    transporter_id = db.Column(db.Integer, db.ForeignKey('transporter.id'), nullable=True)
    assigned_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan')


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    variant_id = db.Column(db.Integer, db.ForeignKey('product_variant.id'), nullable=False)
    product_name = db.Column(db.String(120), nullable=False)
    weight_kg = db.Column(db.Integer, nullable=False)  # size value (kg / L / pcs …)
    unit = db.Column(db.String(10), default='kg')  # snapshot of product.unit at order time
    vat_percent = db.Column(db.Float, default=0.0)  # snapshot of product VAT % at order time
    unit_price = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Integer, default=1)
    subtotal = db.Column(db.Float, nullable=False)



class StatusLog(db.Model):
    """Audit trail for order status / payment changes."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False, index=True)
    actor = db.Column(db.String(80), default='')  # admin:username / transporter:phone / system
    field = db.Column(db.String(40), default='status')  # status | payment_status
    old_value = db.Column(db.String(40), default='')
    new_value = db.Column(db.String(40), default='')
    note = db.Column(db.String(255), default='')
    created_at = db.Column(db.DateTime, default=utcnow)


class RateHit(db.Model):
    """One row per rate-limited event (login failures, chat messages, ...)."""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(160), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class PhoneOtp(db.Model):
    """One-time codes for phone verification (register / password reset)."""
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(30), nullable=False, index=True)
    purpose = db.Column(db.String(20), nullable=False, default='register')  # register | reset
    code_hash = db.Column(db.String(256), nullable=False)
    attempts = db.Column(db.Integer, default=0)
    max_attempts = db.Column(db.Integer, default=5)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class SupportRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), default='')
    phone = db.Column(db.String(30), default='')
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='open')  # open, closed
    created_at = db.Column(db.DateTime, default=utcnow)


class ChatSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    visitor_name = db.Column(db.String(120), default='')
    visitor_phone = db.Column(db.String(30), default='')
    status = db.Column(db.String(20), default='open')  # open, closed
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    messages = db.relationship('ChatMessage', backref='chat_session', lazy=True, cascade='all, delete-orphan', order_by='ChatMessage.created_at')


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_session.id'), nullable=False, index=True)
    sender = db.Column(db.String(20), nullable=False)  # visitor | admin | ai
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Babel locale selector
# ---------------------------------------------------------------------------
def get_locale():
    if 'lang' in session:
        return session['lang']
    return request.accept_languages.best_match(app.config['BABEL_SUPPORTED_LOCALES']) or 'am'

babel.init_app(app, locale_selector=get_locale)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Admin, int(user_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_settings():
    s = SiteSettings.query.first()
    if not s:
        s = SiteSettings()
        db.session.add(s)
        db.session.commit()
    return s


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif', 'webp'}


_PIL_FORMAT_EXT = {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}


def _sniff_image_ext(head):
    """Detect the real image type from its first bytes (never trust the filename)."""
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


def save_image(file, prefix='img', private=False):
    """Validate and store an uploaded image.

    Returns a relative path ('uploads/..' for public images, 'private/..' for
    receipts) or None when the file is not a real, valid image.
    """
    if not file or not file.filename:
        return None
    try:
        file.stream.seek(0)
        head = file.stream.read(16)
        file.stream.seek(0)
    except Exception:
        return None
    sniffed = _sniff_image_ext(head)
    if not sniffed:
        return None

    folder = PRIVATE_UPLOAD_FOLDER if private else app.config['UPLOAD_FOLDER']
    os.makedirs(folder, exist_ok=True)
    stamp = utcnow().strftime('%Y%m%d%H%M%S')
    token = os.urandom(8).hex() if private else os.urandom(4).hex()
    max_side = 1600 if (private or prefix in ('logo', 'authlogo', 'trlogo')) else 800

    if not HAS_PILLOW:
        name = f"{prefix}_{stamp}_{token}.{sniffed}"
        try:
            file.save(os.path.join(folder, name))
        except Exception:
            return None
        return f"{'private' if private else 'uploads'}/{name}"

    try:
        img = Image.open(file.stream)
        fmt = img.format
        ext = _PIL_FORMAT_EXT.get(fmt)
        if not ext:
            return None
        img.load()  # fully decode: rejects truncated / malformed files
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img.thumbnail((max_side, max_side))
        save_kwargs = {}
        if fmt == 'JPEG':
            if img.mode != 'RGB':
                img = img.convert('RGB')
            save_kwargs = {'quality': 85, 'optimize': True}
        elif fmt == 'PNG':
            if img.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P'):
                img = img.convert('RGBA')
            save_kwargs = {'optimize': True}
        elif fmt == 'WEBP':
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGBA')
            save_kwargs = {'quality': 85}
        name = f"{prefix}_{stamp}_{token}.{ext}"
        img.save(os.path.join(folder, name), format=fmt, **save_kwargs)
        return f"{'private' if private else 'uploads'}/{name}"
    except Exception:
        app.logger.warning('Rejected an invalid image upload', exc_info=True)
        return None


def _resolve_stored_file(rel):
    """Map a stored relative path to (folder, filename) or (None, None)."""
    rel = (rel or '').replace('\\', '/')
    if rel.startswith('private/'):
        return PRIVATE_UPLOAD_FOLDER, os.path.basename(rel)
    if rel.startswith('uploads/'):
        return app.config['UPLOAD_FOLDER'], os.path.basename(rel)
    return None, None


def delete_stored_file(rel):
    folder, name = _resolve_stored_file(rel)
    if not folder or not name:
        return
    try:
        os.remove(os.path.join(folder, name))
    except OSError:
        pass


def generate_order_number():
    return f"DD{utcnow().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(8).upper()}"


def validate_customer_password(password):
    """Min 6 chars, 1 uppercase, 1 digit, 1 special character."""
    if not password or len(password) < 6:
        return False, _('Password must be at least 6 characters')
    if not any(c.isupper() for c in password):
        return False, _('Password must include at least 1 capital letter (A-Z)')
    if not any(c.isdigit() for c in password):
        return False, _('Password must include at least 1 number')
    if not any((not c.isalnum() and not c.isspace()) for c in password):
        return False, _('Password must include at least 1 symbol (!@#$)')
    return True, ''


# ---- Rate limiting (stored in the DB so it is shared by every worker) ------
def client_ip():
    return (request.remote_addr or 'unknown')[:64]


def rate_count(key, window_seconds):
    since = utcnow() - timedelta(seconds=window_seconds)
    return RateHit.query.filter(RateHit.key == key, RateHit.created_at >= since).count()


def rate_hit(key):
    db.session.add(RateHit(key=key))
    if secrets.randbelow(50) == 0:  # occasional cleanup of old rows
        RateHit.query.filter(RateHit.created_at < utcnow() - timedelta(days=1)).delete(synchronize_session=False)
    db.session.commit()


def rate_exceeded(scope, limit, window_seconds):
    """Count this request against `scope` for the client IP; True if over the limit."""
    key = f'{scope}:{client_ip()}'
    if rate_count(key, window_seconds) >= limit:
        return True
    rate_hit(key)
    return False


LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 15 * 60
# Admin lockout is stricter on IP (prevents easy lockout of the real admin)
# and more lenient on username so an attacker cannot easily lock the admin account.
ADMIN_IP_MAX_FAILS = 12
ADMIN_USER_MAX_FAILS = 15


def login_blocked(scope, ident):
    ident = (ident or '').strip().lower()[:80]
    if scope == 'admin':
        return (rate_count(f'loginfail:{scope}:{ident}', LOGIN_WINDOW) >= ADMIN_USER_MAX_FAILS or
                rate_count(f'loginfail-ip:{scope}:{client_ip()}', LOGIN_WINDOW) >= ADMIN_IP_MAX_FAILS)
    # Customer / transporter: primarily lock by IP so an attacker cannot lock someone else's phone.
    # Identity lock is higher threshold.
    if scope in ('customer', 'transporter'):
        return (rate_count(f'loginfail:{scope}:{ident}', LOGIN_WINDOW) >= 12 or
                rate_count(f'loginfail-ip:{scope}:{client_ip()}', LOGIN_WINDOW) >= LOGIN_MAX_FAILS)
    return (rate_count(f'loginfail:{scope}:{ident}', LOGIN_WINDOW) >= LOGIN_MAX_FAILS or
            rate_count(f'loginfail-ip:{scope}:{client_ip()}', LOGIN_WINDOW) >= 30)


def login_failed(scope, ident):
    ident = (ident or '').strip().lower()[:80]
    db.session.add(RateHit(key=f'loginfail:{scope}:{ident}'))
    db.session.add(RateHit(key=f'loginfail-ip:{scope}:{client_ip()}'))
    db.session.commit()


def login_succeeded(scope, ident):
    ident = (ident or '').strip().lower()[:80]
    RateHit.query.filter(RateHit.key == f'loginfail:{scope}:{ident}').delete(synchronize_session=False)
    db.session.commit()



def attach_guest_orders_to_customer(customer):
    """Link past guest checkouts (same phone) to this customer account."""
    if not customer or not customer.phone:
        return
    phones = {customer.phone}
    # also try denormalized local form
    p = customer.phone
    if p.startswith('+251') and len(p) >= 13:
        phones.add('0' + p[4:])
    Order.query.filter(
        Order.customer_id.is_(None),
        Order.phone.in_(list(phones)),
    ).update({Order.customer_id: customer.id}, synchronize_session=False)
    db.session.commit()


def start_fresh_session(**values):
    """New session after login (prevents session fixation); keeps language & pending orders."""
    keep = {k: session[k] for k in ('lang', 'pending_orders', '_csrf') if k in session}
    session.clear()
    session.update(keep)
    session.update(values)
    session.setdefault('_csrf', secrets.token_hex(16))


def get_current_customer():
    cid = session.get('customer_id')
    if not cid:
        return None
    c = db.session.get(Customer, cid)
    if c and c.is_active:
        return c
    session.pop('customer_id', None)
    return None


def get_current_transporter():
    tid = session.get('transporter_id')
    if not tid:
        return None
    t = db.session.get(Transporter, tid)
    if t and t.is_active:
        return t
    return None


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('admin_login', next=request.path))
        # ensure it is Admin account (not confused with other sessions)
        if not isinstance(current_user, Admin):
            logout_user()
            return redirect(url_for('admin_login', next=request.path))
        return f(*args, **kwargs)
    return decorated




def release_stale_unpaid_orders(max_age_hours=48):
    """Cancel old unpaid/rejected pending orders so reserved stock is released."""
    cutoff = utcnow() - timedelta(hours=max_age_hours)
    stale = (Order.query.filter(
        Order.status == 'pending',
        Order.payment_required == True,
        Order.payment_status.in_(['pending', 'rejected']),
        Order.created_at < cutoff,
        Order.stock_released == False,
    ).limit(50).all())
    for order in stale:
        err = change_order_status(order, 'cancelled', actor='system', note='auto-cancel unpaid after timeout')
        if not err:
            order.payment_note = (order.payment_note or '') + ' [Auto-cancelled: payment not confirmed in time]'
    if stale:
        db.session.commit()
    return len(stale)


def log_order_change(order, field, old_value, new_value, actor='', note=''):
    if str(old_value) == str(new_value):
        return
    db.session.add(StatusLog(
        order_id=order.id,
        actor=(actor or '')[:80],
        field=(field or 'status')[:40],
        old_value=str(old_value or '')[:40],
        new_value=str(new_value or '')[:40],
        note=(note or '')[:255],
    ))


def change_order_status(order, new_status, actor='', note=''):
    """Change an order's status and keep stock consistent.

    Cancelling returns the reserved stock exactly once; re-opening a cancelled
    order reserves it again (and fails if there is not enough stock).
    Returns an error message, or None on success.
    """
    old = order.status
    if new_status == old:
        return None
    # Allowed transitions (prevent e.g. delivered -> pending)
    ALLOWED = {
        'pending': {'confirmed', 'preparing', 'cancelled'},
        'confirmed': {'preparing', 'out_for_delivery', 'cancelled'},
        'preparing': {'out_for_delivery', 'cancelled'},
        'out_for_delivery': {'delivered', 'cancelled'},
        'delivered': set(),  # terminal
        'cancelled': {'pending', 'confirmed'},  # re-open only
    }
    if new_status not in ALLOWED.get(old, set()):
        return _('Cannot change status from %(old)s to %(new)s', old=old, new=new_status)
    if (new_status in ('out_for_delivery', 'delivered') and order.payment_required
            and order.payment_status != 'confirmed'):
        return _('Confirm the bank payment before dispatching or delivering this order')
    if new_status == 'cancelled':
        if not order.stock_released:
            for it in order.items:
                v = db.session.get(ProductVariant, it.variant_id)
                if v:
                    v.stock = (v.stock or 0) + it.quantity
            order.stock_released = True
    elif old == 'cancelled' and order.stock_released:
        variants = []
        for it in order.items:
            v = db.session.get(ProductVariant, it.variant_id)
            if not v or (v.stock or 0) < it.quantity:
                return _('Not enough stock to re-open this order')
            variants.append((v, it.quantity))
        for v, qty in variants:
            v.stock -= qty
        order.stock_released = False
    old_status = old
    order.status = new_status
    log_order_change(order, 'status', old_status, new_status, actor=actor, note=note)
    return None


@contextmanager
def init_lock():
    """Cross-process lock so only one gunicorn worker initialises the database."""
    try:
        import fcntl
    except ImportError:  # Windows: single process, no lock needed
        fcntl = None
    fh = open(os.path.join(app.instance_path, 'init.lock'), 'a+')
    try:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


# ---------------------------------------------------------------------------
# Context processors
# ---------------------------------------------------------------------------
@app.context_processor
def inject_globals():
    settings = get_settings()
    # Map tiles: set MAP_TILES_URL on Render (MapTiler/Mapbox). Esri fallback is for local dev only.
    map_tiles_url = (os.environ.get('MAP_TILES_URL') or '').strip() or (
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}'
    )
    map_attribution = (os.environ.get('MAP_ATTRIBUTION') or '').strip() or 'Tiles &copy; Esri'
    return {
        'settings': settings,
        'current_lang': get_locale(),
        'languages': app.config['LANGUAGES'],
        'now': utcnow(),
        'prepay_min': PREPAY_MIN,
        'prepay_max': PREPAY_MAX,
        'current_customer': get_current_customer(),
        'map_tiles_url': map_tiles_url,
        'map_attribution': map_attribution,
        'unit_label': unit_label,
        'unit_labels': UNIT_LABELS,
        'allowed_units': ALLOWED_UNITS,
        'vat_label': vat_label,
        'product_vat_label': product_vat_label,
        'site_delivery_fee': float(getattr(settings, 'delivery_fee', 0) or 0),
        'default_vat_percent': float(getattr(settings, 'default_vat_percent', 15) or 0),
    }


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.route('/set_language/<lang>')
def set_language(lang):
    if lang in app.config['LANGUAGES']:
        session['lang'] = lang
    return redirect(safe_referrer() or url_for('index'))


@app.route('/')
def index():
    categories = Category.query.filter_by(is_active=True).order_by(Category.sort_order, Category.id).all()
    cat_id = safe_int(request.args.get('category'), None)
    q = Product.query.filter_by(is_active=True)
    if cat_id:
        q = q.filter_by(category_id=cat_id)
    products = q.order_by(Product.sort_order, Product.id).all()
    return render_template('index.html', products=products, categories=categories, active_category=cat_id)


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    product = db.get_or_404(Product, product_id)
    if not product.is_active:
        abort(404)
    return render_template('product.html', product=product)


@app.route('/cart')
def cart():
    try:
        release_stale_unpaid_orders()
    except Exception:
        db.session.rollback()
    pending_orders = []
    nums = session.get('pending_orders', [])
    if nums:
        pending_orders = Order.query.filter(Order.order_number.in_(nums)).order_by(Order.created_at.desc()).all()
    cust = get_current_customer()
    if cust:
        extra = Order.query.filter_by(customer_id=cust.id).filter(
            Order.status.in_(['pending', 'confirmed', 'preparing', 'out_for_delivery'])
        ).order_by(Order.created_at.desc()).limit(15).all()
        seen = {o.order_number for o in pending_orders}
        for o in extra:
            if o.order_number not in seen:
                pending_orders.append(o)
    return render_template('cart.html', pending_orders=pending_orders)


def _json_error(message, status=400, **extra):
    payload = {'success': False, 'error': message}
    payload.update(extra)
    return jsonify(payload), status


@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    settings = get_settings()
    banks = BankAccount.query.filter_by(is_active=True).order_by(BankAccount.sort_order, BankAccount.id).all()
    if request.method != 'POST':
        return render_template('checkout.html', banks=banks)

    # Only successful orders count towards the limit (typos / validation errors do not).
    if rate_count(f'checkout:{client_ip()}', 3600) >= 60:
        return _json_error(_('Too many attempts. Please wait a while and try again.'), 429)

    # Idempotency: same client key within a short window returns the previous order
    idem = ''
    if request.is_json:
        body = request.get_json(silent=True) or {}
        idem = (body.get('idempotency_key') or '')[:64]
    else:
        idem = (request.form.get('idempotency_key') or '')[:64]
    if idem:
        prev = session.get('checkout_idem') or {}
        if prev.get('key') == idem and prev.get('order_number'):
            return jsonify({
                'success': True,
                'order_number': prev['order_number'],
                'redirect': url_for('order_success', order_number=prev['order_number']),
                'duplicate': True,
            })

    try:
        if request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                data = {}
            cart_raw = data.get('cart', [])
        else:
            data = request.form
            cart_raw = data.get('cart', '[]')
        cart_items = json.loads(cart_raw) if isinstance(cart_raw, str) else cart_raw
    except (TypeError, ValueError):
        return _json_error(_('Invalid cart data'))

    if not isinstance(cart_items, list) or not cart_items:
        return _json_error(_('Cart is empty'))
    if len(cart_items) > 50:
        return _json_error(_('Too many items in cart'))

    raw_phone = (data.get('phone') or '').strip()
    if not raw_phone:
        return _json_error(_('Phone number is required'))
    phone = normalize_phone(raw_phone)
    if not phone:
        return _json_error(_('Please enter a valid Ethiopian phone number (e.g. 0911 234 567)'))

    customer_name = (data.get('customer_name') or '').strip()[:120]
    address_text = (data.get('address_text') or '').strip()[:500]
    notes = (data.get('notes') or '').strip()[:1000]
    transaction_id = ' '.join((data.get('transaction_id') or '').split()).upper()[:120]
    location_method = (data.get('location_method') or 'manual').strip()
    if location_method not in ('gps', 'manual'):
        location_method = 'manual'

    lat_f = parse_coord(data.get('latitude'), 90)
    lng_f = parse_coord(data.get('longitude'), 180)
    if lat_f is None or lng_f is None:
        return _json_error(_('Please set your location on the map'))
    # Ethiopia approximate bounding box (roughly 3.4°N–14.9°N, 32.9°E–48.0°E)
    if not (3.4 <= lat_f <= 14.9 and 32.9 <= lng_f <= 48.0):
        return _json_error(_('Delivery location must be inside Ethiopia'))
    # Reject default centre / unconfirmed pin. Client must send location_confirmed=1 only after user action.
    loc_ok = str(data.get('location_confirmed') or '').strip().lower() in ('1', 'true', 'yes')
    # Require explicit confirmation for both GPS and manual (prevents silent default pin)
    if not loc_ok:
        return _json_error(_('Please set your exact delivery location on the map'))
    if abs(lat_f - 9.03) < 0.0005 and abs(lng_f - 38.74) < 0.0005:
        return _json_error(_('Please set your exact delivery location on the map'))

    # Merge quantities by variant_id
    merged = {}
    for item in cart_items:
        if not isinstance(item, dict):
            return _json_error(_('Invalid product in cart'))
        vid = safe_int(item.get('variant_id'), None)
        if vid is None:
            return _json_error(_('Invalid product in cart'))
        qty = safe_int(item.get('quantity', 1), None)
        if qty is None or qty < 1 or qty > 1000:
            return _json_error(_('Invalid quantity'))
        merged[vid] = merged.get(vid, 0) + qty

    total = 0.0
    order_items_data = []
    for vid, qty in merged.items():
        variant = db.session.get(ProductVariant, vid)
        if not variant or not variant.is_available:
            return _json_error(_('Product unavailable'))
        product = variant.product
        if not product or not product.is_active:
            return _json_error(_('Product unavailable'))
        if (variant.stock or 0) < qty:
            return _json_error(_('Not enough stock: %(name)s', name=product.name_en or product.name_am), 409)
        unit = variant.unit_price_for(qty)
        subtotal = unit * qty
        total += subtotal
        order_items_data.append({
            'variant': variant,
            'product': product,
            'qty': qty,
            'unit': unit,
            'subtotal': subtotal
        })

    # Auto-estimate from product settings (admin can still override later)
    settings = get_settings()
    items_subtotal = float(total)
    # Delivery fee is set by admin AFTER the order is placed (Orders → set delivery price)
    delivery_fee = 0.0
    grand_total = items_subtotal  # updated when admin sets delivery fee
    vat_flags = set()
    for oi in order_items_data:
        pct = float(getattr(oi['product'], 'vat_percent', 0) or 0)
        vat_flags.add('exempt' if pct <= 0 else 'inclusive')
    vat_mode = 'mixed' if len(vat_flags) > 1 else (next(iter(vat_flags)) if vat_flags else 'inclusive')

    mins, maxs = [], []
    for oi in order_items_data:
        p = oi['product']
        mn = getattr(p, 'delivery_min_hours', None) or getattr(settings, 'delivery_min_hours', 2) or 2
        mx = getattr(p, 'delivery_max_hours', None) or getattr(settings, 'delivery_max_hours', 6) or 6
        mins.append(int(mn))
        maxs.append(int(mx))
    if mins and maxs:
        dmin, dmax = max(mins), max(maxs)
        estimated = f"{dmin}-{dmax}" if dmin != dmax else str(dmin)
    else:
        estimated = ''

    # No bank payment at checkout — admin quotes delivery fee first, then requests payment
    payment_required = False
    payment_status = 'awaiting_quote'
    receipt_path = ''
    bank_id = None
    bank = None
    transaction_id = ''

    cust = get_current_customer()
    acc_raw = data.get('location_accuracy_m')
    try:
        loc_acc = float(acc_raw) if acc_raw not in (None, '') else None
        if loc_acc is not None and (loc_acc <= 0 or loc_acc > 50000):
            loc_acc = None
    except (TypeError, ValueError):
        loc_acc = None
    try:
        order = Order(
            order_number=generate_order_number(),
            customer_id=cust.id if cust else None,
            customer_name=customer_name or (cust.name if cust else ''),
            phone=phone,
            address_text=address_text,
            latitude=lat_f,
            longitude=lng_f,
            location_method=location_method,
            location_accuracy_m=loc_acc,
            notes=notes,
            items_subtotal=items_subtotal,
            delivery_fee=delivery_fee,
            total_amount=grand_total,
            vat_mode=vat_mode,
            estimated_delivery=estimated,
            status='pending',
            payment_required=payment_required,
            payment_status=payment_status,
            transaction_id=transaction_id,
            receipt_path=receipt_path,
            bank_account_id=bank_id or None,
            bank_name_snapshot='',
            bank_account_name_snapshot='',
            bank_account_number_snapshot='',
            payment_note=_('Waiting for admin to set delivery fee'),
        )
        db.session.add(order)
        db.session.flush()

        lang = get_locale()
        for oi in order_items_data:
            # Atomic reservation: only succeeds while enough stock is left (no overselling)
            reserved = (ProductVariant.query
                        .filter(ProductVariant.id == oi['variant'].id,
                                ProductVariant.stock >= oi['qty'])
                        .update({ProductVariant.stock: ProductVariant.stock - oi['qty']},
                                synchronize_session=False))
            if reserved != 1:
                label = oi['product'].name_en or oi['product'].name_am
                db.session.rollback()
                delete_stored_file(receipt_path)
                return _json_error(_('Not enough stock: %(name)s', name=label), 409)
            db.session.add(OrderItem(
                order_id=order.id,
                product_id=oi['product'].id,
                variant_id=oi['variant'].id,
                product_name=(
                    f"{oi['product'].name_am or ''} / {oi['product'].name_en or ''}".strip(' /')
                    if (oi['product'].name_am and oi['product'].name_en
                        and oi['product'].name_am != oi['product'].name_en)
                    else (oi['product'].name_am if lang == 'am' else (oi['product'].name_en or oi['product'].name_am))
                ),
                weight_kg=oi['variant'].weight_kg,
                unit=(oi['product'].unit or 'kg'),
                vat_percent=float(getattr(oi['product'], 'vat_percent', 0) or 0),
                unit_price=oi['unit'],
                quantity=oi['qty'],
                subtotal=oi['subtotal']
            ))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        delete_stored_file(receipt_path)
        app.logger.exception('Checkout failed')
        return _json_error(_('Server error. Please try again.'), 500)

    rate_hit(f'checkout:{client_ip()}')
    # remember for cart "Pending" display
    pending = session.get('pending_orders', [])
    pending.insert(0, order.order_number)
    session['pending_orders'] = pending[:20]
    session.permanent = True  # keep guest pending_orders across browser restarts (14 days)
    if idem:
        session['checkout_idem'] = {'key': idem, 'order_number': order.order_number}
    return jsonify({
        'success': True,
        'order_number': order.order_number,
        'estimated': estimated,
        'payment_required': payment_required,
        'redirect': url_for('order_success', order_number=order.order_number)
    })


def _can_view_order(order):
    allowed = session.get('pending_orders') or []
    cust = get_current_customer()
    ok = order.order_number in allowed
    if cust and order.customer_id == cust.id:
        ok = True
    if current_user.is_authenticated and isinstance(current_user, Admin):
        ok = True
    return ok


@app.route('/order/success/<order_number>')
def order_success(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    if not _can_view_order(order):
        abort(403)
    banks = BankAccount.query.filter_by(is_active=True).order_by(BankAccount.sort_order, BankAccount.id).all()
    return render_template('order_success.html', order=order, banks=banks)


@app.route('/api/order/<order_number>/status')
def api_order_status(order_number):
    """Lightweight poll for order-success page (1s live refresh, no badge)."""
    order = Order.query.filter_by(order_number=order_number).first()
    if not order or not _can_view_order(order):
        return jsonify({'error': 'forbidden'}), 403
    return jsonify({
        'order_number': order.order_number,
        'status': order.status,
        'payment_status': order.payment_status,
        'payment_required': bool(order.payment_required),
        'payment_note': order.payment_note or '',
        'estimated_delivery': order.estimated_delivery or '',
        'items_subtotal': float(order.items_subtotal or 0),
        'delivery_fee': float(order.delivery_fee or 0),
        'total_amount': float(order.total_amount or 0),
    })


@app.route('/files/order/<int:oid>/<kind>')
def order_file(oid, kind):
    """Receipts are private: only the admin, the owner or the assigned transporter can see them."""
    if kind not in ('receipt', 'cod'):
        abort(404)
    order = db.get_or_404(Order, oid)
    rel = order.receipt_path if kind == 'receipt' else order.delivery_receipt_path
    if not rel:
        abort(404)
    allowed = current_user.is_authenticated and isinstance(current_user, Admin)
    if not allowed and kind == 'cod':
        t = get_current_transporter()
        allowed = bool(t and order.transporter_id == t.id)
    if not allowed and kind == 'receipt':
        cust = get_current_customer()
        allowed = bool(cust and order.customer_id == cust.id)
        if not allowed:
            allowed = order.order_number in (session.get('pending_orders') or [])
    if not allowed:
        abort(403)
    folder, name = _resolve_stored_file(rel)
    if not folder or not name:
        abort(404)
    resp = send_from_directory(folder, name)
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp


@app.route('/help', methods=['GET', 'POST'])
def help_page():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()[:120]
        phone = request.form.get('phone', '').strip()[:30]
        message = request.form.get('message', '').strip()
        if not message:
            flash(_('Please write your message.'), 'error')
            return redirect(url_for('help_page'))
        if len(message) > 2000:
            flash(_('Your message is too long (max 2000 characters).'), 'error')
            return redirect(url_for('help_page'))
        if rate_exceeded('help', 20, 3600):
            flash(_('Too many requests. Please try again later.'), 'error')
            return redirect(url_for('help_page'))
        req = SupportRequest(name=name, phone=phone, message=message)
        db.session.add(req)
        db.session.commit()
        flash(_('Your request has been submitted. We will contact you soon.'), 'success')
        return redirect(url_for('help_page'))
    return render_template('help.html')


def detect_lang(text):
    """Simple Amharic detection via Ethiopic script."""
    for ch in text or '':
        if '\u1200' <= ch <= '\u137F':
            return 'am'
    return 'en'


def ai_help_reply(user_message, settings=None):
    """
    Offline AI help assistant (FAQ + rules) for when admin is unavailable.
    Answers in Amharic or English based on the user's message.
    """
    settings = settings or get_settings()
    lang = detect_lang(user_message)
    msg = (user_message or '').strip().lower()
    # normalize some Amharic without needing full NLP
    raw = user_message or ''

    phone = getattr(settings, 'help_phone', '') or ''
    telegram = getattr(settings, 'help_telegram', '') or ''
    email = getattr(settings, 'help_email', '') or ''
    site = getattr(settings, 'site_name', 'Dado Delivery')

    def am_en(am, en):
        return am if lang == 'am' else en

    # greetings — whole words only (avoid matching "hi" inside "which" / "shipping")
    import re as _re
    greet_pat = _re.compile(
        r'(^|\s)(hello|hi|hey|ሰላም|ሀይ|ሃይ|ጤና|እንደምን|good\s+morning|good\s+evening)(\s|$|[!?.])',
        _re.I
    )
    if greet_pat.search(msg) and len(msg) < 40:
        return am_en(
            f'ሰላም! እኔ የ {site} AI ረዳት ነኝ። Admin ሳይኖር መረጃ እሰጣለሁ።\n'
            'ስለ ምርት፣ ዋጋ፣ ማድረሻ፣ ክፍያ ወይም ትዕዛዝ ይጠይቁኝ።',
            f'Hello! I am the {site} AI assistant. I can help while admin is away.\n'
            'Ask about products, prices, delivery, payment, or orders.'
        )

    # products / catalog
    if any(k in msg or k in raw for k in ['product', 'ምርት', 'ዕቃ', 'እቃ', 'ምስር', 'ባቄላ', 'ሽሮ', 'አተር', 'ጓያ', 'ሽንብራ', 'lentil', 'bean', 'catalog', 'what do you sell']):
        products = Product.query.filter_by(is_active=True).order_by(Product.sort_order).limit(12).all()
        if products:
            lines = []
            for p in products:
                name = p.name_am if lang == 'am' else (p.name_en or p.name_am)
                ul = unit_label(p.unit, lang)
                vars_ = ', '.join(f'{v.weight_kg}{ul}' for v in p.variants if v.is_available)
                lines.append(f'• {name}' + (f' ({vars_})' if vars_ else ''))
            body = '\n'.join(lines)
            return am_en(
                f'ያሉ ምርቶች፦\n{body}\n\nመጠኖች በምርት አይነት ይለያያሉ (ኪ.ግ፣ ሊትር፣ ብዛት…)። ከመነሻ ገጽ ምርጫ ያድርጉ።',
                f'Available products:\n{body}\n\nSizes depend on the product (kg, liters, pcs…). Choose from the home page.'
            )
        return am_en('በአሁኑ ሰዓት የታተሙ ምርቶች የሉም።', 'No published products right now.')

    # price
    if any(k in msg or k in raw for k in ['price', 'ዋጋ', 'ስንት', 'cost', 'how much', 'በስንት']):
        return am_en(
            'ዋጋው በእያንዳንዱ ምርትና መጠን ይለያያል፤ ከ10 በላይ ፍሬ ሲያዙ ቅናሽ (bulk) ሊኖር ይችላል።\n'
            'ትክክለኛ ዋጋ ለማየት ምርቱን ከመነሻ ገጽ ይምረጡ።',
            'Prices vary by product and size. Buying 10+ units may get bulk discount.\n'
            'Open a product on the home page for exact prices.'
        )

    # delivery
    if any(k in msg or k in raw for k in ['delivery', 'ማድረሻ', 'ይደርሳል', 'መጓጓዣ', 'transporter', 'አድራሽ', 'location', 'ቦታ', 'map', 'ካርታ', 'gps']):
        return am_en(
            'ማድረሻ፦ ትዕዛዝ ሲያስገቡ GPS ወይም ካርታ ላይ ቦታዎን ያስቀምጡ።\n'
            'የመድረሻ ሰዓትን Admin ከትዕዛዝ በኋላ ያስተካክላል — በ Cart ላይ ያያሉ።\n'
            'ከ 100,000 ብር በላይ ክፍያ በመላኪያ ጊዜ ነው።',
            'Delivery: set your place with GPS or the map at checkout.\n'
            'Admin sets delivery hours after you order — see it on Cart.\n'
            'Over 100,000 ETB is cash/pay on delivery.'
        )

    # payment
    if any(k in msg or k in raw for k in ['payment', 'ክፍያ', 'ባንክ', 'bank', 'receipt', 'ደረሰኝ', 'transaction', 'ትራንዛክሽን', 'pay']):
        return am_en(
            'ክፍያ፦\n'
            '• 100–100,000 ብር፦ ባንክ ይምረጡ፣ ትራንዛክሽን ID ወይም የደረሰኝ ፎቶ ይላኩ። Admin ካረጋገጠ ትዕዛዝ ይረጋገጣል።\n'
            '• ከ 100,000 በላይ፦ በመላኪያ ጊዜ ይከፍላሉ።',
            'Payment:\n'
            '• 100–100,000 ETB: pick a bank, send transaction ID or receipt photo. Admin confirms.\n'
            '• Over 100,000 ETB: pay on delivery.'
        )

    # order status / track
    if any(k in msg or k in raw for k in ['order', 'ትዕዛዝ', 'ኦርደር', 'status', 'ሁኔታ', 'pending', 'track', 'የት ደረሰ']):
        return am_en(
            'ትዕዛዝዎን ለመከታተል Cart ገጽን ይክፈቱ — Pending እና የመድረሻ ግምት ይታያል።\n'
            'የትዕዛዝ ቁጥርዎን (DD...) ካለዎት እዚህ ይጻፉልን።',
            'Track orders on the Cart page — Pending and delivery estimate appear there.\n'
            'If you have an order number (DD...), type it here.'
        )

    # hours / contact
    if any(k in msg or k in raw for k in ['contact', 'ስልክ', 'phone', 'telegram', 'ቴሌግራም', 'email', 'ሰዓት', 'hours', 'admin']):
        contact = []
        if phone:
            contact.append(f'📞 {phone}')
        if telegram:
            contact.append(f'Telegram: {telegram}')
        if email:
            contact.append(f'Email: {email}')
        c = '\n'.join(contact) if contact else am_en('Admin በቅርቡ ይመልሳል።', 'Admin will reply soon.')
        return am_en(
            f'ሰው ማነጋገር ከፈለጉ፦\n{c}\n\nይህ መልእክት ለ Adminም ይቀመጣል።',
            f'To reach a person:\n{c}\n\nYour message is also saved for Admin.'
        )

    # account
    if any(k in msg or k in raw for k in ['register', 'login', 'account', 'ተመዝገብ', 'ግባ', 'አካውንት', 'password', 'የይለፍ']):
        return am_en(
            'አካውንት፦ በስልክ ቁጥር ይመዝገቡ። የይለፍ ቃል ቢያንስ 6 ቁምፊ፣ 1 አቢይ ሆሄ፣ 1 ቁጥር፣ 1 ምልክት።\n'
            'ከተመዘገቡ በኋላ ከማንኛውም መሣሪያ Login ማድረግ ይችላሉ። የይለፍ ቃል ከረሱ “የይለፍ ቃል ረሳሁ” ይጠቀሙ።',
            'Account: Register with your phone number. Password needs 6+ chars, 1 capital, 1 number, 1 symbol.\n'
            'After registering you can log in from any device. Use Forgot password if needed.')

    # thanks — whole words only
    if _re.search(r'(^|\s)(thanks|thank|thank you|okay|ok|አመሰግን|እሺ)(\s|$|[!?.])', msg, _re.I) and len(msg) < 30:
        return am_en('በደስታ! ሌላ ጥያቄ ካለዎት ይጻፉ።', 'You are welcome! Ask if you need anything else.')

    # default
    return am_en(
        'ገባኝ። Admin ሳይኖር እኔ (AI) እረዳዎታለሁ።\n'
        'ይጠይቁ፦ ምርት · ዋጋ · ማድረሻ · ክፍያ · ትዕዛዝ · ስልክ\n'
        f'ወይም ሰው፦ {phone or "Admin በቅርቡ"}',
        'Got it. I (AI) can help while admin is away.\n'
        'Ask about: products · price · delivery · payment · order · contact\n'
        f'Or a person: {phone or "Admin soon"}'
    )



# ---- Live chat API ----
SESSION_KEY_RE = re.compile(r'^[0-9a-f]{32}$')


@app.route('/api/chat/messages', methods=['GET'])
def chat_get_messages():
    session_key = request.args.get('session_key', '').strip()
    if not session_key or not SESSION_KEY_RE.match(session_key):
        return jsonify({'messages': []})
    chat = ChatSession.query.filter_by(session_key=session_key).first()
    if not chat:
        return jsonify({'messages': [], 'status': 'new'})
    msgs = [{
        'id': m.id,
        'sender': m.sender,
        'message': m.message,
        'created_at': (to_addis(m.created_at).strftime('%H:%M') if m.created_at else '')
    } for m in chat.messages]
    return jsonify({'messages': msgs, 'status': chat.status})


@app.route('/api/chat/send', methods=['POST'])
def chat_send():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    session_key = (data.get('session_key') or '').strip()
    message = (data.get('message') or '').strip()
    name = (data.get('name') or '').strip()[:120]
    phone = (data.get('phone') or '').strip()[:30]
    if not message:
        return jsonify({'success': False, 'error': 'empty'}), 400
    if len(message) > 2000:
        return jsonify({'success': False, 'error': _('Message too long')}), 400
    if rate_exceeded('chat', 120, 3600):
        return jsonify({'success': False, 'error': _('Too many messages. Please wait.')}), 429
    # Only keys we generated ourselves are accepted (stops arbitrary key creation)
    if not SESSION_KEY_RE.match(session_key):
        session_key = os.urandom(16).hex()

    chat = ChatSession.query.filter_by(session_key=session_key).first()
    if chat:
        # rate limit: max 30 visitor messages per chat per hour
        recent_count = ChatMessage.query.filter_by(session_id=chat.id, sender='visitor').filter(
            ChatMessage.created_at >= utcnow() - timedelta(hours=1)
        ).count()
        if recent_count >= 30:
            return jsonify({'success': False, 'error': _('Too many messages. Please wait.')}), 429
        if name:
            chat.visitor_name = name
        if phone:
            chat.visitor_phone = phone
        chat.status = 'open'
        chat.updated_at = utcnow()
    else:
        chat = ChatSession(session_key=session_key, visitor_name=name, visitor_phone=phone, status='open')
        db.session.add(chat)
        db.session.flush()

    msg = ChatMessage(session_id=chat.id, sender='visitor', message=message)
    db.session.add(msg)
    db.session.flush()

    ai_payload = None
    # AI only when admin enabled it in Settings AND admin has not replied in last 30 min
    settings = get_settings()
    if getattr(settings, 'ai_chat_enabled', False):
        last_admin = ChatMessage.query.filter_by(session_id=chat.id, sender='admin').order_by(
            ChatMessage.created_at.desc()
        ).first()
        admin_active = bool(last_admin and last_admin.created_at and
                            (utcnow() - last_admin.created_at).total_seconds() < 1800)
        if not admin_active:
            ai_text = ai_help_reply(message, settings)
            ai_msg = ChatMessage(session_id=chat.id, sender='ai', message=ai_text)
            db.session.add(ai_msg)
            db.session.flush()  # assigns ai_msg.id so the browser can de-duplicate it when polling
            ai_payload = {
                'id': ai_msg.id,
                'sender': 'ai',
                'message': ai_msg.message,
                'created_at': to_addis(ai_msg.created_at or utcnow()).strftime('%H:%M')
            }
    chat.updated_at = utcnow()
    db.session.commit()

    return jsonify({
        'success': True,
        'session_key': session_key,
        'message': {
            'id': msg.id,
            'sender': 'visitor',
            'message': msg.message,
            'created_at': (to_addis(msg.created_at).strftime('%H:%M') if msg.created_at else '')
        },
        'ai_message': ai_payload
    })


# ---------------------------------------------------------------------------
# Customer auth (register / login) — works on any device via server session
# ---------------------------------------------------------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if get_current_customer():
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()[:120]
        raw_phone = request.form.get('phone', '').strip()[:30]
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if rate_exceeded('register', 20, 3600):
            flash(_('Too many requests. Please try again later.'), 'error')
            return redirect(url_for('register'))
        if not raw_phone or not password:
            flash(_('Phone number and password are required'), 'error')
            return redirect(url_for('register'))
        phone = normalize_phone(raw_phone)
        if not phone:
            flash(_('Please enter a valid Ethiopian phone number (e.g. 0911 234 567)'), 'error')
            return redirect(url_for('register'))
        if password != password2:
            flash(_('Passwords do not match'), 'error')
            return redirect(url_for('register'))
        ok, msg = validate_customer_password(password)
        if not ok:
            flash(msg, 'error')
            return redirect(url_for('register'))
        if Customer.query.filter_by(phone=phone).first():
            flash(_('Could not create account. Try logging in, or use a different phone number.'), 'error')
            return redirect(url_for('register'))

        if otp_enabled():
            # Store pending registration in session and send OTP.
            # Keep only the password hash in the session (never the plain password).
            session['pending_register'] = {
                'name': name,
                'phone': phone,
                'password_hash': generate_password_hash(password),
            }
            ok_sms, err = create_and_send_otp(phone, purpose='register')
            if not ok_sms:
                session.pop('pending_register', None)
                flash(err or _('Could not send verification code'), 'error')
                return redirect(url_for('register'))
            flash(_('We sent a verification code to %(phone)s', phone=phone), 'success')
            return redirect(url_for('register_verify'))

        # OTP disabled — create account immediately
        phone_digits = phone.replace('+', '')
        email_placeholder = f'{phone_digits}@phone.local'
        c = Customer(name=name, email=email_placeholder, phone=phone)
        c.set_password(password)
        db.session.add(c)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(_('Could not create account. Try logging in, or use a different phone number.'), 'error')
            return redirect(url_for('register'))
        start_fresh_session(customer_id=c.id)
        session.permanent = True
        attach_guest_orders_to_customer(c)
        flash(_('Account created successfully'), 'success')
        return redirect(url_for('index'))
    return render_template('auth/register.html', otp_on=otp_enabled())


@app.route('/register/verify', methods=['GET', 'POST'])
def register_verify():
    if get_current_customer():
        return redirect(url_for('index'))
    pending = session.get('pending_register')
    if not pending or not pending.get('phone'):
        flash(_('Please start registration again.'), 'error')
        return redirect(url_for('register'))

    phone = pending['phone']
    if request.method == 'POST':
        action = request.form.get('action') or 'verify'
        if action == 'resend':
            ok_sms, err = create_and_send_otp(phone, purpose='register')
            if ok_sms:
                flash(_('A new code was sent to %(phone)s', phone=phone), 'success')
            else:
                flash(err or _('Could not resend code'), 'error')
            return redirect(url_for('register_verify'))

        code = request.form.get('code', '').strip()
        ok, err = verify_otp(phone, code, purpose='register')
        if not ok:
            flash(err, 'error')
            return redirect(url_for('register_verify'))

        # Create account
        if Customer.query.filter_by(phone=phone).first():
            session.pop('pending_register', None)
            flash(_('Could not create account. Try logging in, or use a different phone number.'), 'error')
            return redirect(url_for('customer_login'))
        phone_digits = phone.replace('+', '')
        email_placeholder = f'{phone_digits}@phone.local'
        c = Customer(name=(pending.get('name') or '')[:120],
                     email=email_placeholder, phone=phone)
        if pending.get('password_hash'):
            c.password_hash = pending['password_hash']
        else:
            c.set_password(secrets.token_urlsafe(12))
        db.session.add(c)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            session.pop('pending_register', None)
            flash(_('Could not create account. Try logging in, or use a different phone number.'), 'error')
            return redirect(url_for('customer_login'))
        session.pop('pending_register', None)
        start_fresh_session(customer_id=c.id)
        session.permanent = True
        attach_guest_orders_to_customer(c)
        flash(_('Phone verified. Account created successfully'), 'success')
        return redirect(url_for('index'))

    return render_template('auth/verify_otp.html', phone=phone, purpose='register')


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if get_current_customer():
        return redirect(url_for('index'))
    if not otp_enabled():
        flash(_('Password reset by SMS is not available right now. Contact support.'), 'error')
        return redirect(url_for('customer_login'))
    if request.method == 'POST':
        raw_phone = request.form.get('phone', '').strip()[:30]
        phone = normalize_phone(raw_phone)
        if not phone:
            flash(_('Please enter a valid Ethiopian phone number (e.g. 0911 234 567)'), 'error')
            return redirect(url_for('forgot_password'))
        if rate_exceeded(f'reset:{client_ip()}', 10, 3600):
            flash(_('Too many requests. Please try again later.'), 'error')
            return redirect(url_for('forgot_password'))
        # Always look the same whether the phone exists or not (anti-enumeration)
        exists = Customer.query.filter_by(phone=phone).first() is not None
        if exists:
            ok_sms, err = create_and_send_otp(phone, purpose='reset')
            if not ok_sms:
                flash(err or _('Could not send verification code'), 'error')
                return redirect(url_for('forgot_password'))
        session['pending_reset'] = {'phone': phone}
        flash(_('If this number is registered, a reset code was sent.'), 'success')
        return redirect(url_for('reset_password'))
    return render_template('auth/forgot_password.html')


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if get_current_customer():
        return redirect(url_for('index'))
    pending = session.get('pending_reset') or {}
    phone = pending.get('phone')
    if not phone:
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        action = request.form.get('action') or 'reset'
        if action == 'resend':
            if Customer.query.filter_by(phone=phone).first():
                ok_sms, err = create_and_send_otp(phone, purpose='reset')
                if ok_sms:
                    flash(_('A new code was sent to %(phone)s', phone=phone), 'success')
                else:
                    flash(err or _('Could not resend code'), 'error')
            else:
                flash(_('A new code was sent to %(phone)s', phone=phone), 'success')
            return redirect(url_for('reset_password'))

        code = request.form.get('code', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        # Validate password BEFORE consuming the OTP
        if password != password2:
            flash(_('Passwords do not match'), 'error')
            return redirect(url_for('reset_password'))
        ok_pw, msg = validate_customer_password(password)
        if not ok_pw:
            flash(msg, 'error')
            return redirect(url_for('reset_password'))
        ok, err = verify_otp(phone, code, purpose='reset')
        if not ok:
            flash(err, 'error')
            return redirect(url_for('reset_password'))
        c = Customer.query.filter_by(phone=phone).first()
        if not c:
            session.pop('pending_reset', None)
            flash(_('Account not found'), 'error')
            return redirect(url_for('register'))
        c.set_password(password)
        db.session.commit()
        session.pop('pending_reset', None)
        flash(_('Password updated. You can log in now.'), 'success')
        return redirect(url_for('customer_login'))
    return render_template('auth/reset_password.html', phone=phone)


@app.route('/login', methods=['GET', 'POST'])
def customer_login():
    if get_current_customer():
        return redirect(url_for('index'))
    if request.method == 'POST':
        raw_phone = request.form.get('phone', '').strip()[:30]
        password = request.form.get('password', '')
        phone = normalize_phone(raw_phone) or raw_phone  # try normalized, fall back to raw for old accounts
        if login_blocked('customer', phone):
            flash(_('Too many failed attempts. Please try again in 15 minutes.'), 'error')
            return render_template('auth/login.html'), 429
        # Try normalized first, then original input (for accounts created before normalization)
        c = Customer.query.filter_by(phone=phone, is_active=True).first()
        if not c and phone != raw_phone:
            c = Customer.query.filter_by(phone=raw_phone, is_active=True).first()
        if c and c.check_password(password):
            login_succeeded('customer', c.phone)
            # Optionally upgrade old phone format to normalized
            if c.phone != phone and normalize_phone(c.phone) == phone:
                c.phone = phone
                db.session.commit()
            attach_guest_orders_to_customer(c)
            start_fresh_session(customer_id=c.id)
            session.permanent = True
            flash(_('Logged in successfully'), 'success')
            nxt = request.args.get('next') or request.form.get('next')
            if nxt and is_safe_path(nxt):
                return redirect(nxt)
            return redirect(url_for('index'))
        login_failed('customer', phone)
        flash(_('Invalid phone or password'), 'error')
    return render_template('auth/login.html')


@app.route('/logout', methods=['POST'])
def customer_logout():
    session.pop('customer_id', None)
    session.pop('pending_orders', None)  # Clear pending orders so next user on shared device cannot see them
    flash(_('Logged out'), 'success')
    return redirect(url_for('index'))


@app.route('/account', methods=['GET', 'POST'])
def account():
    c = get_current_customer()
    if not c:
        return redirect(url_for('customer_login'))
    if request.method == 'POST':
        action = request.form.get('action') or ''
        if action == 'update_profile':
            name = (request.form.get('name') or '').strip()[:120]
            if name:
                c.name = name
                db.session.commit()
                flash(_('Profile updated'), 'success')
            else:
                flash(_('Name is required'), 'error')
            return redirect(url_for('account'))
        if action == 'change_password':
            current = request.form.get('current_password') or ''
            new_pw = request.form.get('new_password') or ''
            new_pw2 = request.form.get('new_password2') or ''
            if not c.check_password(current):
                flash(_('Current password is incorrect'), 'error')
                return redirect(url_for('account'))
            if new_pw != new_pw2:
                flash(_('Passwords do not match'), 'error')
                return redirect(url_for('account'))
            ok, msg = validate_customer_password(new_pw)
            if not ok:
                flash(msg, 'error')
                return redirect(url_for('account'))
            c.set_password(new_pw)
            db.session.commit()
            flash(_('Password changed'), 'success')
            return redirect(url_for('account'))
    orders = Order.query.filter_by(customer_id=c.id).order_by(Order.created_at.desc()).limit(20).all()
    return render_template('auth/account.html', customer=c, orders=orders)


# ---------------------------------------------------------------------------
# Transporter portal
# ---------------------------------------------------------------------------

@app.route('/order/<order_number>/accept-cod', methods=['POST'])
def accept_cod(order_number):
    """Customer accepts delivery fee for COD orders (over PREPAY_MAX)."""
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    if not _can_view_order(order):
        abort(403)
    if order.payment_status != 'awaiting_customer':
        flash(_('Nothing to confirm on this order'), 'error')
        return redirect(url_for('order_success', order_number=order_number))
    order.payment_status = 'not_required'
    order.payment_note = _('Customer accepted delivery fee — pay on delivery')
    if order.status == 'pending':
        change_order_status(order, 'confirmed', actor='customer', note='COD accepted')
    db.session.commit()
    flash(_('Thank you. Pay when your order is delivered.'), 'success')
    return redirect(url_for('order_success', order_number=order_number))


@app.route('/order/<order_number>/resubmit-payment', methods=['POST'])
def resubmit_payment(order_number):
    """Customer uploads / re-uploads receipt after admin requested payment."""
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    if not _can_view_order(order):
        abort(403)
    if not order.payment_required:
        flash(_('This order does not require bank payment'), 'error')
        return redirect(url_for('order_success', order_number=order_number))
    if order.payment_status not in ('rejected', 'pending'):
        flash(_('Payment can only be submitted when payment was requested'), 'error')
        return redirect(url_for('order_success', order_number=order_number))
    if order.status in ('cancelled', 'delivered'):
        flash(_('This order is already closed'), 'error')
        return redirect(url_for('order_success', order_number=order_number))

    transaction_id = ' '.join((request.form.get('transaction_id') or '').split()).upper()[:120]
    bank_id = safe_int(request.form.get('bank_account_id'), 0)
    bank = db.session.get(BankAccount, bank_id) if bank_id else None
    if bank and bank.is_active:
        order.bank_account_id = bank.id
        order.bank_name_snapshot = bank.bank_name
        order.bank_account_name_snapshot = bank.account_name
        order.bank_account_number_snapshot = bank.account_number
    upload = request.files.get('receipt')
    new_path = ''
    if upload and upload.filename:
        new_path = save_image(upload, 'receipt', private=True)
        if not new_path:
            flash(_('The receipt must be a valid JPG, PNG, GIF or WEBP image'), 'error')
            return redirect(url_for('order_success', order_number=order_number))
    if not transaction_id and not new_path and not order.receipt_path:
        flash(_('Send transaction ID or receipt photo'), 'error')
        return redirect(url_for('order_success', order_number=order_number))
    if transaction_id:
        # duplicate check among other active orders
        dup = (Order.query.filter(
            Order.transaction_id == transaction_id,
            Order.id != order.id,
            Order.status != 'cancelled',
            Order.payment_status.in_(['pending', 'confirmed']),
        ).first())
        if dup:
            flash(_('This transaction ID was already used on order %(n)s', n=dup.order_number), 'error')
            return redirect(url_for('order_success', order_number=order_number))
        order.transaction_id = transaction_id
    if new_path:
        order.receipt_path = new_path
    order.payment_status = 'pending'
    order.payment_note = _('New payment proof submitted — waiting for admin review')
    try:
        log_order_change(order, 'payment_status', 'rejected', 'pending', actor='customer', note='resubmit')
    except Exception:
        pass
    db.session.commit()
    flash(_('Payment proof sent. We will review it shortly.'), 'success')
    return redirect(url_for('order_success', order_number=order_number))


@app.route('/transporter/login', methods=['GET', 'POST'])
def transporter_login():
    if get_current_transporter():
        return redirect(url_for('transporter_dashboard'))
    if request.method == 'POST':
        raw_phone = request.form.get('phone', '').strip()[:30]
        password = request.form.get('password', '')
        phone = normalize_phone(raw_phone) or raw_phone
        if login_blocked('transporter', phone):
            flash(_('Too many failed attempts. Please try again in 15 minutes.'), 'error')
            return render_template('transporter/login.html'), 429
        t = Transporter.query.filter_by(phone=phone, is_active=True).first()
        if not t and phone != raw_phone:
            t = Transporter.query.filter_by(phone=raw_phone, is_active=True).first()
        if t and t.check_password(password):
            login_succeeded('transporter', t.phone)
            start_fresh_session(transporter_id=t.id)
            session.permanent = True
            return redirect(url_for('transporter_dashboard'))
        login_failed('transporter', phone)
        flash(_('Invalid phone or password'), 'error')
    return render_template('transporter/login.html')


@app.route('/transporter/logout', methods=['POST'])
def transporter_logout():
    session.pop('transporter_id', None)
    return redirect(url_for('transporter_login'))


@app.route('/transporter')
def transporter_dashboard():
    t = get_current_transporter()
    if not t:
        return redirect(url_for('transporter_login'))
    show = (request.args.get('show') or 'active').strip()
    q = Order.query.filter_by(transporter_id=t.id)
    if show == 'done':
        q = q.filter(Order.status.in_(['delivered', 'cancelled']))
    else:
        q = q.filter(Order.status.notin_(['delivered', 'cancelled']))
        show = 'active'
    orders = q.order_by(Order.assigned_at.desc().nullslast(), Order.created_at.desc()).limit(100).all()
    return render_template('transporter/dashboard.html', transporter=t, orders=orders, show=show)


@app.route('/transporter/order/<int:oid>')
def transporter_order(oid):
    t = get_current_transporter()
    if not t:
        return redirect(url_for('transporter_login'))
    order = db.get_or_404(Order, oid)
    if order.transporter_id != t.id:
        flash(_('Not assigned to you'), 'error')
        return redirect(url_for('transporter_dashboard'))
    return render_template('transporter/order_detail.html', order=order, transporter=t)


@app.route('/transporter/order/<int:oid>/status', methods=['POST'])
def transporter_order_status(oid):
    t = get_current_transporter()
    if not t:
        return redirect(url_for('transporter_login'))
    order = db.get_or_404(Order, oid)
    if order.transporter_id != t.id:
        abort(403)
    new_status = request.form.get('status')
    if new_status not in ('out_for_delivery', 'delivered'):
        flash(_('Invalid status'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    if order.status in ('cancelled', 'delivered'):
        flash(_('This order is already closed'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    if new_status == 'delivered' and order.total_amount > PREPAY_MAX and not order.delivery_receipt_path:
        flash(_('Upload COD receipt before marking delivered'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    actor = f'transporter:{(t.phone or t.name or str(t.id))}'
    err = change_order_status(order, new_status, actor=actor)
    if err:
        flash(err, 'error')
        return redirect(url_for('transporter_order', oid=oid))
    db.session.commit()
    flash(_('Status updated'), 'success')
    return redirect(url_for('transporter_order', oid=oid))



@app.route('/transporter/password', methods=['GET', 'POST'])
def transporter_change_password():
    t = get_current_transporter()
    if not t:
        return redirect(url_for('transporter_login'))
    if request.method == 'POST':
        current = request.form.get('current_password') or ''
        new_pw = request.form.get('new_password') or ''
        new_pw2 = request.form.get('new_password2') or ''
        if not t.check_password(current):
            flash(_('Current password is incorrect'), 'error')
            return redirect(url_for('transporter_change_password'))
        if len(new_pw) < 6:
            flash(_('Password must be at least 6 characters'), 'error')
            return redirect(url_for('transporter_change_password'))
        if new_pw != new_pw2:
            flash(_('Passwords do not match'), 'error')
            return redirect(url_for('transporter_change_password'))
        t.set_password(new_pw)
        db.session.commit()
        flash(_('Password changed'), 'success')
        return redirect(url_for('transporter_dashboard'))
    return render_template('transporter/password.html', transporter=t)


@app.route('/transporter/order/<int:oid>/delivery-receipt', methods=['POST'])
def transporter_delivery_receipt(oid):
    """For COD orders (>100,000 ETB): transporter uploads receipt after delivery."""
    t = get_current_transporter()
    if not t:
        return redirect(url_for('transporter_login'))
    order = db.get_or_404(Order, oid)
    if order.transporter_id != t.id:
        abort(403)
    if order.total_amount <= PREPAY_MAX:
        flash(_('Receipt upload is only for orders over 100,000 ETB'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    if order.status in ('cancelled', 'delivered'):
        flash(_('This order is already closed — receipt cannot be changed'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    # COD receipt only when already out for delivery (avoids stuck photo + failed status)
    if order.status != 'out_for_delivery':
        flash(_('Mark the order out for delivery before uploading the COD receipt'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    if order.delivery_receipt_path:
        flash(_('A receipt was already uploaded. Contact admin to change it.'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    if 'receipt' not in request.files or not request.files['receipt'].filename:
        flash(_('Please choose a receipt photo'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    raw_amt = (request.form.get('amount_collected') or '').strip()
    if not raw_amt:
        flash(_('Please enter the amount collected'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    amt = safe_float(raw_amt, None)
    if amt is None or not math.isfinite(amt) or amt <= 0 or amt > 1e10:
        flash(_('Please enter a valid amount collected'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    expected = float(order.total_amount or 0)
    note_extra = ''
    if expected > 0 and abs(amt - expected) > max(1.0, expected * 0.02):
        # Allow shortfall/overage but flag clearly for admin
        note_extra = f' [Amount differs: collected {amt:.0f} vs order {expected:.0f}]'
    path_saved = save_image(request.files['receipt'], 'cod_receipt', private=True)
    if not path_saved:
        flash(_('Could not save receipt. Use a JPG, PNG, GIF or WEBP photo.'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    # Save receipt only — do NOT auto-mark delivered (admin reviews amount / photo)
    order.delivery_receipt_path = path_saved
    note = (request.form.get('note', '') or '').strip()[:200]
    order.delivery_receipt_note = (note + note_extra).strip()[:255]
    order.cod_amount_collected = amt
    order.delivery_receipt_at = utcnow()
    try:
        log_order_change(order, 'cod_receipt', '', f'collected={amt:.0f}', actor=f'transporter:{(t.phone or t.name or str(t.id))}', note=note_extra or 'COD receipt uploaded')
    except Exception:
        pass
    db.session.commit()
    if note_extra:
        flash(_('Receipt sent. Amount differs from total — admin will review. Then mark delivered.'), 'success')
    else:
        flash(_('Receipt sent to admin ✓ You can now mark delivered.'), 'success')
    return redirect(url_for('transporter_order', oid=oid))


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    # Always require credentials — if already logged in as admin, still show form
    # unless they just logged in (POST). Non-admin sessions are cleared first.
    if request.method == 'GET':
        if current_user.is_authenticated and not isinstance(current_user, Admin):
            logout_user()
            session.clear()
    if request.method == 'POST':
        # Support both new field names (admin_username) and legacy (username)
        username = (
            request.form.get('admin_username')
            or request.form.get('username')
            or ''
        ).strip()[:80]
        password = request.form.get('admin_password') or request.form.get('password') or ''
        if login_blocked('admin', username):
            flash(_('Too many failed attempts. Please try again in 15 minutes.'), 'error')
            return render_template('admin/login.html'), 429
        admin = Admin.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            login_succeeded('admin', username)
            nxt = request.args.get('next') or request.form.get('next')
            start_fresh_session()
            login_user(admin, remember=False)
            flash(_('Logged in'), 'success')
            return redirect(nxt if is_safe_path(nxt) else url_for('admin_dashboard'))
        login_failed('admin', username)
        flash(_('Invalid username or password'), 'error')
    return render_template('admin/login.html')


@app.route('/admin/logout', methods=['POST'])
@login_required
def admin_logout():
    logout_user()
    session.clear()
    flash(_('Logged out — please sign in again'), 'success')
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    try:
        release_stale_unpaid_orders()
    except Exception:
        db.session.rollback()
    total_orders = Order.query.count()
    pending_orders = Order.query.filter_by(status='pending').count()
    total_products = Product.query.count()
    open_supports = SupportRequest.query.filter_by(status='open').count()
    total_transporters = Transporter.query.filter_by(is_active=True).count()
    unassigned_orders = Order.query.filter(
        Order.transporter_id.is_(None),
        Order.status.in_(['pending', 'confirmed', 'preparing', 'out_for_delivery'])
    ).count()
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()
    return render_template('admin/dashboard.html',
                           total_orders=total_orders,
                           pending_orders=pending_orders,
                           total_products=total_products,
                           open_supports=open_supports,
                           total_transporters=total_transporters,
                           unassigned_orders=unassigned_orders,
                           recent_orders=recent_orders)


@app.route('/admin/api/live')
@admin_required
def admin_api_live():
    """Lightweight fingerprint for admin list pages (poll every 5s).

    Returns a short hash of counts + latest ids so the browser only reloads
    when something actually changed.
    """
    import hashlib
    page = (request.args.get('page') or '').strip()
    parts = []

    if page == 'admin_orders':
        status = (request.args.get('status') or '').strip()
        q = (request.args.get('q') or '').strip()[:80]
        query = Order.query
        if status:
            query = query.filter_by(status=status)
        if q:
            like = f'%{q}%'
            query = query.filter(or_(
                Order.order_number.ilike(like),
                Order.phone.ilike(like),
                Order.customer_name.ilike(like),
                Order.transaction_id.ilike(like),
            ))
        total = query.count()
        latest = query.order_by(Order.updated_at.desc(), Order.id.desc()).limit(5).all()
        parts.append(f'o:{total}')
        for o in latest:
            parts.append(f'{o.id}:{o.status}:{o.payment_status}:{o.updated_at}')
    elif page == 'admin_support':
        open_req = SupportRequest.query.filter_by(status='open').count()
        chats = ChatSession.query.count()
        open_chats = ChatSession.query.filter_by(status='open').count()
        last_chat = ChatSession.query.order_by(ChatSession.updated_at.desc()).first()
        last_req = SupportRequest.query.order_by(SupportRequest.created_at.desc()).first()
        parts.append(f'sr:{open_req}:c:{chats}:oc:{open_chats}')
        if last_chat:
            parts.append(f'ch:{last_chat.id}:{last_chat.updated_at}:{last_chat.status}')
        if last_req:
            parts.append(f'rq:{last_req.id}:{last_req.status}:{last_req.created_at}')
    elif page == 'admin_customers':
        total = Customer.query.count()
        latest = Customer.query.order_by(Customer.created_at.desc()).limit(5).all()
        parts.append(f'cu:{total}')
        for c in latest:
            parts.append(f'{c.id}:{c.is_active}:{c.created_at}')
    else:
        # dashboard default
        total_orders = Order.query.count()
        pending = Order.query.filter_by(status='pending').count()
        open_sup = SupportRequest.query.filter_by(status='open').count()
        open_chats = ChatSession.query.filter_by(status='open').count()
        last_order = Order.query.order_by(Order.updated_at.desc()).first()
        parts.append(f'd:{total_orders}:{pending}:{open_sup}:{open_chats}')
        if last_order:
            parts.append(f'lo:{last_order.id}:{last_order.status}:{last_order.updated_at}')

    raw = '|'.join(str(p) for p in parts)
    fp = hashlib.sha256(raw.encode('utf-8', errors='ignore')).hexdigest()[:20]
    return jsonify({'fp': fp, 'page': page or 'admin_dashboard'})



@app.route('/admin/categories', methods=['GET', 'POST'])
@admin_required
def admin_categories():
    if request.method == 'POST':
        action = request.form.get('action') or 'add'
        if action == 'add':
            name_am = (request.form.get('name_am') or '').strip()[:120]
            name_en = (request.form.get('name_en') or '').strip()[:120]
            if not name_am or not name_en:
                flash(_('Category names are required'), 'error')
                return redirect(url_for('admin_categories'))
            db.session.add(Category(
                name_am=name_am,
                name_en=name_en,
                sort_order=form_int('sort_order', 0, -10000, 10000),
                is_active=bool(request.form.get('is_active', 1)),
            ))
            db.session.commit()
            flash(_('Category added'), 'success')
        elif action == 'edit':
            cat = db.get_or_404(Category, form_int('id', 0, 0, 10**9))
            cat.name_am = (request.form.get('name_am') or '').strip()[:120] or cat.name_am
            cat.name_en = (request.form.get('name_en') or '').strip()[:120] or cat.name_en
            cat.sort_order = form_int('sort_order', cat.sort_order or 0, -10000, 10000)
            cat.is_active = bool(request.form.get('is_active'))
            db.session.commit()
            flash(_('Category updated'), 'success')
        elif action == 'delete':
            cat = db.get_or_404(Category, form_int('id', 0, 0, 10**9))
            if Product.query.filter_by(category_id=cat.id).first():
                # Keep products: just deactivate category
                cat.is_active = False
                db.session.commit()
                flash(_('Category has products — deactivated instead of deleted'), 'success')
            else:
                db.session.delete(cat)
                db.session.commit()
                flash(_('Category deleted'), 'success')
        return redirect(url_for('admin_categories'))
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    return render_template('admin/categories.html', categories=categories)


@app.route('/admin/products')
@admin_required
def admin_products():
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    cat_id = form_int('category', 0, 0, 10**9) or None
    q = Product.query
    if cat_id:
        q = q.filter_by(category_id=cat_id)
    products = q.order_by(Product.sort_order, Product.id).all()
    return render_template('admin/products.html', products=products, categories=categories)


def _read_delivery_hours():
    d_min = form_int('delivery_min_hours', 2, 0, 720)
    d_max = form_int('delivery_max_hours', 6, 0, 720)
    if d_max < d_min:
        d_max = d_min
    return d_min, d_max


def _save_product_image(product):
    upload = request.files.get('image')
    if upload and upload.filename:
        img_path = save_image(upload, 'product')
        if img_path:
            old = product.image_path
            product.image_path = img_path
            if old and old != img_path:
                delete_stored_file(old)
        else:
            flash(_('The image could not be saved. Use a JPG, PNG, GIF or WEBP file.'), 'error')


def _read_product_unit():
    """Validate unit from form; default kg."""
    u = (request.form.get('unit') or 'kg').strip()
    return u if u in ALLOWED_UNITS else 'kg'


def _read_product_vat_percent():
    """VAT % on listed price. 0 = exempt. Max 100."""
    if request.form.get('vat_exempt'):
        return 0.0
    return form_float('vat_percent', 0.0, 0.0, 100.0)


@app.route('/admin/products/add', methods=['GET', 'POST'])
@admin_required
def admin_product_add():
    if request.method == 'POST':
        name_am = request.form.get('name_am', '').strip()[:120]
        name_en = request.form.get('name_en', '').strip()[:120]
        if not name_am or not name_en:
            flash(_('Product names are required'), 'error')
            return redirect(url_for('admin_product_add'))

        d_min, d_max = _read_delivery_hours()
        cat_id = form_int('category_id', 0, 0, 10**9) or None
        if cat_id and not db.session.get(Category, cat_id):
            cat_id = None
        unit = _read_product_unit()
        product = Product(
            category_id=cat_id,
            name_am=name_am,
            name_en=name_en,
            description_am=request.form.get('description_am', ''),
            description_en=request.form.get('description_en', ''),
            unit=unit,
            vat_percent=_read_product_vat_percent(),
            is_active=bool(request.form.get('is_active')),
            sort_order=form_int('sort_order', 0, -10000, 10000),
            delivery_min_hours=d_min,
            delivery_max_hours=d_max
        )
        _save_product_image(product)

        db.session.add(product)
        db.session.flush()

        for w in sizes_for_unit(unit):
            is_avail = bool(request.form.get(f'available_{w}'))
            price = form_float(f'price_{w}', 0.0, 0.0, 1e8)
            if is_avail and price <= 0:
                flash(_('Available sizes must have a price greater than 0'), 'error')
                return redirect(url_for('admin_product_add'))
            bulk = form_float_or_none(f'bulk_price_{w}', 0.0, 1e8)
            if bulk is not None and (bulk <= 0 or bulk > price):
                flash(_('Bulk price must be > 0 and less than or equal to regular price'), 'error')
                return redirect(url_for('admin_product_add'))
            variant = ProductVariant(
                product_id=product.id,
                weight_kg=w,
                price=price,
                bulk_price=bulk,
                bulk_min_qty=form_int(f'bulk_min_{w}', 10, 1, 100000),
                stock=form_int(f'stock_{w}', 100, 0, 10000000),
                is_available=is_avail
            )
            db.session.add(variant)

        db.session.commit()
        flash(_('Product added successfully'), 'success')
        return redirect(url_for('admin_products'))

    # Pre-select category when coming from Categories page (?category=ID)
    preselect_cat = form_int('category', 0, 0, 10**9) or None
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    return render_template(
        'admin/product_form.html',
        product=None,
        weights=WEIGHTS,
        unit_presets=UNIT_PRESETS,
        unit_labels=UNIT_LABELS,
        allowed_units=ALLOWED_UNITS,
        categories=categories,
        preselect_category=preselect_cat
    )


@app.route('/admin/products/<int:pid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_product_edit(pid):
    product = db.get_or_404(Product, pid)
    if request.method == 'POST':
        name_am = request.form.get('name_am', '').strip()[:120]
        name_en = request.form.get('name_en', '').strip()[:120]
        if not name_am or not name_en:
            flash(_('Product names are required'), 'error')
            return redirect(url_for('admin_product_edit', pid=pid))
        d_min, d_max = _read_delivery_hours()
        cat_id = form_int('category_id', 0, 0, 10**9) or None
        if cat_id and not db.session.get(Category, cat_id):
            cat_id = None
        unit = _read_product_unit()
        product.category_id = cat_id
        product.name_am = name_am
        product.name_en = name_en
        product.description_am = request.form.get('description_am', '')
        product.description_en = request.form.get('description_en', '')
        product.unit = unit
        product.vat_percent = _read_product_vat_percent()
        product.is_active = bool(request.form.get('is_active'))
        product.sort_order = form_int('sort_order', 0, -10000, 10000)
        product.delivery_min_hours = d_min
        product.delivery_max_hours = d_max
        _save_product_image(product)

        existing = {v.weight_kg: v for v in product.variants}
        sizes = sizes_for_unit(unit)
        # Also keep any existing sizes that are not in the new preset (so data is not lost)
        for w in existing:
            if w not in sizes:
                sizes.append(w)
        sizes = sorted(set(sizes))

        for w in sizes:
            is_avail = bool(request.form.get(f'available_{w}'))
            price = form_float(f'price_{w}', 0.0, 0.0, 1e8)
            if is_avail and price <= 0:
                flash(_('Available sizes must have a price greater than 0'), 'error')
                return redirect(url_for('admin_product_edit', pid=pid))
            bulk = form_float_or_none(f'bulk_price_{w}', 0.0, 1e8)
            if bulk is not None and (bulk <= 0 or bulk > price):
                flash(_('Bulk price must be > 0 and less than or equal to regular price'), 'error')
                return redirect(url_for('admin_product_edit', pid=pid))
            variant = existing.get(w)
            if variant is None:
                variant = ProductVariant(product_id=product.id, weight_kg=w)
                db.session.add(variant)
            variant.price = price
            variant.bulk_price = bulk
            variant.bulk_min_qty = form_int(f'bulk_min_{w}', 10, 1, 100000)
            if request.form.get(f'update_stock_{w}') and f'stock_{w}' in request.form:
                variant.stock = form_int(f'stock_{w}', 0, 0, 10000000)
            elif variant.id is None:
                variant.stock = form_int(f'stock_{w}', 100, 0, 10000000)
            variant.is_available = is_avail

        db.session.commit()
        flash(_('Product updated successfully'), 'success')
        return redirect(url_for('admin_products'))

    # Sizes to show: preset for current unit + any extra sizes already on the product
    unit = product.unit or 'kg'
    sizes = sizes_for_unit(unit)
    for v in product.variants:
        if v.weight_kg not in sizes:
            sizes.append(v.weight_kg)
    sizes = sorted(set(sizes))
    return render_template(
        'admin/product_form.html',
        product=product,
        weights=sizes,
        unit_presets=UNIT_PRESETS,
        unit_labels=UNIT_LABELS,
        allowed_units=ALLOWED_UNITS,
        categories=Category.query.order_by(Category.sort_order, Category.id).all()
    )


@app.route('/admin/products/<int:pid>/delete', methods=['POST'])
@admin_required
def admin_product_delete(pid):
    product = db.get_or_404(Product, pid)
    product.is_active = False
    db.session.commit()
    flash(_('Product hidden (soft-deleted)'), 'success')
    return redirect(url_for('admin_products'))


@app.route('/admin/orders')
@admin_required
def admin_orders():
    status = request.args.get('status', '').strip()
    q = (request.args.get('q') or '').strip()[:80]
    query = Order.query.order_by(Order.created_at.desc())
    if status:
        query = query.filter_by(status=status)
    if q:
        like = f'%{q}%'
        query = query.filter(or_(
            Order.order_number.ilike(like),
            Order.phone.ilike(like),
            Order.customer_name.ilike(like),
            Order.transaction_id.ilike(like),
        ))
    orders = query.limit(300).all()
    return render_template('admin/orders.html', orders=orders, current_status=status, search_q=q)


def _parse_hours(raw):
    """Return (value, ok). Blank -> (None, True); invalid -> (None, False)."""
    raw = (raw or '').strip()
    if not raw:
        return None, True
    v = safe_int(raw, None)
    if v is None or v < 0 or v > 720:
        return None, False
    return v, True


@app.route('/admin/orders/<int:oid>', methods=['GET', 'POST'])
@admin_required
def admin_order_detail(oid):
    order = db.get_or_404(Order, oid)
    bank = db.session.get(BankAccount, order.bank_account_id) if order.bank_account_id else None
    transporters = Transporter.query.filter_by(is_active=True).order_by(Transporter.name).all()
    assigned = db.session.get(Transporter, order.transporter_id) if order.transporter_id else None
    if request.method == 'POST':
        action = request.form.get('action', 'status')
        back = redirect(url_for('admin_order_detail', oid=oid))
        if action == 'set_delivery':
            if order.status in ('cancelled', 'delivered'):
                flash(_('Cannot change delivery fee on a closed order'), 'error')
                return back
            fee = form_float('delivery_fee', 0.0, 0.0, 1e7)
            items = float(order.items_subtotal or 0)
            if items <= 0:
                items = float(order.total_amount or 0)
                order.items_subtotal = items
            order.delivery_fee = fee
            order.total_amount = items + fee
            if order.payment_status in ('awaiting_quote', 'awaiting_customer', ''):
                order.payment_note = _('Delivery fee set to %(n)s ETB — request payment when ready', n=f'{fee:.0f}')
            log_order_change(order, 'delivery_fee', None, fee, actor='admin', note='set delivery fee')
            db.session.commit()
            flash(_('Delivery fee saved: %(n)s ETB. Grand total %(t)s ETB.',
                    n=f'{fee:.0f}', t=f'{order.total_amount:.0f}'), 'success')
            return back

        if action == 'request_payment':
            if order.status in ('cancelled', 'delivered'):
                flash(_('Order is closed'), 'error')
                return back
            fee = form_float('delivery_fee', float(order.delivery_fee or 0), 0.0, 1e7)
            items = float(order.items_subtotal or 0)
            if items <= 0:
                items = max(0.0, float(order.total_amount or 0) - float(order.delivery_fee or 0))
                order.items_subtotal = items
            order.delivery_fee = fee
            order.total_amount = items + fee
            grand = order.total_amount
            if grand < PREPAY_MIN:
                # Small order — no bank deposit; mark ready
                order.payment_required = False
                order.payment_status = 'not_required'
                order.payment_note = _('No bank payment required (under %(n)s ETB). Ready to process.', n=PREPAY_MIN)
                if order.status == 'pending':
                    change_order_status(order, 'confirmed', actor='admin', note='small order auto-confirm')
            elif grand <= PREPAY_MAX:
                order.payment_required = True
                order.payment_status = 'pending'
                order.payment_note = _(
                    'Please pay %(t)s ETB by bank (items %(i)s + delivery %(d)s). Upload receipt or transaction ID.',
                    t=f'{grand:.0f}', i=f'{items:.0f}', d=f'{fee:.0f}'
                )
            else:
                # Over 100,000 — COD: ask customer to accept delivery fee; pay on delivery
                order.payment_required = False
                order.payment_status = 'awaiting_customer'
                order.payment_note = _(
                    'Delivery fee is %(d)s ETB. Grand total %(t)s ETB. No bank transfer needed — pay when items are delivered. Please confirm on your order page.',
                    d=f'{fee:.0f}', t=f'{grand:.0f}'
                )
            log_order_change(order, 'payment_status', None, order.payment_status, actor='admin', note='request payment / customer notice')
            db.session.commit()
            flash(_('Customer payment request sent'), 'success')
            return back

        if action == 'assign':
            tid = (request.form.get('transporter_id') or '').strip()
            if tid:
                if order.status in ('cancelled', 'delivered'):
                    flash(_('This order is already closed'), 'error')
                    return back
                if order.payment_status == 'awaiting_quote':
                    flash(_('Set delivery fee and request payment first'), 'error')
                    return back
                if order.payment_required and order.payment_status not in ('confirmed', 'not_required'):
                    flash(_('Confirm payment before assigning transporter'), 'error')
                    return back
                if order.payment_status == 'awaiting_customer':
                    flash(_('Wait for customer to accept delivery fee (COD)'), 'error')
                    return back
                tr = db.session.get(Transporter, safe_int(tid, 0))
                if not tr or not tr.is_active:
                    flash(_('Transporter not found or inactive'), 'error')
                    return back
                order.transporter_id = tr.id
                order.assigned_at = utcnow()
                if order.status == 'pending':
                    err = change_order_status(order, 'confirmed', actor='admin', note='assigned transporter')
                    if err:
                        flash(err, 'error')
                        return back
            else:
                if order.status == 'delivered':
                    flash(_('This order is already closed'), 'error')
                    return back
                order.transporter_id = None
                order.assigned_at = None
            db.session.commit()
            flash(_('Order sent to transporter') if tid else _('Transporter removed from order'), 'success')
            return back
        if action == 'payment':
            pay = request.form.get('payment_status')
            note = request.form.get('payment_note', '').strip()[:1000]
            if pay in ['pending', 'confirmed', 'rejected']:
                if not order.payment_required and pay != 'confirmed':
                    flash(_('This order does not require bank payment'), 'error')
                    return back
                if order.status in ('cancelled', 'delivered'):
                    flash(_('Cannot change payment status on a closed order'), 'error')
                    return back
                old_pay = order.payment_status
                order.payment_status = pay
                if note:
                    order.payment_note = note
                elif pay == 'confirmed':
                    order.payment_note = _('Payment confirmed')
                elif pay == 'rejected':
                    order.payment_note = _('Payment rejected — please upload a new receipt or contact support')
                if pay == 'confirmed' and order.status == 'pending':
                    err = change_order_status(order, 'confirmed', actor='admin', note='payment confirmed')
                    if err:
                        flash(err, 'error')
                        return back
                if pay == 'rejected':
                    order.transaction_id = ''
                    # Keep old receipt_path for audit; customer resubmit will replace it
                try:
                    log_order_change(order, 'payment_status', old_pay, pay, actor='admin', note=note or '')
                except Exception:
                    pass
                db.session.commit()
                flash(_('Confirmed') if pay == 'confirmed' else _('Payment status updated'), 'success')
            return back
        if action == 'eta':
            dmin_i, ok1 = _parse_hours(request.form.get('delivery_min_hours'))
            dmax_i, ok2 = _parse_hours(request.form.get('delivery_max_hours'))
            if not (ok1 and ok2):
                flash(_('Invalid hours'), 'error')
                return back
            if dmin_i is not None and dmax_i is not None:
                if dmax_i < dmin_i:
                    dmin_i, dmax_i = dmax_i, dmin_i
                order.estimated_delivery = f"{dmin_i}-{dmax_i}"
            elif dmin_i is not None:
                order.estimated_delivery = str(dmin_i)
            elif dmax_i is not None:
                order.estimated_delivery = str(dmax_i)
            msg = request.form.get('customer_message', '').strip()[:1000]
            if msg:
                order.payment_note = msg  # visible to customer on cart / account / success
            db.session.commit()
            flash(_('Delivery time saved — customer will see it on Cart / order'), 'success')
            return back
        new_status = request.form.get('status')
        if new_status in ['pending', 'confirmed', 'preparing', 'out_for_delivery', 'delivered', 'cancelled']:
            actor = f'admin:{(current_user.username if current_user.is_authenticated else "?")}'
            err = change_order_status(order, new_status, actor=actor)
            if err:
                db.session.rollback()
                flash(err, 'error')
            else:
                db.session.commit()
                flash(_('Order status updated'), 'success')
        return back
    logs = StatusLog.query.filter_by(order_id=order.id).order_by(StatusLog.created_at.desc()).limit(50).all()
    return render_template('admin/order_detail.html', order=order, bank=bank, transporters=transporters, assigned=assigned, status_logs=logs)


@app.route('/admin/banks', methods=['GET', 'POST'])
@admin_required
def admin_banks():
    if request.method == 'POST':
        action = request.form.get('action', 'add')
        bank_name = request.form.get('bank_name', '').strip()[:120]
        account_name = request.form.get('account_name', '').strip()[:120]
        account_number = request.form.get('account_number', '').strip()[:80]
        if action == 'add':
            if not bank_name or not account_number:
                flash(_('Bank name and account number are required'), 'error')
                return redirect(url_for('admin_banks'))
            b = BankAccount(
                bank_name=bank_name,
                account_name=account_name,
                account_number=account_number,
                is_active=True
            )
            db.session.add(b)
            db.session.commit()
            flash(_('Bank account added'), 'success')
        elif action in ('edit', 'delete'):
            b = db.get_or_404(BankAccount, safe_int(request.form.get('id'), 0))
            if action == 'edit':
                if not bank_name or not account_number:
                    flash(_('Bank name and account number are required'), 'error')
                    return redirect(url_for('admin_banks'))
                b.bank_name = bank_name
                b.account_name = account_name
                b.account_number = account_number
                b.is_active = bool(request.form.get('is_active'))
                db.session.commit()
                flash(_('Bank account updated'), 'success')
            else:
                if Order.query.filter_by(bank_account_id=b.id).first():
                    # Old orders still point at this account: keep it, just hide it.
                    b.is_active = False
                    db.session.commit()
                    flash(_('This bank account is used by existing orders, so it was deactivated instead of deleted'), 'success')
                else:
                    db.session.delete(b)
                    db.session.commit()
                    flash(_('Bank account deleted'), 'success')
        return redirect(url_for('admin_banks'))
    banks = BankAccount.query.order_by(BankAccount.sort_order, BankAccount.id).all()
    return render_template('admin/banks.html', banks=banks)


@app.route('/admin/transporters', methods=['GET', 'POST'])
@admin_required
def admin_transporters():
    if request.method == 'POST':
        action = request.form.get('action', 'add')
        if action == 'add':
            raw_phone = request.form.get('phone', '').strip()
            phone = normalize_phone(raw_phone) or raw_phone
            if not phone:
                flash(_('Phone required'), 'error')
                return redirect(url_for('admin_transporters'))
            if not valid_phone(phone):
                flash(_('Please enter a valid phone number'), 'error')
                return redirect(url_for('admin_transporters'))
            if Transporter.query.filter_by(phone=phone).first():
                flash(_('Phone already registered'), 'error')
                return redirect(url_for('admin_transporters'))
            typed_pw_early = request.form.get('password', '').strip()
            if typed_pw_early and len(typed_pw_early) < 6:
                flash(_('Password must be at least 6 characters'), 'error')
                return redirect(url_for('admin_transporters'))
            t = Transporter(
                name=request.form.get('name', '').strip()[:120],
                phone=phone,
                vehicle_info=request.form.get('vehicle_info', '').strip()[:120],
                is_active=True
            )
            typed_pw = request.form.get('password', '').strip()
            pw = typed_pw or secrets.token_urlsafe(8)
            t.set_password(pw)
            db.session.add(t)
            db.session.commit()
            if typed_pw:
                flash(_('Transporter created'), 'success')
            else:
                flash(_('Transporter created. Temporary password (shown once): %(pw)s', pw=pw), 'success')
        elif action in ('edit', 'delete'):
            t = db.get_or_404(Transporter, safe_int(request.form.get('id'), 0))
            if action == 'edit':
                raw_phone = request.form.get('phone', '').strip()
                phone = normalize_phone(raw_phone) or raw_phone
                if not valid_phone(phone):
                    flash(_('Please enter a valid phone number'), 'error')
                    return redirect(url_for('admin_transporters'))
                clash = Transporter.query.filter(Transporter.phone == phone, Transporter.id != t.id).first()
                if clash:
                    flash(_('Phone already registered'), 'error')
                    return redirect(url_for('admin_transporters'))
                t.name = request.form.get('name', '').strip()[:120]
                t.phone = phone
                t.vehicle_info = request.form.get('vehicle_info', '').strip()[:120]
                t.is_active = bool(request.form.get('is_active'))
                new_pw = request.form.get('password', '').strip()
                if new_pw:
                    if len(new_pw) < 6:
                        flash(_('Password must be at least 6 characters'), 'error')
                        return redirect(url_for('admin_transporters'))
                    t.set_password(new_pw)
                db.session.commit()
                flash(_('Transporter updated'), 'success')
            else:
                if Order.query.filter_by(transporter_id=t.id).first():
                    # Keep the history of past deliveries: deactivate instead of deleting.
                    t.is_active = False
                    db.session.commit()
                    flash(_('This transporter has past orders, so the account was deactivated instead of deleted'), 'success')
                else:
                    db.session.delete(t)
                    db.session.commit()
                    flash(_('Transporter deleted'), 'success')
        return redirect(url_for('admin_transporters'))
    transporters = Transporter.query.order_by(Transporter.created_at.desc()).all()
    return render_template('admin/transporters.html', transporters=transporters)


@app.route('/admin/vat-delivery', methods=['GET', 'POST'])
@admin_required
def admin_vat_delivery():
    """Dedicated page: site delivery fee + default VAT % for new products."""
    settings = get_settings()
    if request.method == 'POST':
        settings.delivery_fee = form_float('delivery_fee', 0.0, 0.0, 1e7)
        settings.default_vat_percent = form_float('default_vat_percent', 15.0, 0.0, 100.0)
        db.session.commit()
        flash(_('VAT & Delivery settings saved'), 'success')
        return redirect(url_for('admin_vat_delivery'))
    products = Product.query.filter_by(is_active=True).order_by(Product.sort_order, Product.id).limit(200).all()
    return render_template('admin/vat_delivery.html', settings=settings, products=products)


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    settings = get_settings()
    if request.method == 'POST':
        form_type = request.form.get('form_type', 'site')
        if form_type == 'account':
            new_username = request.form.get('admin_username', '').strip()[:80]
            current_pw = request.form.get('current_password', '')
            new_pw = request.form.get('new_password', '')
            new_pw2 = request.form.get('new_password2', '')
            if not current_user.check_password(current_pw):
                flash(_('Current password is incorrect'), 'error')
                return redirect(url_for('admin_settings'))
            if new_username and new_username != current_user.username:
                if Admin.query.filter(Admin.username == new_username, Admin.id != current_user.id).first():
                    flash(_('Username already taken'), 'error')
                    return redirect(url_for('admin_settings'))
                current_user.username = new_username
            if new_pw:
                if new_pw != new_pw2:
                    flash(_('New passwords do not match'), 'error')
                    return redirect(url_for('admin_settings'))
                if len(new_pw) < 8:
                    flash(_('New password must be at least 8 characters'), 'error')
                    return redirect(url_for('admin_settings'))
                current_user.set_password(new_pw)
                # Remove one-time password file after admin sets their own password
                try:
                    note_path = os.path.join(app.instance_path, 'initial_admin_password.txt')
                    if os.path.isfile(note_path):
                        os.remove(note_path)
                except OSError:
                    pass
            db.session.commit()
            flash(_('Admin account updated ✓'), 'success')
            return redirect(url_for('admin_settings'))

        d_min = form_int('delivery_min_hours', 2, 0, 720)
        d_max = form_int('delivery_max_hours', 6, 0, 720)
        if d_max < d_min:
            d_max = d_min
        settings.site_name = (request.form.get('site_name') or 'Dado Delivery').strip()[:120] or 'Dado Delivery'
        settings.primary_color = clean_color(request.form.get('primary_color'), settings.primary_color or '#2E7D32')
        settings.secondary_color = clean_color(request.form.get('secondary_color'), settings.secondary_color or '#FF6F00')
        settings.font_family = clean_font(request.form.get('font_family'), settings.font_family or 'Noto Sans Ethiopic, sans-serif')
        settings.delivery_min_hours = d_min
        settings.delivery_max_hours = d_max
        settings.help_phone = request.form.get('help_phone', '').strip()[:30]
        settings.help_email = request.form.get('help_email', '').strip()[:120]
        settings.help_telegram = request.form.get('help_telegram', '').strip()[:80]
        settings.ai_chat_enabled = bool(request.form.get('ai_chat_enabled'))
        settings.footer_text_am = request.form.get('footer_text_am', '')
        settings.footer_text_en = request.form.get('footer_text_en', '')
        settings.auth_primary_color = clean_color(request.form.get('auth_primary_color'), settings.auth_primary_color or '#2E7D32')
        settings.auth_font_family = clean_font(request.form.get('auth_font_family'), settings.font_family)
        settings.logo_display_height = form_int('logo_display_height', 80, 40, 300)
        settings.auth_logo_height = form_int('auth_logo_height', 80, 40, 300)
        settings.transporter_logo_height = form_int('transporter_logo_height', 80, 40, 300)

        for field, prefix, attr in (('logo', 'logo', 'logo_path'),
                                    ('auth_logo', 'authlogo', 'auth_logo_path'),
                                    ('transporter_logo', 'trlogo', 'transporter_logo_path')):
            upload = request.files.get(field)
            if upload and upload.filename:
                p = save_image(upload, prefix)
                if p:
                    old_path = getattr(settings, attr, '') or ''
                    setattr(settings, attr, p)
                    if old_path and old_path != p:
                        delete_stored_file(old_path)
                else:
                    flash(_('The image could not be saved. Use a JPG, PNG, GIF or WEBP file.'), 'error')

        db.session.commit()
        flash(_('Settings saved ✓'), 'success')
        return redirect(url_for('admin_settings'))

    return render_template('admin/settings.html', settings=settings)


@app.route('/admin/customers', methods=['GET', 'POST'])
@admin_required
def admin_customers():
    if request.method == 'POST':
        action = request.form.get('action', '')
        cid = safe_int(request.form.get('id'), 0)
        c = db.session.get(Customer, cid) if cid else None
        if not c:
            flash(_('Customer not found'), 'error')
            return redirect(url_for('admin_customers'))
        if action == 'toggle_active':
            c.is_active = not c.is_active
            db.session.commit()
            flash(_('Customer activated') if c.is_active else _('Customer blocked'), 'success')
        elif action == 'reset_password':
            import secrets as _secrets
            temp = _secrets.token_urlsafe(8)
            c.set_password(temp)
            db.session.commit()
            flash(_('Temporary password for %(phone)s: %(pw)s — share once then customer should change it.',
                    phone=c.phone, pw=temp), 'success')
        return redirect(url_for('admin_customers'))
    customers = Customer.query.order_by(Customer.created_at.desc()).limit(1000).all()
    return render_template('admin/customers.html', customers=customers)


@app.route('/admin/support')
@admin_required
def admin_support():
    requests_list = SupportRequest.query.order_by(SupportRequest.created_at.desc()).limit(500).all()
    chats = ChatSession.query.order_by(ChatSession.updated_at.desc()).limit(500).all()
    return render_template('admin/support.html', requests=requests_list, chats=chats)


@app.route('/admin/support/<int:rid>/close', methods=['POST'])
@admin_required
def admin_support_close(rid):
    req = db.get_or_404(SupportRequest, rid)
    req.status = 'closed'
    db.session.commit()
    flash(_('Request closed'), 'success')
    return redirect(url_for('admin_support'))


@app.route('/admin/chat/<int:cid>', methods=['GET', 'POST'])
@admin_required
def admin_chat_detail(cid):
    chat = db.get_or_404(ChatSession, cid)
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'close':
            chat.status = 'closed'
            db.session.commit()
            flash(_('Chat closed'), 'success')
            return redirect(url_for('admin_support'))
        message = request.form.get('message', '').strip()[:2000]
        if message:
            msg = ChatMessage(session_id=chat.id, sender='admin', message=message)
            db.session.add(msg)
            chat.updated_at = utcnow()
            chat.status = 'open'
            db.session.commit()
        return redirect(url_for('admin_chat_detail', cid=cid))
    return render_template('admin/chat_detail.html', chat=chat)


# ---------------------------------------------------------------------------
# Initialize database & seed data
# ---------------------------------------------------------------------------
def _ensure_column(table, column, ddl):
    """Add a missing column to an existing table (SQLite and PostgreSQL)."""
    insp = sa_inspect(db.engine)
    if table not in insp.get_table_names():
        return False
    if column in {c['name'] for c in insp.get_columns(table)}:
        return False
    quoted = '"order"' if table == 'order' else table
    db.session.execute(db.text(f'ALTER TABLE {quoted} ADD COLUMN {column} {ddl}'))
    db.session.commit()
    return True


def _migrate_private_files():
    """Move receipts that older versions stored in the public /static folder."""
    rows = Order.query.filter(or_(Order.receipt_path.like('uploads/%'),
                                  Order.delivery_receipt_path.like('uploads/%'))).all()
    for order in rows:
        for attr in ('receipt_path', 'delivery_receipt_path'):
            rel = (getattr(order, attr) or '')
            if not rel.startswith('uploads/'):
                continue
            name = os.path.basename(rel)
            src_path = os.path.join(app.config['UPLOAD_FOLDER'], name)
            dst_path = os.path.join(PRIVATE_UPLOAD_FOLDER, name)
            try:
                if os.path.isfile(src_path) and not os.path.isfile(dst_path):
                    shutil.move(src_path, dst_path)
                if os.path.isfile(dst_path):
                    setattr(order, attr, 'private/' + name)
            except OSError as e:
                print('receipt move note:', e)
    if rows:
        db.session.commit()


def init_db():
    # The lock makes this safe when several gunicorn workers start at the same time.
    with app.app_context(), init_lock():
        db.create_all()
        ts = 'DATETIME' if db.engine.dialect.name == 'sqlite' else 'TIMESTAMP'
        # lightweight migrations for existing databases
        try:
            for col, ddl in [
                ('logo_display_height', 'INTEGER DEFAULT 80'),
                ('transporter_logo_path', "VARCHAR(255) DEFAULT ''"),
                ('transporter_logo_height', 'INTEGER DEFAULT 80'),
                ('auth_logo_height', 'INTEGER DEFAULT 80'),
            ]:
                _ensure_column('site_settings', col, ddl)
        except Exception as e:
            db.session.rollback()
            print('settings migrate note:', e)
        try:
            _ensure_column('product', 'unit', "VARCHAR(10) DEFAULT 'kg'")
            _ensure_column('order_item', 'unit', "VARCHAR(10) DEFAULT 'kg'")
            _ensure_column('site_settings', 'ai_chat_enabled', 'BOOLEAN DEFAULT FALSE')
            _ensure_column('site_settings', 'delivery_fee', 'FLOAT DEFAULT 0')
            _ensure_column('site_settings', 'default_vat_percent', 'FLOAT DEFAULT 15')
            _ensure_column('product', 'vat_percent', 'FLOAT DEFAULT 0')
            _ensure_column('order_item', 'vat_percent', 'FLOAT DEFAULT 0')
            _ensure_column('order', 'items_subtotal', 'FLOAT DEFAULT 0')
            _ensure_column('order', 'delivery_fee', 'FLOAT DEFAULT 0')
            _ensure_column('order', 'vat_mode', "VARCHAR(20) DEFAULT 'inclusive'")
        except Exception as e:
            db.session.rollback()
            print('unit/ai/vat migrate note:', e)
        try:
            for col, ddl in [
                ('delivery_receipt_path', "VARCHAR(255) DEFAULT ''"),
                ('cod_amount_collected', 'FLOAT'),
                ('delivery_receipt_note', "VARCHAR(255) DEFAULT ''"),
                ('delivery_receipt_at', ts),
                ('bank_name_snapshot', "VARCHAR(120) DEFAULT ''"),
                ('bank_account_name_snapshot', "VARCHAR(120) DEFAULT ''"),
                ('bank_account_number_snapshot', "VARCHAR(80) DEFAULT ''"),
                ('location_accuracy_m', 'FLOAT'),
            ]:
                _ensure_column('order', col, ddl)
            if _ensure_column('order', 'stock_released', 'BOOLEAN DEFAULT FALSE'):
                # Orders cancelled by older versions already had their stock returned.
                db.session.execute(db.text("UPDATE \"order\" SET stock_released = TRUE WHERE status = 'cancelled'"))
                db.session.commit()
            if db.engine.dialect.name == 'postgresql':
                db.session.execute(db.text('ALTER TABLE "order" ALTER COLUMN order_number TYPE VARCHAR(40)'))
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print('order migrate note:', e)

        try:
            _migrate_private_files()
        except Exception as e:
            db.session.rollback()
            print('private files migrate note:', e)

        if not Admin.query.first():
            admin_user = os.environ.get('ADMIN_USERNAME', 'admin').strip() or 'admin'
            admin_pw = os.environ.get('ADMIN_PASSWORD', '').strip()
            generated = not admin_pw
            if generated:
                admin_pw = secrets.token_urlsafe(10)
            admin = Admin(username=admin_user)
            admin.set_password(admin_pw)
            db.session.add(admin)
            db.session.commit()
            if generated:
                # Shown once: written to the console and to a private file, never a fixed default.
                note_path = os.path.join(app.instance_path, 'initial_admin_password.txt')
                try:
                    fd = os.open(note_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, 'w') as fh:
                        fh.write(f"username: {admin_user}\npassword: {admin_pw}\n"
                                 "Change this password in Admin > Settings, then delete this file.\n")
                except OSError:
                    note_path = '(could not write file)'
                print(f"Admin created. username={admin_user}  password={admin_pw}")
                print(f"This password is also saved in {note_path} - change it after the first login, then delete the file.")
            else:
                print(f"Admin created: username={admin_user}")

        if not SiteSettings.query.first():
            settings = SiteSettings()
            db.session.add(settings)
            db.session.commit()

        # Safety: never expose placeholder bank numbers to customers
        for bad in BankAccount.query.filter(BankAccount.account_number.ilike('%REPLACE%')).all():
            if bad.is_active:
                bad.is_active = False
                print('Deactivated placeholder bank account id=', bad.id)
        db.session.commit()

        if BankAccount.query.count() == 0:
            # Sample entry only. It is INACTIVE so customers are never told to pay into a fake account;
            # edit it with the real details in Admin > Banks and tick "active".
            db.session.add(BankAccount(
                bank_name='Commercial Bank of Ethiopia',
                account_name='Dado Delivery',
                account_number='REPLACE-WITH-REAL-ACCOUNT',
                is_active=False
            ))
            db.session.commit()
            print('Sample (inactive) bank account created - set the real details in Admin > Banks')

        
        if Category.query.count() == 0:
            for i, (am, en) in enumerate([
                ('እህል', 'Grains'),
                ('ዘይት', 'Oil'),
                ('ሸቀጦች', 'Groceries'),
            ]):
                db.session.add(Category(name_am=am, name_en=en, sort_order=i, is_active=True))
            db.session.commit()
            print('Default categories created: Grains, Oil, Groceries')

        if Product.query.count() == 0:
            products_data = [
                {'name_am': 'ምስር ክክ', 'name_en': 'Red Lentils (Misir Kikk)', 'desc_am': 'ጥራት ያለው ቀይ ምስር', 'desc_en': 'High quality red split lentils'},
                {'name_am': 'ባቄላ ክክ', 'name_en': 'Fava Beans (Bakela Kikk)', 'desc_am': 'የተፈጨ ባቄላ', 'desc_en': 'Split fava beans'},
                {'name_am': 'ሽሮ ክክ', 'name_en': 'Shiro Powder', 'desc_am': 'የተፈጨ ሽሮ', 'desc_en': 'Traditional Ethiopian shiro powder'},
                {'name_am': 'ድፍን ምስር', 'name_en': 'Whole Lentils (Difn Misir)', 'desc_am': 'ሙሉ ምስር', 'desc_en': 'Whole green/brown lentils'},
                {'name_am': 'አተር ክክ', 'name_en': 'Split Peas (Ater Kikk)', 'desc_am': 'የተፈጨ አተር', 'desc_en': 'Yellow/green split peas'},
                {'name_am': 'ጓያ', 'name_en': 'Guaya (Grass Pea)', 'desc_am': 'ጓያ', 'desc_en': 'Grass pea / chickling vetch'},
                {'name_am': 'ሽንብራ', 'name_en': 'Chickpeas (Shinbra)', 'desc_am': 'ሽንብራ', 'desc_en': 'Dried chickpeas'},
            ]
            default_prices = {10: 850, 15: 1200, 25: 1900, 50: 3600, 100: 7000}  # example ETB

            for i, p in enumerate(products_data):
                product = Product(
                    name_am=p['name_am'],
                    name_en=p['name_en'],
                    description_am=p['desc_am'],
                    description_en=p['desc_en'],
                    is_active=True,
                    sort_order=i,
                    delivery_min_hours=2,
                    delivery_max_hours=6
                )
                db.session.add(product)
                db.session.flush()
                for w, price in default_prices.items():
                    unit = price * (1 + i * 0.05)
                    variant = ProductVariant(
                        product_id=product.id,
                        weight_kg=w,
                        price=unit,
                        bulk_min_qty=10,
                        bulk_price=round(unit * 0.94, 0),  # ~6% off when qty >= 10
                        stock=200,
                        is_available=True
                    )
                    db.session.add(variant)
            db.session.commit()
            print("Seed products created.")


# ---------------------------------------------------------------------------
# Error pages and small APIs
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    return render_template('error.html', code=404, message=_('Page not found')), 404


@app.errorhandler(403)
def forbidden(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Forbidden'}), 403
    return render_template('error.html', code=403, message=_('Access denied')), 403


@app.errorhandler(405)
def method_not_allowed(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Method not allowed'}), 405
    return render_template('error.html', code=405, message=_('This action is not allowed here')), 405


@app.errorhandler(413)
def too_large(e):
    if request.path.startswith('/api/') or request.path == '/checkout':
        return jsonify({'success': False, 'error': _('File too large (max 20 MB)')}), 413
    flash(_('File too large (max 20 MB)'), 'error')
    return redirect(safe_referrer() or url_for('index'))


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Server error'}), 500
    return render_template('error.html', code=500, message=_('Something went wrong')), 500


@app.route('/api/cart/prices', methods=['POST'])
def api_cart_prices():
    """Refresh cart line prices from current DB (daily price updates)."""
    if rate_exceeded('cart_prices', 120, 3600):
        return jsonify({'error': 'rate_limited'}), 429
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    items = data.get('items') or []
    if not isinstance(items, list):
        items = []
    result = []
    for it in items[:100]:
        if not isinstance(it, dict):
            continue
        vid = safe_int(it.get('variant_id'), None)
        qty = safe_int(it.get('quantity', 1), 1)
        if vid is None:
            continue
        qty = min(1000, max(1, qty))
        variant = db.session.get(ProductVariant, vid)
        if not variant or not variant.is_available or not variant.product or not variant.product.is_active:
            result.append({'variant_id': vid, 'available': False})
            continue
        unit = variant.unit_price_for(qty)
        lang = get_locale()
        pname = variant.product.name_am if lang == 'am' else (variant.product.name_en or variant.product.name_am)
        result.append({
            'variant_id': vid,
            'available': True,
            'price': unit,
            'base_price': variant.price,
            'bulk_price': variant.bulk_price,
            'bulk_min': variant.bulk_min_qty,
            'stock': variant.stock,
            'name': pname,
            'weight_kg': variant.weight_kg,
            'unit': variant.product.unit or 'kg',
        })
    return jsonify({'items': result})


# Always init DB (works with gunicorn and python app.py)
init_db()

if __name__ == '__main__':
    app.run(debug=False, host=os.environ.get('HOST', '0.0.0.0'), port=int(os.environ.get('PORT', '5000')))
