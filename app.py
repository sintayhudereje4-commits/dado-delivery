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
from urllib.parse import urlparse
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
WEIGHTS = [10, 15, 25, 50, 100]

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


def valid_phone(phone):
    return bool(phone) and bool(PHONE_RE.match(phone.strip()))


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
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
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


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name_am = db.Column(db.String(120), nullable=False)
    name_en = db.Column(db.String(120), nullable=False)
    description_am = db.Column(db.Text, default='')
    description_en = db.Column(db.Text, default='')
    image_path = db.Column(db.String(255), default='')
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
    weight_kg = db.Column(db.Integer, nullable=False)  # 10, 15, 25, 50, 100
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
    """Registered store customer (email login)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), default='')
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(30), default='')
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
    status = db.Column(db.String(30), default='pending')  # pending, confirmed, preparing, out_for_delivery, delivered, cancelled
    notes = db.Column(db.Text, default='')
    total_amount = db.Column(db.Float, default=0.0)
    estimated_delivery = db.Column(db.String(80), default='')
    # Payment (for totals 100–100000 ETB)
    payment_required = db.Column(db.Boolean, default=False)
    payment_status = db.Column(db.String(30), default='not_required')  # not_required, pending, confirmed, rejected
    transaction_id = db.Column(db.String(120), default='')
    receipt_path = db.Column(db.String(255), default='')
    bank_account_id = db.Column(db.Integer, db.ForeignKey('bank_account.id'), nullable=True)
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
    weight_kg = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Integer, default=1)
    subtotal = db.Column(db.Float, nullable=False)


class RateHit(db.Model):
    """One row per rate-limited event (login failures, chat messages, ...)."""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(160), nullable=False, index=True)
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
    """At least 8 chars, 1 uppercase, 1 symbol."""
    if not password or len(password) < 8:
        return False, _('Password must be at least 8 characters')
    if not re.search(r'[A-Z]', password):
        return False, _('Password must contain at least 1 capital letter')
    if not re.search(r'[^A-Za-z0-9]', password):
        return False, _('Password must contain at least 1 symbol')
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


def login_blocked(scope, ident):
    ident = (ident or '').strip().lower()[:80]
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


def change_order_status(order, new_status):
    """Change an order's status and keep stock consistent.

    Cancelling returns the reserved stock exactly once; re-opening a cancelled
    order reserves it again (and fails if there is not enough stock).
    Returns an error message, or None on success.
    """
    old = order.status
    if new_status == old:
        return None
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
    order.status = new_status
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
    return {
        'settings': settings,
        'current_lang': get_locale(),
        'languages': app.config['LANGUAGES'],
        'now': utcnow(),
        'prepay_min': PREPAY_MIN,
        'prepay_max': PREPAY_MAX,
        'current_customer': get_current_customer()
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
    products = Product.query.filter_by(is_active=True).order_by(Product.sort_order, Product.id).all()
    return render_template('index.html', products=products)


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    product = db.get_or_404(Product, product_id)
    if not product.is_active:
        abort(404)
    return render_template('product.html', product=product)


@app.route('/cart')
def cart():
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

    phone = (data.get('phone') or '').strip()
    if not phone:
        return _json_error(_('Phone number is required'))
    if not valid_phone(phone):
        return _json_error(_('Please enter a valid phone number'))

    customer_name = (data.get('customer_name') or '').strip()[:120]
    address_text = (data.get('address_text') or '').strip()[:500]
    notes = (data.get('notes') or '').strip()[:1000]
    transaction_id = (data.get('transaction_id') or '').strip()[:120]
    location_method = (data.get('location_method') or 'manual').strip()
    if location_method not in ('gps', 'manual'):
        location_method = 'manual'

    lat_f = parse_coord(data.get('latitude'), 90)
    lng_f = parse_coord(data.get('longitude'), 180)
    if lat_f is None or lng_f is None:
        return _json_error(_('Please set your location on the map'))
    # Reject the untouched default Addis centre (within ~50 m) unless the customer confirmed it
    if abs(lat_f - 9.03) < 0.0005 and abs(lng_f - 38.74) < 0.0005 and location_method == 'manual':
        if not data.get('location_confirmed'):
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

    # Delivery time is decided by admin after the order is submitted
    estimated = ''

    payment_required = PREPAY_MIN <= total <= PREPAY_MAX
    payment_status = 'not_required'
    receipt_path = ''
    bank_id = None

    if payment_required:
        payment_status = 'pending'
        bank_id = safe_int(data.get('bank_account_id'), 0)
        bank = db.session.get(BankAccount, bank_id) if bank_id else None
        if not bank or not bank.is_active:
            return _json_error(_('Please select a bank account'), code='payment_required', total=total)
        upload = request.files.get('receipt')
        if upload and upload.filename:
            receipt_path = save_image(upload, 'receipt', private=True)
            if not receipt_path:
                return _json_error(_('The receipt must be a valid JPG, PNG, GIF or WEBP image'))
        if not transaction_id and not receipt_path:
            return _json_error(_('Send transaction ID or receipt photo'), code='payment_required', total=total)

    cust = get_current_customer()
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
            notes=notes,
            total_amount=total,
            estimated_delivery=estimated,
            status='pending',
            payment_required=payment_required,
            payment_status=payment_status,
            transaction_id=transaction_id,
            receipt_path=receipt_path,
            bank_account_id=bank_id or None
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
                product_name=oi['product'].name_am if lang == 'am' else oi['product'].name_en,
                weight_kg=oi['variant'].weight_kg,
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
    return jsonify({
        'success': True,
        'order_number': order.order_number,
        'estimated': estimated,
        'payment_required': payment_required,
        'redirect': url_for('order_success', order_number=order.order_number)
    })


@app.route('/order/success/<order_number>')
def order_success(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    allowed = session.get('pending_orders') or []
    # Allow if in this browser session, or logged-in customer owns it, or admin
    cust = get_current_customer()
    ok = order.order_number in allowed
    if cust and order.customer_id == cust.id:
        ok = True
    if current_user.is_authenticated and isinstance(current_user, Admin):
        ok = True
    if not ok:
        abort(403)
    return render_template('order_success.html', order=order)


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

    # greetings
    greet_keys = ['hello', 'hi', 'hey', 'ሰላም', 'ሀይ', 'ሃይ', 'ጤና', 'እንደምን', 'good morning', 'good evening']
    if any(k in msg or k in raw for k in greet_keys) and len(msg) < 40:
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
                vars_ = ', '.join(f'{v.weight_kg}kg' for v in p.variants if v.is_available)
                lines.append(f'• {name}' + (f' ({vars_})' if vars_ else ''))
            body = '\n'.join(lines)
            return am_en(
                f'ያሉ ምርቶች፦\n{body}\n\nክብደት፦ 10 / 15 / 25 / 50 / 100 ኪ.ግ። ከመነሻ ገጽ ምርጫ ያድርጉ።',
                f'Available products:\n{body}\n\nWeights: 10 / 15 / 25 / 50 / 100 kg. Choose from the home page.'
            )
        return am_en('በአሁኑ ሰዓት የታተሙ ምርቶች የሉም።', 'No published products right now.')

    # price
    if any(k in msg or k in raw for k in ['price', 'ዋጋ', 'ስንት', 'cost', 'how much', 'በስንት']):
        return am_en(
            'ዋጋው በእያንዳንዱ ምርትና ክብደት ይለያያል፤ ከ10 በላይ ፍሬ ሲያዙ ቅናሽ (bulk) ሊኖር ይችላል።\n'
            'ትክክለኛ ዋጋ ለማየት ምርቱን ከመነሻ ገጽ ይምረጡ።',
            'Prices vary by product and weight. Buying 10+ units may get bulk discount.\n'
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
            'አካውንት፦ Register በ email። የይለፍ ቃል ቢያንስ 8 ቁምፊ፣ 1 አቢይ ሆሄ፣ 1 ምልክት።\n'
            'ከተመዘገቡ በኋላ ከማንኛውም መሣሪያ Login ማድረግ ይችላሉ።',
            'Account: Register with email. Password needs 8+ chars, 1 capital, 1 symbol.\n'
            'After register you can Login from any device.'
        )

    # thanks
    if any(k in msg or k in raw for k in ['thanks', 'thank', 'አመሰግን', 'እሺ', 'ok', 'okay']):
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
        'created_at': m.created_at.strftime('%H:%M')
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
    # AI only if admin has not replied in last 30 minutes
    last_admin = ChatMessage.query.filter_by(session_id=chat.id, sender='admin').order_by(
        ChatMessage.created_at.desc()
    ).first()
    admin_active = bool(last_admin and last_admin.created_at and
                        (utcnow() - last_admin.created_at).total_seconds() < 1800)
    if not admin_active:
        settings = get_settings()
        ai_text = ai_help_reply(message, settings)
        ai_msg = ChatMessage(session_id=chat.id, sender='ai', message=ai_text)
        db.session.add(ai_msg)
        db.session.flush()  # assigns ai_msg.id so the browser can de-duplicate it when polling
        ai_payload = {
            'id': ai_msg.id,
            'sender': 'ai',
            'message': ai_msg.message,
            'created_at': (ai_msg.created_at or utcnow()).strftime('%H:%M')
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
            'created_at': msg.created_at.strftime('%H:%M') if msg.created_at else ''
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
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()[:30]
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if rate_exceeded('register', 20, 3600):
            flash(_('Too many requests. Please try again later.'), 'error')
            return redirect(url_for('register'))
        if not email or not password:
            flash(_('Email and password are required'), 'error')
            return redirect(url_for('register'))
        if not valid_email(email):
            flash(_('Please enter a valid email address'), 'error')
            return redirect(url_for('register'))
        if phone and not valid_phone(phone):
            flash(_('Please enter a valid phone number'), 'error')
            return redirect(url_for('register'))
        if password != password2:
            flash(_('Passwords do not match'), 'error')
            return redirect(url_for('register'))
        ok, msg = validate_customer_password(password)
        if not ok:
            flash(msg, 'error')
            return redirect(url_for('register'))
        if Customer.query.filter_by(email=email).first():
            flash(_('Email already registered'), 'error')
            return redirect(url_for('register'))
        c = Customer(name=name, email=email, phone=phone)
        c.set_password(password)
        db.session.add(c)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(_('Email already registered'), 'error')
            return redirect(url_for('register'))
        start_fresh_session(customer_id=c.id)
        session.permanent = True
        flash(_('Account created successfully'), 'success')
        return redirect(url_for('index'))
    return render_template('auth/register.html')


@app.route('/login', methods=['GET', 'POST'])
def customer_login():
    if get_current_customer():
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()[:120]
        password = request.form.get('password', '')
        if login_blocked('customer', email):
            flash(_('Too many failed attempts. Please try again in 15 minutes.'), 'error')
            return render_template('auth/login.html'), 429
        c = Customer.query.filter_by(email=email, is_active=True).first()
        if c and c.check_password(password):
            login_succeeded('customer', email)
            start_fresh_session(customer_id=c.id)
            session.permanent = True
            flash(_('Logged in successfully'), 'success')
            return redirect(url_for('index'))
        login_failed('customer', email)
        flash(_('Invalid email or password'), 'error')
    return render_template('auth/login.html')


@app.route('/logout', methods=['POST'])
def customer_logout():
    session.pop('customer_id', None)
    flash(_('Logged out'), 'success')
    return redirect(url_for('index'))


@app.route('/account')
def account():
    c = get_current_customer()
    if not c:
        return redirect(url_for('customer_login'))
    orders = Order.query.filter_by(customer_id=c.id).order_by(Order.created_at.desc()).limit(20).all()
    return render_template('auth/account.html', customer=c, orders=orders)


# ---------------------------------------------------------------------------
# Transporter portal
# ---------------------------------------------------------------------------
@app.route('/transporter/login', methods=['GET', 'POST'])
def transporter_login():
    if get_current_transporter():
        return redirect(url_for('transporter_dashboard'))
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()[:30]
        password = request.form.get('password', '')
        if login_blocked('transporter', phone):
            flash(_('Too many failed attempts. Please try again in 15 minutes.'), 'error')
            return render_template('transporter/login.html'), 429
        t = Transporter.query.filter_by(phone=phone, is_active=True).first()
        if t and t.check_password(password):
            login_succeeded('transporter', phone)
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
    orders = Order.query.filter_by(transporter_id=t.id).order_by(Order.assigned_at.desc().nullslast(), Order.created_at.desc()).all()
    return render_template('transporter/dashboard.html', transporter=t, orders=orders)


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
    if new_status in ('out_for_delivery', 'delivered'):
        if order.status in ('cancelled', 'delivered'):
            flash(_('This order is already closed'), 'error')
            return redirect(url_for('transporter_order', oid=oid))
        if new_status == 'delivered' and order.total_amount > PREPAY_MAX and not order.delivery_receipt_path:
            flash(_('Upload COD receipt before marking delivered'), 'error')
            return redirect(url_for('transporter_order', oid=oid))
        err = change_order_status(order, new_status)
        if err:
            flash(err, 'error')
            return redirect(url_for('transporter_order', oid=oid))
        db.session.commit()
        flash(_('Status updated'), 'success')
    return redirect(url_for('transporter_order', oid=oid))


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
    if order.status == 'cancelled':
        flash(_('This order is already closed'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    if 'receipt' not in request.files or not request.files['receipt'].filename:
        flash(_('Please choose a receipt photo'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    raw_amt = (request.form.get('amount_collected') or '').strip()
    amt = order.total_amount if not raw_amt else safe_float(raw_amt, None)
    if amt is None or not math.isfinite(amt) or amt <= 0 or amt > 1e10:
        flash(_('Please enter a valid amount collected'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    path_saved = save_image(request.files['receipt'], 'cod_receipt', private=True)
    if not path_saved:
        flash(_('Could not save receipt. Use a JPG, PNG, GIF or WEBP photo.'), 'error')
        return redirect(url_for('transporter_order', oid=oid))
    order.delivery_receipt_path = path_saved
    order.delivery_receipt_note = request.form.get('note', '').strip()[:255]
    order.cod_amount_collected = amt
    order.delivery_receipt_at = utcnow()
    if order.status != 'delivered':
        order.status = 'delivered'
    db.session.commit()
    flash(_('Receipt sent to admin ✓'), 'success')
    return redirect(url_for('transporter_order', oid=oid))


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated and isinstance(current_user, Admin):
        nxt = request.args.get('next')
        return redirect(nxt if is_safe_path(nxt) else url_for('admin_dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()[:80]
        password = request.form.get('password', '')
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


@app.route('/admin/products')
@admin_required
def admin_products():
    products = Product.query.order_by(Product.sort_order, Product.id).all()
    return render_template('admin/products.html', products=products)


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
            product.image_path = img_path
        else:
            flash(_('The image could not be saved. Use a JPG, PNG, GIF or WEBP file.'), 'error')


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
        product = Product(
            name_am=name_am,
            name_en=name_en,
            description_am=request.form.get('description_am', ''),
            description_en=request.form.get('description_en', ''),
            is_active=bool(request.form.get('is_active')),
            sort_order=form_int('sort_order', 0, -10000, 10000),
            delivery_min_hours=d_min,
            delivery_max_hours=d_max
        )
        _save_product_image(product)

        db.session.add(product)
        db.session.flush()

        for w in WEIGHTS:
            variant = ProductVariant(
                product_id=product.id,
                weight_kg=w,
                price=form_float(f'price_{w}', 0.0, 0.0, 1e8),
                bulk_price=form_float_or_none(f'bulk_price_{w}', 0.0, 1e8),
                bulk_min_qty=form_int(f'bulk_min_{w}', 10, 1, 100000),
                stock=form_int(f'stock_{w}', 100, 0, 10000000),
                is_available=bool(request.form.get(f'available_{w}'))
            )
            db.session.add(variant)

        db.session.commit()
        flash(_('Product added successfully'), 'success')
        return redirect(url_for('admin_products'))

    return render_template('admin/product_form.html', product=None, weights=WEIGHTS)


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
        product.name_am = name_am
        product.name_en = name_en
        product.description_am = request.form.get('description_am', '')
        product.description_en = request.form.get('description_en', '')
        product.is_active = bool(request.form.get('is_active'))
        product.sort_order = form_int('sort_order', 0, -10000, 10000)
        product.delivery_min_hours = d_min
        product.delivery_max_hours = d_max
        _save_product_image(product)

        for variant in product.variants:
            w = variant.weight_kg
            variant.price = form_float(f'price_{w}', 0.0, 0.0, 1e8)
            variant.bulk_price = form_float_or_none(f'bulk_price_{w}', 0.0, 1e8)
            variant.bulk_min_qty = form_int(f'bulk_min_{w}', 10, 1, 100000)
            variant.stock = form_int(f'stock_{w}', 0, 0, 10000000)
            variant.is_available = bool(request.form.get(f'available_{w}'))

        db.session.commit()
        flash(_('Product updated successfully'), 'success')
        return redirect(url_for('admin_products'))

    return render_template('admin/product_form.html', product=product, weights=WEIGHTS)


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
    status = request.args.get('status', '')
    query = Order.query.order_by(Order.created_at.desc())
    if status:
        query = query.filter_by(status=status)
    orders = query.limit(500).all()
    return render_template('admin/orders.html', orders=orders, current_status=status)


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
        if action == 'assign':
            tid = (request.form.get('transporter_id') or '').strip()
            if tid:
                if order.status in ('cancelled', 'delivered'):
                    flash(_('This order is already closed'), 'error')
                    return back
                if order.payment_required and order.payment_status not in ('confirmed', 'not_required'):
                    flash(_('Confirm payment before assigning transporter'), 'error')
                    return back
                tr = db.session.get(Transporter, safe_int(tid, 0))
                if not tr or not tr.is_active:
                    flash(_('Transporter not found or inactive'), 'error')
                    return back
                order.transporter_id = tr.id
                order.assigned_at = utcnow()
                if order.status == 'pending':
                    order.status = 'confirmed'
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
                order.payment_status = pay
                order.payment_note = note
                if pay == 'confirmed' and order.status == 'pending':
                    order.status = 'confirmed'
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
                order.notes = ((order.notes or '') + ('\n' if order.notes else '') + '[Admin]: ' + msg).strip()
            db.session.commit()
            flash(_('Delivery time saved — customer will see it on Cart / order'), 'success')
            return back
        new_status = request.form.get('status')
        if new_status in ['pending', 'confirmed', 'preparing', 'out_for_delivery', 'delivered', 'cancelled']:
            err = change_order_status(order, new_status)
            if err:
                db.session.rollback()
                flash(err, 'error')
            else:
                db.session.commit()
                flash(_('Order status updated'), 'success')
        return back
    return render_template('admin/order_detail.html', order=order, bank=bank, transporters=transporters, assigned=assigned)


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
            phone = request.form.get('phone', '').strip()
            if not phone:
                flash(_('Phone required'), 'error')
                return redirect(url_for('admin_transporters'))
            if not valid_phone(phone):
                flash(_('Please enter a valid phone number'), 'error')
                return redirect(url_for('admin_transporters'))
            if Transporter.query.filter_by(phone=phone).first():
                flash(_('Phone already registered'), 'error')
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
                phone = request.form.get('phone', '').strip()
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
                    setattr(settings, attr, p)
                else:
                    flash(_('The image could not be saved. Use a JPG, PNG, GIF or WEBP file.'), 'error')

        db.session.commit()
        flash(_('Settings saved ✓'), 'success')
        return redirect(url_for('admin_settings'))

    return render_template('admin/settings.html', settings=settings)


@app.route('/admin/customers')
@admin_required
def admin_customers():
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
            for col, ddl in [
                ('delivery_receipt_path', "VARCHAR(255) DEFAULT ''"),
                ('cod_amount_collected', 'FLOAT'),
                ('delivery_receipt_note', "VARCHAR(255) DEFAULT ''"),
                ('delivery_receipt_at', ts),
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
        result.append({
            'variant_id': vid,
            'available': True,
            'price': unit,
            'base_price': variant.price,
            'bulk_price': variant.bulk_price,
            'bulk_min': variant.bulk_min_qty,
            'stock': variant.stock,
            'name': variant.product.name_am,
            'weight_kg': variant.weight_kg,
        })
    return jsonify({'items': result})


# Always init DB (works with gunicorn and python app.py)
init_db()

if __name__ == '__main__':
    app.run(debug=False, host=os.environ.get('HOST', '0.0.0.0'), port=int(os.environ.get('PORT', '5000')))
