# Dado Delivery (www.dadodelivery.com)

Multi-product e-commerce platform for grains & legumes delivery.

## Features

- **Public storefront** (Amharic + English)
  - Product catalog with 5 weight options (10 / 15 / 25 / 50 / 100 kg)
  - Shopping cart (localStorage)
  - Phone login with normalized Ethiopian numbers (+251…)
  - Checkout with GPS location sharing OR manual address + phone
  - Delivery time estimation (admin-configurable)
  - Help / support form (available anytime)

- **Admin panel** (`/admin`)
  - Full product management (add / edit / delete / publish, change photos, prices, stock)
  - Daily price updates
  - Order management with status workflow + Google Maps link for GPS orders
  - Site settings: logo, colors, fonts, delivery hours, help contacts, footer text
  - Support request inbox

## Products (seeded)

| Amharic     | English                  |
|-------------|--------------------------|
| ምስር ክክ     | Red Lentils (Misir Kikk) |
| ባቄላ ክክ     | Fava Beans (Bakela Kikk) |
| ሽሮ ክክ      | Shiro Powder             |
| ድፍን ምስር    | Whole Lentils            |
| አተር ክክ     | Split Peas (Ater Kikk)   |
| ጓያ         | Guaya (Grass Pea)        |
| ሽንብራ       | Chickpeas (Shinbra)      |

## Quick Start

```bash
cd dado_delivery
pip install -r requirements.txt
python app.py
```

Open: http://127.0.0.1:5000

**Admin login**
- URL: http://127.0.0.1:5000/admin/login
- Username: `admin` (or `ADMIN_USERNAME`)
- Password: `ADMIN_PASSWORD` if you set it. Otherwise a **random password is generated on the first start**,
  printed once in the console and saved to `instance/initial_admin_password.txt`.
  Log in, change it in *Admin → Settings*, then delete that file.

## Configuration (environment variables)

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Signs sessions and CSRF tokens. If not set, a key is created once in `instance/.secret_key` and shared by all gunicorn workers. Set it explicitly in production. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Used only when the first admin is created. |
| `DATABASE_URL` | Optional, e.g. `postgresql://user:pass@host/dbname`. Default: SQLite file `dado.db`. |
| `SESSION_COOKIE_SECURE` | Set to `1` when the site is served over HTTPS. |
| `TRUST_PROXY` | Set to `1` behind nginx / a load balancer so the real client IP is used for rate limiting. |
| `HOST` / `PORT` | Only for `python app.py` (default `0.0.0.0:5000`). |

## After the first start - checklist

1. **Admin → Banks:** a sample bank account is created but is **inactive**. Enter your real account details and tick *active*.
   Customers with orders between 100 and 100,000 ETB cannot check out until at least one bank account is active.
2. **Admin → Settings:** change the admin password.
3. **Admin → Transporters:** add your riders (if you leave the password empty a temporary one is shown once).

## Payment rules

- Orders **100 – 100,000 ETB**: customer pays by bank deposit first (transaction ID or receipt photo); the admin confirms it.
- Orders **over 100,000 ETB**: cash on delivery; the transporter uploads the receipt.
- The limits are the constants `PREPAY_MIN` / `PREPAY_MAX` at the top of `app.py`.

## Private files

Bank-deposit receipts and COD receipts are stored in `instance/private_uploads/` (not in `static/`) and are served only to
the admin, the customer who owns the order, or the assigned transporter. Back this folder up together with the database.

## Production notes

1. Point the domain `www.dadodelivery.com` to your server and serve it over HTTPS (`SESSION_COOKIE_SECURE=1`).
2. Use gunicorn + nginx (or similar):
   ```bash
   TRUST_PROXY=1 SESSION_COOKIE_SECURE=1 gunicorn -w 4 -b 127.0.0.1:8000 app:app
   ```
   Several workers are safe: the secret key is shared and database initialisation is locked.
3. Keep `dado.db`, `instance/` and `static/uploads/` on a persistent volume, or switch to PostgreSQL (`DATABASE_URL`).
4. For offline-tolerant maps, download Leaflet 1.9.4 and serve it from your own server (the checkout still works
   without the map: customers can use GPS or paste coordinates).

## Tech stack

- Python 3 + Flask
- Flask-SQLAlchemy (SQLite by default)
- Flask-Login
- Flask-Babel (Amharic translations in `translations/am`)
- Responsive CSS (mobile-first)
- Client-side cart via localStorage
- Geolocation API for order location

Built to work on all devices. Ready to deploy when you have a server.


## Map tiles (production)

For commercial use do **not** rely on the default Esri free tiles.

1. Create a [MapTiler](https://www.maptiler.com) account and API key.
2. Restrict the key to your domain (HTTP referrer).
3. Set on Render (or any host):

```bash
MAP_TILES_URL=https://api.maptiler.com/maps/streets-v4/256/{z}/{x}/{y}.png?key=YOUR_KEY
MAP_ATTRIBUTION=© MapTiler © OpenStreetMap
TRUST_PROXY=1
```

Without `MAP_TILES_URL`, the app falls back to Esri (development only).


## SMS / OTP (phone verification)

Registration and password reset can require an SMS code.

Set environment variables on Render (or locally):

```bash
# Enable (auto-on when SMS_PROVIDER is set; force off with OTP_ENABLED=0)
OTP_ENABLED=1
SMS_PROVIDER=console   # or: twilio | africastalking | http

# --- Twilio ---
# SMS_PROVIDER=twilio
# TWILIO_ACCOUNT_SID=ACxxxx
# TWILIO_AUTH_TOKEN=xxxx
# TWILIO_FROM=+1234567890

# --- Africa's Talking (East Africa) ---
# SMS_PROVIDER=africastalking
# AT_USERNAME=sandbox   # or your production username
# AT_API_KEY=xxxx
# AT_FROM=DADO         # optional sender ID

# --- Generic HTTP SMS gateway ---
# SMS_PROVIDER=http
# SMS_HTTP_URL=https://your-sms-api.example/send
# SMS_HTTP_TOKEN=optional-bearer-token
```

`SMS_PROVIDER=console` prints the code in the server logs (good for testing on free tier).
