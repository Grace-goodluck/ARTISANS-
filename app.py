import sqlite3
import os
import ssl
import urllib.parse
import uuid
import hmac
import hashlib
import bcrypt
import cloudinary
import cloudinary.uploader
import requests
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, make_response, jsonify
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

import re as _re

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ["SECRET_KEY"]

csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[])

@app.template_filter('wa_number')
def wa_number_filter(number):
    """Convert any Nigerian phone number to wa.me international format."""
    if not number:
        return ''
    digits = _re.sub(r'[^\d]', '', str(number))
    if digits.startswith('234'):
        return digits
    if digits.startswith('0') and len(digits) >= 10:
        return '234' + digits[1:]
    return digits

# ── Cloudinary config ──────────────────────────────────────────────────────────
cloudinary.config(
    cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key=os.environ["CLOUDINARY_API_KEY"],
    api_secret=os.environ["CLOUDINARY_API_SECRET"],
    secure=True
)

# ── Paystack config ────────────────────────────────────────────────────────────
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY")
PAYSTACK_BASE_URL = "https://api.paystack.co"

# ── Database config ────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")  # set on Vercel → PostgreSQL
UPLOAD_FOLDER = "/tmp/uploads" if os.environ.get('VERCEL') else "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def upload_image(file_obj):
    """Upload a file to Cloudinary and return its secure URL."""
    result = cloudinary.uploader.upload(file_obj, folder="artisaans-crib")
    return result['secure_url']


class _DictCursor:
    """Wraps a pg8000 cursor so rows are returned as dicts (like RealDictCursor)."""
    def __init__(self, cur):
        self._cur = cur

    def _to_dict(self, row):
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchone(self):
        return self._to_dict(self._cur.fetchone())

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in rows]

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _parse_db_url(raw):
    """Parse a PostgreSQL URL safely, handling special characters in passwords."""
    s = raw.strip()
    if '://' in s:
        s = s.split('://', 1)[1]
    # split off database name
    if '/' in s:
        hostpart, database = s.rsplit('/', 1)
    else:
        hostpart, database = s, 'postgres'
    # use rfind so @ inside passwords is handled correctly
    if '@' in hostpart:
        idx = hostpart.rfind('@')
        userinfo, hostinfo = hostpart[:idx], hostpart[idx+1:]
    else:
        userinfo, hostinfo = '', hostpart
    if ':' in hostinfo:
        host, port = hostinfo.rsplit(':', 1)
        port = int(port)
    else:
        host, port = hostinfo, 5432
    if ':' in userinfo:
        user, password = userinfo.split(':', 1)
    else:
        user, password = userinfo, ''
    return host, port, database, urllib.parse.unquote(user), urllib.parse.unquote(password)


def get_db_connection():
    if DATABASE_URL:
        import pg8000.dbapi
        host, port, database, user, password = _parse_db_url(DATABASE_URL)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = pg8000.dbapi.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            ssl_context=ssl_ctx,
        )
        return conn
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


def db_execute(conn, sql, params=()):
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute(sql.replace('?', '%s'), params)
        return _DictCursor(cur)
    return conn.execute(sql, params)


def db_insert(conn, sql, params=()):
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute(sql.replace('?', '%s') + ' RETURNING id', params)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))['id']
    conn.execute(sql, params)
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


ADMIN_EMAIL = "goodluckgrace08@gmail.com"


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def make_referral_code(name):
    import hashlib, time
    raw = f"{name}{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()[:8].upper()


def calc_badges(artisan, avg_rating, review_count, response_rate):
    badges = []
    if artisan.get('verified'):
        badges.append(('fa-circle-check', 'Verified', '#c49b3a'))
    if artisan.get('is_promoted'):
        badges.append(('fa-rocket', 'Promoted', '#e05c00'))
    if float(avg_rating or 0) >= 4.5 and review_count >= 3:
        badges.append(('fa-trophy', 'Top Rated', '#f0c040'))
    if int(artisan.get('view_count') or 0) >= 100:
        badges.append(('fa-fire', 'Popular', '#ff6b35'))
    if int(artisan.get('experience') or 0) >= 5:
        badges.append(('fa-medal', 'Experienced', '#aaa'))
    if response_rate is not None and response_rate >= 80:
        badges.append(('fa-bolt', 'Quick Responder', '#66dd66'))
    return badges


def send_email(to_email, subject, body):
    mail_user = os.environ.get('MAIL_USER')
    mail_pass = os.environ.get('MAIL_PASS')
    if not mail_user or not mail_pass or not to_email:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = mail_user
        msg['To'] = to_email
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(mail_user, mail_pass)
            s.send_message(msg)
    except Exception:
        pass


def paystack_initialize(email, amount_kobo, reference, callback_url, metadata=None):
    """Create a Paystack transaction. Returns the authorization_url on success, None on failure."""
    if not PAYSTACK_SECRET_KEY:
        return None
    try:
        resp = requests.post(
            f"{PAYSTACK_BASE_URL}/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={
                "email": email,
                "amount": amount_kobo,
                "currency": "NGN",
                "reference": reference,
                "callback_url": callback_url,
                "metadata": metadata or {},
            },
            timeout=15,
        )
        data = resp.json()
        if resp.ok and data.get("status"):
            return data["data"]["authorization_url"]
    except Exception:
        app.logger.exception("Paystack initialize failed")
    return None


def paystack_verify(reference):
    """Verify a Paystack transaction by reference. Returns the 'data' dict on success, None otherwise."""
    if not PAYSTACK_SECRET_KEY:
        return None
    try:
        resp = requests.get(
            f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            timeout=15,
        )
        data = resp.json()
        if resp.ok and data.get("status"):
            return data["data"]
    except Exception:
        app.logger.exception("Paystack verify failed")
    return None


def finalize_payment(reference):
    """Verify a payment with Paystack and, if successful, mark it paid and create the booking.
    Idempotent — safe to call from both the browser callback and the webhook."""
    conn = get_db_connection()
    payment = db_execute(conn, "SELECT * FROM payments WHERE reference=?", (reference,)).fetchone()
    if not payment:
        conn.close()
        return None
    payment = dict(payment)

    if payment["status"] == "success":
        conn.close()
        return payment

    verified = paystack_verify(reference)
    if not verified or verified.get("status") != "success" or int(verified.get("amount") or 0) != payment["amount_kobo"]:
        db_execute(conn, "UPDATE payments SET status=? WHERE reference=?", ("failed", reference))
        conn.commit()
        conn.close()
        return None

    artisan = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (payment["artisan_id"],)).fetchone()
    package = db_execute(conn, "SELECT * FROM packages WHERE id=?", (payment["package_id"],)).fetchone()
    artisan = dict(artisan) if artisan else None
    package = dict(package) if package else None

    booking_id = db_insert(conn,
        "INSERT INTO bookings (artisan_id, client_name, client_phone, client_email, note, status, amount_kobo, payment_reference) "
        "VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?)",
        (payment["artisan_id"], payment["client_name"], payment["client_phone"], payment["client_email"],
         f"Paid booking — {package['title'] if package else 'Service'} package", payment["amount_kobo"], reference))

    db_execute(conn, "UPDATE payments SET status='success', booking_id=?, verified_at=CURRENT_TIMESTAMP WHERE reference=?",
               (booking_id, reference))
    conn.commit()
    conn.close()

    if artisan and artisan.get('email'):
        send_email(
            artisan['email'],
            "New paid booking on Artisaan's Crib",
            f"Hello {artisan['name']},\n\n{payment['client_name']} just paid ₦{payment['amount_kobo']//100:,} "
            f"for your {package['title'] if package else 'service'} package and their booking is confirmed.\n\n"
            f"Phone: {payment['client_phone']}\nEmail: {payment['client_email']}\n\n"
            f"Log in to view it: https://artisans-crib.vercel.app/my-bookings"
        )

    payment["status"] = "success"
    payment["booking_id"] = booking_id
    return payment


def profile_completion(artisan):
    fields = ['name','email','skill','location','phone','description',
              'image','experience','certifications','availability',
              'price_range','service_area','whatsapp']
    filled = sum(1 for f in fields if artisan.get(f))
    return int(filled * 100 / len(fields))


def is_admin():
    if 'user_id' not in session:
        return False
    conn = get_db_connection()
    user = db_execute(conn, "SELECT email FROM users WHERE id=?", (session['user_id'],)).fetchone()
    conn.close()
    return bool(user and user['email'] == ADMIN_EMAIL)


@app.context_processor
def inject_is_platform_admin():
    return {'is_platform_admin': is_admin()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return redirect(url_for('home'))
        return view(*args, **kwargs)
    return wrapped


# ── No-cache header ────────────────────────────────────────────────────────────
@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ── Security headers ───────────────────────────────────────────────────────────
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net unpkg.com; "
    "style-src 'self' 'unsafe-inline' cdnjs.cloudflare.com unpkg.com; "
    "font-src 'self' cdnjs.cloudflare.com; "
    "img-src 'self' data: res.cloudinary.com; "
    "frame-src www.youtube.com; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=(), payment=()"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── Table creation ─────────────────────────────────────────────────────────────
def create_table():
    conn = get_db_connection()
    PK = "SERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS users (
            id {PK},
            name TEXT,
            email TEXT UNIQUE,
            password TEXT
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS artisans (
            id {PK},
            name TEXT,
            email TEXT,
            dob TEXT,
            gender TEXT,
            languages TEXT,
            skill TEXT,
            experience INTEGER,
            certifications TEXT,
            availability TEXT,
            price_range TEXT,
            location TEXT,
            service_area TEXT,
            phone TEXT,
            whatsapp TEXT,
            instagram TEXT,
            facebook TEXT,
            tiktok TEXT,
            twitter TEXT,
            youtube TEXT,
            website TEXT,
            description TEXT,
            custom_orders TEXT,
            marketing TEXT,
            image TEXT
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS reviews (
            id {PK},
            artisan_id INTEGER,
            rating INTEGER,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS portfolios (
            id {PK},
            artisan_id INTEGER,
            image TEXT
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS bookmarks (
            id {PK},
            user_id INTEGER,
            artisan_id INTEGER
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS messages (
            id {PK},
            artisan_id INTEGER,
            sender_name TEXT,
            sender_phone TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS reports (
            id {PK},
            artisan_id INTEGER,
            reporter_name TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS service_requests (
            id {PK},
            name TEXT,
            phone TEXT,
            skill_needed TEXT,
            location TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()  # commit initial table creations before alter loops

    for col, definition in [
        ("verified", "INTEGER DEFAULT 0"),
        ("user_id", "INTEGER"),
        ("view_count", "INTEGER DEFAULT 0"),
        ("is_available", "INTEGER DEFAULT 1"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE artisans ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("avatar", "TEXT"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE users ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("reply", "TEXT"),
        ("replied_at", "TIMESTAMP"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE reviews ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS bookings (
            id {PK},
            artisan_id INTEGER,
            client_name TEXT,
            client_phone TEXT,
            client_email TEXT,
            service_date TEXT,
            note TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS packages (
            id {PK},
            artisan_id INTEGER,
            tier TEXT,
            title TEXT,
            description TEXT,
            price TEXT,
            delivery_days INTEGER
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS faqs (
            id {PK},
            artisan_id INTEGER,
            question TEXT,
            answer TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS flagged_reviews (
            id {PK},
            review_id INTEGER,
            reporter_name TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS jobs (
            id {PK},
            title TEXT,
            skill_needed TEXT,
            location TEXT,
            budget TEXT,
            description TEXT,
            client_name TEXT,
            client_phone TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS job_interests (
            id {PK},
            job_id INTEGER,
            artisan_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS availability_slots (
            id {PK},
            artisan_id INTEGER,
            slot_date TEXT,
            is_available INTEGER DEFAULT 1,
            UNIQUE(artisan_id, slot_date)
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS verification_requests (
            id {PK},
            artisan_id INTEGER,
            id_type TEXT,
            notes TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS quote_requests (
            id {PK},
            artisan_id INTEGER,
            client_name TEXT,
            client_phone TEXT,
            client_email TEXT,
            service_needed TEXT,
            budget TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS conversations (
            id {PK},
            artisan_id INTEGER,
            client_name TEXT,
            client_phone TEXT,
            client_user_id INTEGER,
            last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id {PK},
            conversation_id INTEGER,
            sender TEXT,
            body TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    db_execute(conn, f'''
        CREATE TABLE IF NOT EXISTS payments (
            id {PK},
            reference TEXT UNIQUE,
            artisan_id INTEGER,
            package_id INTEGER,
            user_id INTEGER,
            client_name TEXT,
            client_email TEXT,
            client_phone TEXT,
            amount_kobo INTEGER,
            status TEXT DEFAULT 'pending',
            booking_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_at TIMESTAMP
        )
    ''')

    conn.commit()  # commit all new table creations before alter loops that may rollback

    for col, definition in [
        ("video_url", "TEXT"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE artisans ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("caption", "TEXT"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE portfolios ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("client_email", "TEXT"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE bookings ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("business_hours", "TEXT"),
        ("photo_url", "TEXT"),
        ("referral_code", "TEXT"),
        ("is_promoted", "INTEGER DEFAULT 0"),
        ("is_featured_week", "INTEGER DEFAULT 0"),
        ("away_message", "TEXT"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE artisans ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("photo_url", "TEXT"),
        ("reviewer_name", "TEXT"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE reviews ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("amount_kobo", "INTEGER"),
        ("payment_reference", "TEXT"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE bookings ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("is_suspended", "INTEGER DEFAULT 0"),
        ("suspended_reason", "TEXT"),
        ("suspended_at", "TIMESTAMP"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE users ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    for col, definition in [
        ("is_suspended", "INTEGER DEFAULT 0"),
    ]:
        try:
            db_execute(conn, f"ALTER TABLE artisans ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    conn.commit()
    conn.close()


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/debug-info")
@admin_required
def debug_info():
    info = {"DATABASE_URL_set": bool(DATABASE_URL)}
    try:
        import pg8000.dbapi
        info["pg8000"] = "imported ok"
    except Exception as e:
        info["pg8000"] = f"IMPORT ERROR: {e}"
    try:
        conn = get_db_connection()
        conn.close()
        info["db_connect"] = "ok"
    except Exception as e:
        info["db_connect"] = f"FAILED: {e}"
    return info


@app.route("/sw.js")
def service_worker():
    response = make_response(app.send_static_file("sw.js"))
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/")
def home():
    conn = get_db_connection()
    count = db_execute(conn, "SELECT COUNT(*) as c FROM artisans WHERE name IS NOT NULL AND name != ''").fetchone()
    artisan_count = count['c'] if count else 0
    categories = db_execute(conn, "SELECT DISTINCT skill FROM artisans WHERE skill IS NOT NULL AND skill != '' ORDER BY skill LIMIT 16").fetchall()
    skill_count = db_execute(conn, "SELECT COUNT(DISTINCT skill) as c FROM artisans WHERE skill IS NOT NULL AND skill != ''").fetchone()['c']
    testimonials = db_execute(conn, """
        SELECT r.rating, r.comment, a.name as artisan_name, a.skill as artisan_skill, a.image as artisan_image
        FROM reviews r JOIN artisans a ON a.id = r.artisan_id
        WHERE r.rating >= 4 AND r.comment IS NOT NULL AND r.comment != '' AND COALESCE(a.is_suspended, 0) = 0
        ORDER BY r.rating DESC, r.id DESC LIMIT 3
    """).fetchall()
    featured_artisan = db_execute(conn, """
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a WHERE a.is_featured_week=1 AND COALESCE(a.is_suspended, 0) = 0 LIMIT 1
    """).fetchone()
    trending_skills = db_execute(conn, """
        SELECT skill, COUNT(*) as c FROM artisans
        WHERE skill IS NOT NULL AND skill != ''
        GROUP BY skill ORDER BY c DESC LIMIT 8
    """).fetchall()
    interval_sql = "NOW() - INTERVAL '30 days'" if DATABASE_URL else "datetime('now', '-30 days')"
    artisan_of_month = db_execute(conn, f"""
        SELECT a.*,
            COUNT(r.id) as month_reviews,
            COALESCE(AVG(r.rating), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r2 WHERE r2.artisan_id=a.id), 0) as review_count
        FROM artisans a
        JOIN reviews r ON r.artisan_id = a.id
        WHERE r.created_at >= {interval_sql} AND a.name IS NOT NULL AND a.name != '' AND COALESCE(a.is_suspended, 0) = 0
        GROUP BY a.id
        ORDER BY month_reviews DESC, avg_rating DESC
        LIMIT 1
    """).fetchone()
    conn.close()
    return render_template("home.html", artisan_count=artisan_count, categories=categories,
                           skill_count=skill_count, testimonials=testimonials,
                           featured_artisan=featured_artisan, trending_skills=trending_skills,
                           artisan_of_month=artisan_of_month)


@app.route("/artisans")
def artisans():
    search          = request.args.get('search', '').strip()
    sort            = request.args.get('sort', 'newest')
    location_filter = request.args.get('location', '').strip()
    rating_filter   = request.args.get('rating', '').strip()
    avail_filter    = request.args.get('avail', '').strip()
    price_filter    = request.args.get('price', '').strip()
    page            = max(1, int(request.args.get('page', 1)))
    per_page        = 12

    conn = get_db_connection()
    rating_sql = """
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a
    """
    op = "ILIKE" if DATABASE_URL else "LIKE"
    conditions = ["a.name IS NOT NULL", "a.name != ''", "COALESCE(a.is_suspended, 0) = 0"]
    params = []

    if search:
        like = '%' + search + '%'
        conditions.append(f"(a.skill {op} ? OR a.location {op} ? OR a.name {op} ?)")
        params.extend([like, like, like])

    if location_filter:
        conditions.append(f"a.location {op} ?")
        params.append('%' + location_filter + '%')

    if rating_filter:
        conditions.append(
            "(SELECT COALESCE(AVG(r.rating),0) FROM reviews r WHERE r.artisan_id=a.id) >= ?"
        )
        params.append(float(rating_filter))

    if avail_filter in ('0', '1'):
        conditions.append("COALESCE(a.is_available, 1) = ?")
        params.append(int(avail_filter))

    if price_filter:
        conditions.append(f"a.price_range IS NOT NULL AND a.price_range != '' AND a.price_range {op} ?")
        params.append('%' + price_filter + '%')

    where = " WHERE " + " AND ".join(conditions)
    all_rows = db_execute(conn, rating_sql + where, tuple(params)).fetchall()

    # featured (verified) artisans float to top, then sort within groups
    if sort == 'rating':
        key_fn = lambda r: float(r['avg_rating'] or 0)
    elif sort == 'views':
        key_fn = lambda r: int(r['view_count'] or 0)
    else:
        key_fn = lambda r: int(r['id'])

    featured = sorted([r for r in all_rows if r.get('verified')], key=key_fn, reverse=True)
    regular  = sorted([r for r in all_rows if not r.get('verified')], key=key_fn, reverse=True)
    all_rows = featured + regular

    total = len(all_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = all_rows[(page-1)*per_page : page*per_page]

    # distinct states for location filter dropdown
    loc_rows = db_execute(conn, "SELECT DISTINCT location FROM artisans WHERE location IS NOT NULL AND location != ''").fetchall()
    states = sorted(set(
        r['location'].split(',')[-1].strip() for r in loc_rows
        if r['location'] and ',' in r['location']
    ))

    skills = db_execute(conn, "SELECT DISTINCT skill FROM artisans WHERE skill IS NOT NULL AND skill != '' ORDER BY skill").fetchall()
    skill_list = [r['skill'] for r in skills]

    conn.close()
    return render_template("artisans.html", artisans=rows, search=search, sort=sort,
                           page=page, total_pages=total_pages, total=total,
                           location_filter=location_filter, states=states,
                           rating_filter=rating_filter, avail_filter=avail_filter,
                           price_filter=price_filter, skill_list=skill_list)


@app.route("/add-artisan", methods=["GET", "POST"])
@login_required
def add_artisan():

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        profile_pic = request.files["profile_pic"]

        if not profile_pic or not allowed_file(profile_pic.filename):
            return render_template("add_artisan.html", error="Invalid file type. Allowed: png, jpg, jpeg, gif, webp")

        image_url = upload_image(profile_pic)

        skill        = request.form["skill"]
        city         = request.form["city"]
        state        = request.form["state"]
        location     = city + ", " + state
        dob          = request.form.get("dob", "")
        gender       = request.form.get("gender", "")
        languages    = request.form.get("languages", "")
        experience   = request.form.get("experience", "")
        certifications = request.form.get("certifications", "")
        availability = request.form.get("availability", "")
        price_range    = request.form.get("price_range", "")
        service_area   = request.form.get("service_area", "")
        business_hours = request.form.get("business_hours", "")
        phone        = request.form.get("phone", "")
        whatsapp     = request.form.get("whatsapp", "")
        instagram    = request.form.get("instagram", "")
        facebook     = request.form.get("facebook", "")
        tiktok       = request.form.get("tiktok", "")
        twitter      = request.form.get("twitter", "")
        youtube      = request.form.get("youtube", "")
        website      = request.form.get("website", "")
        description  = request.form.get("description", "")
        custom_orders = request.form.get("custom_orders", "")
        marketing    = ", ".join(request.form.getlist("marketing[]"))

        conn = get_db_connection()

        existing = db_execute(conn,
            "SELECT id FROM artisans WHERE LOWER(name)=LOWER(?) AND LOWER(skill)=LOWER(?) AND phone=?",
            (name.strip(), skill.strip(), phone.strip())
        ).fetchone()

        if existing:
            conn.close()
            return render_template("add_artisan.html", error="An artisan with that name, skill, and phone number already exists.")

        artisan_id = db_insert(conn,
            "INSERT INTO artisans (name,email,dob,gender,languages,skill,experience,certifications,availability,price_range,location,service_area,phone,whatsapp,instagram,facebook,tiktok,twitter,youtube,website,description,custom_orders,marketing,image,user_id,business_hours) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name,email,dob,gender,languages,skill,experience,certifications,availability,price_range,location,service_area,phone,whatsapp,instagram,facebook,tiktok,twitter,youtube,website,description,custom_orders,marketing,image_url,session.get('user_id'),business_hours)
        )

        for photo in request.files.getlist("portfolio")[:5]:
            if photo and photo.filename and allowed_file(photo.filename):
                photo_url = upload_image(photo)
                db_execute(conn, "INSERT INTO portfolios (artisan_id, image) VALUES (?, ?)", (artisan_id, photo_url))

        conn.commit()
        conn.close()
        return redirect(url_for('artisans'))

    return render_template("add_artisan.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        fullname = request.form.get("fullname", "").strip()
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not fullname or not email or not password:
            return render_template("register.html", error="Please fill in all fields.")
        if len(password.encode('utf-8')) > 72:
            return render_template("register.html", error="Password must be 72 characters or fewer.")

        try:
            hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            conn = get_db_connection()
        except Exception:
            app.logger.exception("Failed to prepare registration")
            return render_template("register.html", error="Something went wrong. Please try again.")

        try:
            db_execute(conn, "INSERT INTO users (name, email, password) VALUES (?, ?, ?)", (fullname, email, hashed))
            conn.commit()
            user = db_execute(conn, "SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        except Exception:
            conn.rollback()
            conn.close()
            return render_template("register.html", error="An account with that email already exists.")
        conn.close()
        if user:
            session["user_id"] = user["id"]
        return redirect(url_for('home'))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        try:
            conn = get_db_connection()
            user = db_execute(conn, "SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            conn.close()
        except Exception:
            app.logger.exception("Failed to look up user during login")
            return render_template("login.html", error="Something went wrong. Please try again.")

        if user:
            stored = user["password"]
            if isinstance(stored, memoryview):
                stored = bytes(stored)
            if isinstance(stored, str):
                stored = stored.encode('utf-8')
            try:
                match = bcrypt.checkpw(password.encode('utf-8'), stored)
            except Exception:
                match = False
            if match:
                if user["is_suspended"]:
                    return render_template("login.html", error="This account has been suspended. Contact support if you think this is a mistake.")
                session["user_id"] = user["id"]
                return redirect(url_for('home'))
            return render_template("login.html", error="Incorrect password")
        return render_template("login.html", error="User not found")

    return render_template("login.html")


@app.route("/users")
@admin_required
def show_users():
    conn = get_db_connection()
    users = db_execute(conn, "SELECT * FROM users").fetchall()
    conn.close()
    return render_template("users.html", users=users, message=request.args.get("message"))


@app.route("/delete/<int:id>", methods=["POST"])
@admin_required
def delete_user(id):
    conn = get_db_connection()
    _cascade_delete_user(conn, id)
    conn.commit()
    conn.close()
    return redirect(url_for('show_users'))


@app.route("/edit/<int:id>", methods=["GET", "POST"])
@admin_required
def edit_user(id):
    conn = get_db_connection()
    user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (id,)).fetchone()

    if request.method == "POST":
        db_execute(conn, "UPDATE users SET name=?, email=? WHERE id=?",
                   (request.form["name"], request.form["email"], id))
        conn.commit()
        conn.close()
        return redirect(url_for('show_users'))

    conn.close()
    return render_template("edit.html", user=user)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/artisan/<int:id>', methods=['GET', 'POST'])
def artisan_profile(id):
    conn = get_db_connection()

    if request.method == 'POST':
        photo_url = None
        photo = request.files.get('review_photo')
        if photo and photo.filename and allowed_file(photo.filename):
            photo_url = upload_image(photo)
        reviewer_name = request.form.get('reviewer_name', '').strip()
        db_execute(conn,
            "INSERT INTO reviews (artisan_id, rating, comment, reviewer_name, photo_url) VALUES (?, ?, ?, ?, ?)",
            (id, request.form['rating'], request.form['comment'], reviewer_name, photo_url))
        conn.commit()
        artisan_row = db_execute(conn, "SELECT name, email FROM artisans WHERE id=?", (id,)).fetchone()
        conn.close()
        if artisan_row and artisan_row.get('email'):
            send_email(
                artisan_row['email'],
                f"New review on Artisaan's Crib",
                f"Hello {artisan_row['name']},\n\nYou received a new {request.form['rating']}-star review on Artisaan's Crib.\n\n"
                f"View it: https://artisans-crib.vercel.app/artisan/{id}"
            )
        return redirect(url_for('artisan_profile', id=id))

    artisan_row = db_execute(conn, "SELECT * FROM artisans WHERE id = ?", (id,)).fetchone()
    if artisan_row is None:
        conn.close()
        return render_template('404.html'), 404
    artisan = dict(artisan_row)
    if artisan.get('is_suspended') and session.get('user_id') != artisan.get('user_id') and not is_admin():
        conn.close()
        return render_template('404.html'), 404

    # increment view count (skip if viewer is the artisan owner)
    if session.get('user_id') != artisan.get('user_id'):
        db_execute(conn, "UPDATE artisans SET view_count = COALESCE(view_count, 0) + 1 WHERE id = ?", (id,))
        conn.commit()
        artisan['view_count'] = int(artisan.get('view_count') or 0) + 1

    reviews   = db_execute(conn, "SELECT * FROM reviews WHERE artisan_id=? ORDER BY created_at DESC", (id,)).fetchall()
    portfolio = db_execute(conn, "SELECT image FROM portfolios WHERE artisan_id=?", (id,)).fetchall()
    related   = db_execute(conn, """
        SELECT id, name, skill, location, image, is_available FROM artisans
        WHERE skill=? AND id!=? AND name IS NOT NULL AND name != '' LIMIT 3
    """, (artisan['skill'], id)).fetchall()
    packages  = db_execute(conn, "SELECT * FROM packages WHERE artisan_id=? ORDER BY id", (id,)).fetchall()
    faqs      = db_execute(conn, "SELECT * FROM faqs WHERE artisan_id=? ORDER BY id", (id,)).fetchall()
    avail_slots = db_execute(conn, "SELECT slot_date, is_available FROM availability_slots WHERE artisan_id=? ORDER BY slot_date", (id,)).fetchall()
    is_bookmarked = False
    if 'user_id' in session:
        bm = db_execute(conn, "SELECT id FROM bookmarks WHERE user_id=? AND artisan_id=?", (session['user_id'], id)).fetchone()
        is_bookmarked = bool(bm)
    saves_count = db_execute(conn, "SELECT COUNT(*) as c FROM bookmarks WHERE artisan_id=?", (id,)).fetchone()['c']
    conn.close()

    review_count   = len(reviews)
    average_rating = round(sum(r['rating'] for r in reviews) / review_count, 1) if reviews else 0
    replied_count  = sum(1 for r in reviews if r.get('reply'))
    response_rate  = int(replied_count * 100 / review_count) if review_count > 0 else None
    rating_breakdown = {5:0, 4:0, 3:0, 2:0, 1:0}
    for rv in reviews:
        rating_breakdown[int(rv['rating'])] = rating_breakdown.get(int(rv['rating']), 0) + 1
    contacted  = request.args.get('contacted') == '1'
    reported   = request.args.get('reported') == '1'
    completion = profile_completion(artisan)
    badges     = calc_badges(artisan, average_rating, review_count, response_rate)
    profile_url = request.host_url.rstrip('/') + f'/artisan/{id}'
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={urllib.parse.quote(profile_url)}"

    return render_template('artisan_profile_new.html',
        artisan=artisan, reviews=reviews,
        average_rating=average_rating, review_count=review_count,
        portfolio=portfolio, is_bookmarked=is_bookmarked,
        contacted=contacted, reported=reported,
        completion=completion, related=related,
        response_rate=response_rate, saves_count=saves_count,
        badges=badges, packages=packages, faqs=faqs,
        qr_url=qr_url, profile_url=profile_url,
        rating_breakdown=rating_breakdown, avail_slots=avail_slots)


@app.route('/api/suggestions')
def suggestions():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    conn = get_db_connection()
    op = "ILIKE" if DATABASE_URL else "LIKE"
    like = '%' + q + '%'
    skills = db_execute(conn, f"SELECT DISTINCT skill FROM artisans WHERE skill {op} ? LIMIT 5", (like,)).fetchall()
    names  = db_execute(conn, f"SELECT DISTINCT name  FROM artisans WHERE name  {op} ? LIMIT 3", (like,)).fetchall()
    locs   = db_execute(conn, f"SELECT DISTINCT location FROM artisans WHERE location {op} ? LIMIT 3", (like,)).fetchall()
    conn.close()
    results = list({r['skill'] for r in skills} | {r['name'] for r in names} | {r['location'] for r in locs if r['location']})[:8]
    return jsonify(sorted(results))


@app.route('/artisan/<int:id>/toggle-availability', methods=['POST'])
@login_required
def toggle_availability(id):
    conn = get_db_connection()
    a = db_execute(conn, "SELECT is_available, user_id FROM artisans WHERE id=?", (id,)).fetchone()
    if a and a['user_id'] == session['user_id']:
        db_execute(conn, "UPDATE artisans SET is_available=? WHERE id=?", (0 if a['is_available'] else 1, id))
        conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id))


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/notifications')
@login_required
def notifications():
    conn = get_db_connection()
    my_artisan = db_execute(conn, "SELECT id, name FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    if not my_artisan:
        conn.close()
        return render_template('notifications.html', messages=[], reviews=[], my_artisan=None)
    msgs    = db_execute(conn, "SELECT * FROM messages WHERE artisan_id=? ORDER BY created_at DESC", (my_artisan['id'],)).fetchall()
    reviews = db_execute(conn, "SELECT * FROM reviews WHERE artisan_id=? ORDER BY created_at DESC", (my_artisan['id'],)).fetchall()
    conn.close()
    return render_template('notifications.html', messages=msgs, reviews=reviews, my_artisan=my_artisan, session=session)


@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    my_artisan = db_execute(conn, "SELECT * FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    if not my_artisan:
        conn.close()
        return redirect(url_for('add_artisan'))
    artisan    = dict(my_artisan)
    msg_count      = db_execute(conn, "SELECT COUNT(*) as c FROM messages WHERE artisan_id=?", (artisan['id'],)).fetchone()['c']
    rev_count      = db_execute(conn, "SELECT COUNT(*) as c FROM reviews  WHERE artisan_id=?", (artisan['id'],)).fetchone()['c']
    saves_count    = db_execute(conn, "SELECT COUNT(*) as c FROM bookmarks WHERE artisan_id=?", (artisan['id'],)).fetchone()['c']
    book_count     = db_execute(conn, "SELECT COUNT(*) as c FROM bookings WHERE artisan_id=? AND status='pending'", (artisan['id'],)).fetchone()['c']
    completed_jobs = db_execute(conn, "SELECT COUNT(*) as c FROM bookings WHERE artisan_id=? AND status='completed'", (artisan['id'],)).fetchone()['c']
    avg_row        = db_execute(conn, "SELECT COALESCE(AVG(rating),0) as avg FROM reviews WHERE artisan_id=?", (artisan['id'],)).fetchone()
    avg_rating     = round(float(avg_row['avg'] or 0), 1)
    replied        = db_execute(conn, "SELECT COUNT(*) as c FROM reviews WHERE artisan_id=? AND reply IS NOT NULL AND reply != ''", (artisan['id'],)).fetchone()['c']
    response_rate  = int(replied * 100 / rev_count) if rev_count > 0 else None
    recent_msgs    = db_execute(conn, "SELECT * FROM messages WHERE artisan_id=? ORDER BY created_at DESC LIMIT 5", (artisan['id'],)).fetchall()
    # ensure referral code exists
    if not artisan.get('referral_code'):
        code = make_referral_code(artisan['name'])
        db_execute(conn, "UPDATE artisans SET referral_code=? WHERE id=?", (code, artisan['id']))
        conn.commit()
        artisan['referral_code'] = code
    has_portfolio = db_execute(conn, "SELECT COUNT(*) as c FROM portfolios WHERE artisan_id=?", (artisan['id'],)).fetchone()['c'] > 0
    completion_pct = profile_completion(artisan)
    if has_portfolio:
        completion_pct = min(100, completion_pct + 8)
    completion_tips = []
    for f, label in [('email','Add your email'),('phone','Add your phone number'),
                     ('description','Write an About section'),('image','Upload a profile photo'),
                     ('experience','Add years of experience'),('price_range','Set your price range'),
                     ('whatsapp','Add WhatsApp number'),('certifications','List certifications'),
                     ('skill','Add your skill/trade'),('location','Add your location')]:
        if not artisan.get(f):
            completion_tips.append(label)
    if not has_portfolio:
        completion_tips.append('Upload portfolio photos')
    pending_bookings = db_execute(conn, "SELECT * FROM bookings WHERE artisan_id=? AND status='pending' ORDER BY created_at DESC LIMIT 5", (artisan['id'],)).fetchall()
    quote_reqs = db_execute(conn, "SELECT * FROM quote_requests WHERE artisan_id=? ORDER BY created_at DESC LIMIT 5", (artisan['id'],)).fetchall()

    # Analytics: reviews & bookings per month (last 6 months)
    if DATABASE_URL:
        month_fn = "TO_CHAR(DATE_TRUNC('month', created_at), 'Mon YYYY')"
        month_key = "TO_CHAR(DATE_TRUNC('month', created_at), 'YYYY-MM')"
    else:
        month_fn = "strftime('%b %Y', created_at)"
        month_key = "strftime('%Y-%m', created_at)"

    rev_monthly = db_execute(conn, f"""
        SELECT {month_fn} as month, {month_key} as mkey, COUNT(*) as cnt
        FROM reviews WHERE artisan_id=?
        GROUP BY mkey, month ORDER BY mkey DESC LIMIT 6
    """, (artisan['id'],)).fetchall()
    book_monthly = db_execute(conn, f"""
        SELECT {month_fn} as month, {month_key} as mkey, COUNT(*) as cnt
        FROM bookings WHERE artisan_id=?
        GROUP BY mkey, month ORDER BY mkey DESC LIMIT 6
    """, (artisan['id'],)).fetchall()
    rating_dist = db_execute(conn, """
        SELECT rating, COUNT(*) as cnt FROM reviews
        WHERE artisan_id=? GROUP BY rating ORDER BY rating
    """, (artisan['id'],)).fetchall()

    import json as _json
    rev_labels  = [r['month'] for r in reversed(rev_monthly)]
    rev_data    = [r['cnt']   for r in reversed(rev_monthly)]
    book_labels = [r['month'] for r in reversed(book_monthly)]
    book_data   = [r['cnt']   for r in reversed(book_monthly)]
    rating_labels = [f"{r['rating']}★" for r in rating_dist]
    rating_data   = [r['cnt'] for r in rating_dist]

    conn.close()
    referral_url = request.host_url.rstrip('/') + '/r/' + artisan['referral_code']
    return render_template('dashboard.html', artisan=artisan, msg_count=msg_count,
                           review_count=rev_count, avg_rating=avg_rating, recent_msgs=recent_msgs,
                           saves_count=saves_count, book_count=book_count, response_rate=response_rate,
                           completed_jobs=completed_jobs, referral_url=referral_url,
                           completion_pct=completion_pct, completion_tips=completion_tips,
                           pending_bookings=pending_bookings, quote_reqs=quote_reqs,
                           rev_labels=_json.dumps(rev_labels), rev_data=_json.dumps(rev_data),
                           book_labels=_json.dumps(book_labels), book_data=_json.dumps(book_data),
                           rating_labels=_json.dumps(rating_labels), rating_data=_json.dumps(rating_data))


@app.route('/artisan/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_artisan(id):

    conn = get_db_connection()
    artisan_row = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (id,)).fetchone()
    if artisan_row is None:
        conn.close()
        return render_template('404.html'), 404
    artisan = dict(artisan_row)

    if request.method == 'POST':
        fields = ['name','email','dob','gender','languages','skill','experience',
                  'certifications','availability','price_range','service_area',
                  'phone','whatsapp','instagram','facebook','tiktok','twitter',
                  'youtube','website','description','custom_orders','business_hours','video_url','away_message']
        u = {f: request.form.get(f, '') for f in fields}
        city  = request.form.get('city', '')
        state = request.form.get('state', '')
        u['location']  = city + ', ' + state if (city or state) else artisan['location']
        u['marketing'] = ', '.join(request.form.getlist('marketing[]'))

        pic = request.files.get('profile_pic')
        if pic and pic.filename and allowed_file(pic.filename):
            u['image'] = upload_image(pic)
        else:
            u['image'] = artisan['image']

        db_execute(conn, '''UPDATE artisans SET
            name=?,email=?,dob=?,gender=?,languages=?,skill=?,experience=?,
            certifications=?,availability=?,price_range=?,location=?,service_area=?,
            phone=?,whatsapp=?,instagram=?,facebook=?,tiktok=?,twitter=?,
            youtube=?,website=?,description=?,custom_orders=?,marketing=?,image=?,business_hours=?,video_url=?,away_message=?
            WHERE id=?''',
            (u['name'],u['email'],u['dob'],u['gender'],u['languages'],u['skill'],u['experience'],
             u['certifications'],u['availability'],u['price_range'],u['location'],u['service_area'],
             u['phone'],u['whatsapp'],u['instagram'],u['facebook'],u['tiktok'],u['twitter'],
             u['youtube'],u['website'],u['description'],u['custom_orders'],u['marketing'],u['image'],
             u['business_hours'],u['video_url'],u['away_message'],id))

        for photo in request.files.getlist('portfolio')[:5]:
            if photo and photo.filename and allowed_file(photo.filename):
                photo_url = upload_image(photo)
                db_execute(conn, "INSERT INTO portfolios (artisan_id, image) VALUES (?,?)", (id, photo_url))

        conn.commit()
        conn.close()
        return redirect(url_for('artisan_profile', id=id))

    portfolio = db_execute(conn, "SELECT id, image FROM portfolios WHERE artisan_id=?", (id,)).fetchall()
    conn.close()
    parts = (artisan.get('location') or '').split(', ', 1)
    return render_template('edit_artisan.html', artisan=artisan, portfolio=portfolio,
                           city=parts[0] if parts else '', state=parts[1] if len(parts) > 1 else '')


@app.route('/artisan/<int:id>/delete-photo/<int:photo_id>', methods=['POST'])
@login_required
def delete_portfolio_photo(id, photo_id):
    conn = get_db_connection()
    photo = db_execute(conn, "SELECT image FROM portfolios WHERE id=? AND artisan_id=?", (photo_id, id)).fetchone()
    if photo:
        db_execute(conn, "DELETE FROM portfolios WHERE id=?", (photo_id,))
        conn.commit()
    conn.close()
    return redirect(url_for('edit_artisan', id=id))


@app.route('/forgot-password', methods=['GET'])
def forgot_password():
    return render_template('forgot_password.html')


@app.route('/account', methods=['GET', 'POST'])
@login_required
@limiter.limit("20 per hour", methods=["POST"])
def account():
    conn = get_db_connection()
    user = db_execute(conn, "SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
    my_artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    saved_count = db_execute(conn, "SELECT COUNT(*) as c FROM bookmarks WHERE user_id=?", (session['user_id'],)).fetchone()['c']
    message = ""
    msg_type = ""

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_profile':
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip()
            try:
                db_execute(conn, "UPDATE users SET name=?, email=? WHERE id=?", (name, email, session['user_id']))
                conn.commit()
                message = "Profile updated successfully!"
                msg_type = "success"
                user = db_execute(conn, "SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
            except Exception:
                try: conn.rollback()
                except: pass
                message = "That email is already in use."
                msg_type = "error"

        elif action == 'update_avatar':
            pic = request.files.get('avatar')
            if pic and pic.filename and allowed_file(pic.filename):
                url = upload_image(pic)
                db_execute(conn, "UPDATE users SET avatar=? WHERE id=?", (url, session['user_id']))
                conn.commit()
                message = "Profile picture updated!"
                msg_type = "success"
                user = db_execute(conn, "SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
            else:
                message = "Please select a valid image (png, jpg, jpeg, gif, webp)."
                msg_type = "error"

        elif action == 'change_password':
            current = request.form.get('current_password', '')
            new_pass = request.form.get('new_password', '')
            stored = user['password']
            if isinstance(stored, memoryview): stored = bytes(stored)
            if isinstance(stored, str): stored = stored.encode('utf-8')
            try:
                if bcrypt.checkpw(current.encode('utf-8'), stored):
                    hashed = bcrypt.hashpw(new_pass.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                    db_execute(conn, "UPDATE users SET password=? WHERE id=?", (hashed, session['user_id']))
                    conn.commit()
                    message = "Password changed successfully!"
                    msg_type = "success"
                else:
                    message = "Current password is incorrect."
                    msg_type = "error"
            except Exception:
                message = "Password change failed."
                msg_type = "error"

        elif action == 'delete_account':
            uid = session['user_id']
            # delete all user data
            db_execute(conn, "DELETE FROM bookmarks WHERE user_id=?", (uid,))
            artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (uid,)).fetchone()
            if artisan:
                aid = artisan['id']
                for sql in ["DELETE FROM reviews WHERE artisan_id=?",
                            "DELETE FROM portfolios WHERE artisan_id=?",
                            "DELETE FROM messages WHERE artisan_id=?",
                            "DELETE FROM artisans WHERE id=?"]:
                    db_execute(conn, sql, (aid,))
            db_execute(conn, "DELETE FROM users WHERE id=?", (uid,))
            conn.commit()
            conn.close()
            session.clear()
            return redirect(url_for('home'))

    conn.close()
    return render_template('account.html', user=user, my_artisan=my_artisan,
                           message=message, msg_type=msg_type, saved_count=saved_count)


@app.route('/artisan/<int:id>/bookmark', methods=['POST'])
@login_required
def toggle_bookmark(id):
    conn = get_db_connection()
    existing = db_execute(conn, "SELECT id FROM bookmarks WHERE user_id=? AND artisan_id=?", (session['user_id'], id)).fetchone()
    if existing:
        db_execute(conn, "DELETE FROM bookmarks WHERE user_id=? AND artisan_id=?", (session['user_id'], id))
    else:
        db_execute(conn, "INSERT INTO bookmarks (user_id, artisan_id) VALUES (?,?)", (session['user_id'], id))
    conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id))


@app.route('/saved')
@login_required
def saved_artisans():
    conn = get_db_connection()
    rows = db_execute(conn, """
        SELECT a.* FROM artisans a
        JOIN bookmarks b ON b.artisan_id = a.id
        WHERE b.user_id = ?
        ORDER BY b.id DESC
    """, (session['user_id'],)).fetchall()
    conn.close()
    return render_template('saved.html', artisans=rows)


@app.route('/artisan/<int:id>/message', methods=['POST'])
def send_message(id):
    sender_name  = request.form.get('sender_name', '')
    sender_phone = request.form.get('sender_phone', '')
    message      = request.form.get('message', '')
    conn = get_db_connection()
    db_execute(conn, "INSERT INTO messages (artisan_id, sender_name, sender_phone, message) VALUES (?,?,?,?)",
        (id, sender_name, sender_phone, message))
    conn.commit()
    artisan = db_execute(conn, "SELECT name, email FROM artisans WHERE id=?", (id,)).fetchone()
    conn.close()
    if artisan and artisan.get('email'):
        send_email(
            artisan['email'],
            f"New inquiry on Artisaan's Crib from {sender_name}",
            f"Hello {artisan['name']},\n\nYou have a new inquiry on Artisaan's Crib.\n\n"
            f"From: {sender_name}\nPhone: {sender_phone}\nMessage:\n{message}\n\n"
            f"Log in to view it: https://artisans-crib.vercel.app/notifications"
        )
    return redirect(url_for('artisan_profile', id=id) + '?contacted=1')


@app.route('/admin')
@admin_required
def admin_panel():
    conn = get_db_connection()
    artisans = db_execute(conn, """
        SELECT a.*,
            COALESCE((SELECT COUNT(*) FROM reports rp WHERE rp.artisan_id=a.id), 0) as report_count
        FROM artisans a ORDER BY a.is_promoted DESC, a.id DESC
    """).fetchall()
    users = db_execute(conn, "SELECT * FROM users ORDER BY id DESC").fetchall()
    msgs = db_execute(conn, """
        SELECT m.*, a.name as artisan_name FROM messages m
        LEFT JOIN artisans a ON a.id = m.artisan_id
        ORDER BY m.created_at DESC
    """).fetchall()

    # ── Platform metrics ──────────────────────────────────────────────
    total_users     = db_execute(conn, "SELECT COUNT(*) as c FROM users").fetchone()['c']
    total_artisans  = db_execute(conn, "SELECT COUNT(*) as c FROM artisans WHERE name IS NOT NULL AND name != ''").fetchone()['c']
    verified_count  = db_execute(conn, "SELECT COUNT(*) as c FROM artisans WHERE verified=1").fetchone()['c']
    suspended_users = db_execute(conn, "SELECT COUNT(*) as c FROM users WHERE is_suspended=1").fetchone()['c']
    total_bookings     = db_execute(conn, "SELECT COUNT(*) as c FROM bookings").fetchone()['c']
    completed_bookings = db_execute(conn, "SELECT COUNT(*) as c FROM bookings WHERE status='completed'").fetchone()['c']
    revenue_row = db_execute(conn, "SELECT COALESCE(SUM(amount_kobo),0) as total, COUNT(*) as cnt FROM payments WHERE status='success'").fetchone()
    total_revenue_naira = int(revenue_row['total'] or 0) / 100
    total_transactions  = revenue_row['cnt']
    pending_verifications = db_execute(conn, "SELECT COUNT(*) as c FROM verification_requests WHERE status='pending'").fetchone()['c']
    open_reports    = db_execute(conn, "SELECT COUNT(*) as c FROM reports").fetchone()['c']
    flagged_count   = db_execute(conn, "SELECT COUNT(*) as c FROM flagged_reviews").fetchone()['c']

    # ── Monthly trends (last 6 months) ────────────────────────────────
    if DATABASE_URL:
        month_fn = "TO_CHAR(DATE_TRUNC('month', created_at), 'Mon YYYY')"
        month_key = "TO_CHAR(DATE_TRUNC('month', created_at), 'YYYY-MM')"
    else:
        month_fn = "strftime('%b %Y', created_at)"
        month_key = "strftime('%Y-%m', created_at)"

    signups_monthly = db_execute(conn, f"""
        SELECT {month_fn} as month, {month_key} as mkey, COUNT(*) as cnt
        FROM users GROUP BY mkey, month ORDER BY mkey DESC LIMIT 6
    """).fetchall()
    revenue_monthly = db_execute(conn, f"""
        SELECT {month_fn} as month, {month_key} as mkey, COALESCE(SUM(amount_kobo),0) as total
        FROM payments WHERE status='success' GROUP BY mkey, month ORDER BY mkey DESC LIMIT 6
    """).fetchall()
    bookings_monthly = db_execute(conn, f"""
        SELECT {month_fn} as month, {month_key} as mkey, COUNT(*) as cnt
        FROM bookings GROUP BY mkey, month ORDER BY mkey DESC LIMIT 6
    """).fetchall()

    import json as _json
    signup_labels  = [r['month'] for r in reversed(signups_monthly)]
    signup_data    = [r['cnt']   for r in reversed(signups_monthly)]
    revenue_labels = [r['month'] for r in reversed(revenue_monthly)]
    revenue_data   = [round(int(r['total'] or 0) / 100, 2) for r in reversed(revenue_monthly)]
    bookings_labels = [r['month'] for r in reversed(bookings_monthly)]
    bookings_data    = [r['cnt']   for r in reversed(bookings_monthly)]

    recent_signups = db_execute(conn, "SELECT id, name, email, created_at, is_suspended FROM users ORDER BY id DESC LIMIT 8").fetchall()
    recent_payments = db_execute(conn, """
        SELECT p.*, a.name as artisan_name FROM payments p
        LEFT JOIN artisans a ON a.id=p.artisan_id
        WHERE p.status='success' ORDER BY p.verified_at DESC LIMIT 8
    """).fetchall()

    conn.close()
    return render_template('admin.html', artisans=artisans, users=users, msgs=msgs,
        total_users=total_users, total_artisans=total_artisans, verified_count=verified_count,
        suspended_users=suspended_users, total_bookings=total_bookings, completed_bookings=completed_bookings,
        total_revenue_naira=total_revenue_naira, total_transactions=total_transactions,
        pending_verifications=pending_verifications, open_reports=open_reports, flagged_count=flagged_count,
        recent_signups=recent_signups, recent_payments=recent_payments,
        signup_labels=_json.dumps(signup_labels), signup_data=_json.dumps(signup_data),
        revenue_labels=_json.dumps(revenue_labels), revenue_data=_json.dumps(revenue_data),
        bookings_labels=_json.dumps(bookings_labels), bookings_data=_json.dumps(bookings_data))


@app.route('/admin/verify/<int:id>', methods=['POST'])
@admin_required
def verify_artisan(id):
    conn = get_db_connection()
    a = db_execute(conn, "SELECT verified FROM artisans WHERE id=?", (id,)).fetchone()
    db_execute(conn, "UPDATE artisans SET verified=? WHERE id=?", (0 if a and a['verified'] else 1, id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


def _cascade_delete_artisan(conn, artisan_id):
    """Delete an artisan and everything that references it."""
    db_execute(conn, "DELETE FROM flagged_reviews WHERE review_id IN (SELECT id FROM reviews WHERE artisan_id=?)", (artisan_id,))
    db_execute(conn, "DELETE FROM chat_messages WHERE conversation_id IN (SELECT id FROM conversations WHERE artisan_id=?)", (artisan_id,))
    for sql in [
        "DELETE FROM reviews WHERE artisan_id=?",
        "DELETE FROM portfolios WHERE artisan_id=?",
        "DELETE FROM bookmarks WHERE artisan_id=?",
        "DELETE FROM messages WHERE artisan_id=?",
        "DELETE FROM packages WHERE artisan_id=?",
        "DELETE FROM faqs WHERE artisan_id=?",
        "DELETE FROM reports WHERE artisan_id=?",
        "DELETE FROM verification_requests WHERE artisan_id=?",
        "DELETE FROM quote_requests WHERE artisan_id=?",
        "DELETE FROM job_interests WHERE artisan_id=?",
        "DELETE FROM availability_slots WHERE artisan_id=?",
        "DELETE FROM conversations WHERE artisan_id=?",
        "DELETE FROM artisans WHERE id=?",
    ]:
        db_execute(conn, sql, (artisan_id,))


def _cascade_delete_user(conn, user_id):
    """Delete a user account, cascading into their artisan profile (if any)."""
    artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (user_id,)).fetchone()
    if artisan:
        _cascade_delete_artisan(conn, artisan['id'])
    db_execute(conn, "DELETE FROM users WHERE id=?", (user_id,))


@app.route('/admin/delete-artisan/<int:id>', methods=['POST'])
@admin_required
def admin_delete_artisan(id):
    conn = get_db_connection()
    _cascade_delete_artisan(conn, id)
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('admin_panel'))


@app.route('/admin/delete-user/<int:id>', methods=['POST'])
@admin_required
def admin_delete_user(id):
    conn = get_db_connection()
    _cascade_delete_user(conn, id)
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/admin/suspend-user/<int:id>', methods=['POST'])
@admin_required
def admin_suspend_user(id):
    reason = request.form.get('reason', '').strip()
    conn = get_db_connection()
    user = db_execute(conn, "SELECT is_suspended FROM users WHERE id=?", (id,)).fetchone()
    if user:
        new_state = 0 if user['is_suspended'] else 1
        db_execute(conn, "UPDATE users SET is_suspended=?, suspended_reason=?, suspended_at=CURRENT_TIMESTAMP WHERE id=?",
                   (new_state, reason if new_state else None, id))
        artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (id,)).fetchone()
        if artisan:
            db_execute(conn, "UPDATE artisans SET is_suspended=? WHERE id=?", (new_state, artisan['id']))
        conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('admin_panel'))


@app.route('/admin/suspend-artisan/<int:id>', methods=['POST'])
@admin_required
def admin_suspend_artisan(id):
    conn = get_db_connection()
    a = db_execute(conn, "SELECT is_suspended FROM artisans WHERE id=?", (id,)).fetchone()
    if a:
        db_execute(conn, "UPDATE artisans SET is_suspended=? WHERE id=?", (0 if a['is_suspended'] else 1, id))
        conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('admin_panel'))


@app.route('/artisan/<int:id>/report', methods=['POST'])
def report_artisan(id):
    reporter_name = request.form.get('reporter_name', 'Anonymous').strip()
    reason = request.form.get('reason', '').strip()
    if reason:
        conn = get_db_connection()
        db_execute(conn, "INSERT INTO reports (artisan_id, reporter_name, reason) VALUES (?,?,?)",
                   (id, reporter_name, reason))
        conn.commit()
        conn.close()
    return redirect(url_for('artisan_profile', id=id) + '?reported=1')


@app.route('/request-service', methods=['GET', 'POST'])
def request_service():
    if request.method == 'POST':
        name         = request.form.get('name', '').strip()
        phone        = request.form.get('phone', '').strip()
        skill_needed = request.form.get('skill_needed', '').strip()
        location     = request.form.get('location', '').strip()
        description  = request.form.get('description', '').strip()
        conn = get_db_connection()
        db_execute(conn,
            "INSERT INTO service_requests (name, phone, skill_needed, location, description) VALUES (?,?,?,?,?)",
            (name, phone, skill_needed, location, description))
        conn.commit()
        conn.close()
        return redirect(url_for('service_requests_list') + '?posted=1')
    return render_template('request_service.html')


@app.route('/requests')
def service_requests_list():
    conn = get_db_connection()
    reqs = db_execute(conn, "SELECT * FROM service_requests ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template('requests.html', requests=reqs, posted=request.args.get('posted'))


@app.route('/review/<int:review_id>/reply', methods=['POST'])
@login_required
def reply_review(review_id):
    reply = request.form.get('reply', '').strip()
    if reply:
        conn = get_db_connection()
        review = db_execute(conn,
            "SELECT r.id, a.user_id FROM reviews r JOIN artisans a ON a.id=r.artisan_id WHERE r.id=?",
            (review_id,)).fetchone()
        if review and review['user_id'] == session['user_id']:
            db_execute(conn,
                "UPDATE reviews SET reply=?, replied_at=CURRENT_TIMESTAMP WHERE id=?",
                (reply, review_id))
            conn.commit()
        conn.close()
    return redirect(url_for('notifications'))


@app.route('/api/artisans-by-ids')
def artisans_by_ids():
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify([])
    try:
        ids = [int(i) for i in ids_param.split(',') if i.strip().isdigit()][:10]
    except ValueError:
        return jsonify([])
    if not ids:
        return jsonify([])
    conn = get_db_connection()
    placeholders = ','.join(['?' for _ in ids])
    rows = db_execute(conn, f"SELECT id, name, skill, location, image, is_available FROM artisans WHERE id IN ({placeholders})", tuple(ids)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/admin/reports')
@admin_required
def admin_reports():
    conn = get_db_connection()
    reports = db_execute(conn, """
        SELECT rp.*, a.name as artisan_name, a.skill as artisan_skill, a.is_suspended as artisan_suspended
        FROM reports rp LEFT JOIN artisans a ON a.id=rp.artisan_id
        ORDER BY rp.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin_reports.html', reports=reports)


@app.route('/admin/delete-report/<int:report_id>', methods=['POST'])
@admin_required
def delete_report(report_id):
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM reports WHERE id=?", (report_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_reports'))


# ── Packages ──────────────────────────────────────────────────────────────────
@app.route('/artisan/<int:id>/packages/add', methods=['POST'])
@login_required
def add_package(id):
    conn = get_db_connection()
    a = db_execute(conn, "SELECT user_id FROM artisans WHERE id=?", (id,)).fetchone()
    if a and a['user_id'] == session['user_id']:
        db_execute(conn, "INSERT INTO packages (artisan_id, tier, title, description, price, delivery_days) VALUES (?,?,?,?,?,?)",
            (id, request.form.get('tier',''), request.form.get('title',''),
             request.form.get('description',''), request.form.get('price',''),
             request.form.get('delivery_days') or None))
        conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '#packages')


@app.route('/artisan/<int:id>/packages/<int:pkg_id>/delete', methods=['POST'])
@login_required
def delete_package(id, pkg_id):
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM packages WHERE id=? AND artisan_id=?", (pkg_id, id))
    conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '#packages')


# ── FAQs ──────────────────────────────────────────────────────────────────────
@app.route('/artisan/<int:id>/faq/add', methods=['POST'])
@login_required
def add_faq(id):
    conn = get_db_connection()
    a = db_execute(conn, "SELECT user_id FROM artisans WHERE id=?", (id,)).fetchone()
    if a and a['user_id'] == session['user_id']:
        db_execute(conn, "INSERT INTO faqs (artisan_id, question, answer) VALUES (?,?,?)",
            (id, request.form.get('question',''), request.form.get('answer','')))
        conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '#faq')


@app.route('/artisan/<int:id>/faq/<int:faq_id>/delete', methods=['POST'])
@login_required
def delete_faq(id, faq_id):
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM faqs WHERE id=? AND artisan_id=?", (faq_id, id))
    conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '#faq')


# ── Flag review ────────────────────────────────────────────────────────────────
@app.route('/review/<int:review_id>/flag', methods=['POST'])
def flag_review(review_id):
    conn = get_db_connection()
    db_execute(conn, "INSERT INTO flagged_reviews (review_id, reporter_name, reason) VALUES (?,?,?)",
        (review_id, request.form.get('reporter_name','Anonymous'), request.form.get('reason','')))
    conn.commit()
    conn.close()
    return ('', 204)


# ── Referral ───────────────────────────────────────────────────────────────────
@app.route('/r/<code>')
def referral_redirect(code):
    conn = get_db_connection()
    a = db_execute(conn, "SELECT id FROM artisans WHERE referral_code=?", (code,)).fetchone()
    conn.close()
    if a:
        return redirect(url_for('artisan_profile', id=a['id']))
    return redirect(url_for('artisans'))


# ── Job board ──────────────────────────────────────────────────────────────────
@app.route('/jobs')
def jobs():
    conn = get_db_connection()
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d')
    all_jobs = db_execute(conn, "SELECT * FROM jobs WHERE status='open' AND created_at >= ? ORDER BY created_at DESC", (cutoff,)).fetchall()
    conn.close()
    return render_template('jobs.html', jobs=all_jobs, posted=request.args.get('posted'))


@app.route('/post-job', methods=['GET', 'POST'])
def post_job():
    if request.method == 'POST':
        conn = get_db_connection()
        skill_needed = request.form.get('skill_needed', '')
        title = request.form.get('title', '')
        loc   = request.form.get('location', '')
        db_execute(conn, "INSERT INTO jobs (title, skill_needed, location, budget, description, client_name, client_phone) VALUES (?,?,?,?,?,?,?)",
            (title, skill_needed, loc, request.form.get('budget',''),
             request.form.get('description',''), request.form.get('client_name',''),
             request.form.get('client_phone','')))
        conn.commit()
        op = "ILIKE" if DATABASE_URL else "LIKE"
        matching = db_execute(conn, f"SELECT email, name FROM artisans WHERE skill {op} ? AND email IS NOT NULL AND email != ''",
            ('%' + skill_needed + '%',)).fetchall()
        conn.close()
        for a in matching[:30]:
            send_email(a['email'], f"New job posted: {title}",
                f"Hello {a['name']},\n\nA new job matching your skill ({skill_needed}) was just posted on Artisaan's Crib.\n\n"
                f"Job: {title}\nLocation: {loc}\n\nLog in to express your interest: https://artisaans-crib.vercel.app/jobs")
        return redirect(url_for('jobs') + '?posted=1')
    return render_template('post_job.html')


@app.route('/job/<int:job_id>/interest', methods=['POST'])
def job_interest(job_id):
    conn = get_db_connection()
    my_artisan = None
    if 'user_id' in session:
        my_artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    if my_artisan:
        existing = db_execute(conn, "SELECT id FROM job_interests WHERE job_id=? AND artisan_id=?", (job_id, my_artisan['id'])).fetchone()
        if not existing:
            db_execute(conn, "INSERT INTO job_interests (job_id, artisan_id) VALUES (?,?)", (job_id, my_artisan['id']))
            conn.commit()
    conn.close()
    return redirect(url_for('jobs'))


# ── Admin: newsletter ──────────────────────────────────────────────────────────
@app.route('/admin/newsletter', methods=['GET', 'POST'])
@admin_required
def admin_newsletter():
    msg = ''
    if request.method == 'POST':
        subject = request.form.get('subject', '')
        body    = request.form.get('body', '')
        target  = request.form.get('target', 'all')
        conn = get_db_connection()
        if target == 'artisans':
            rows = db_execute(conn, "SELECT DISTINCT email FROM artisans WHERE email IS NOT NULL AND email != ''").fetchall()
        elif target == 'users':
            rows = db_execute(conn, "SELECT email FROM users WHERE email IS NOT NULL AND email != ''").fetchall()
        else:
            a_rows = db_execute(conn, "SELECT DISTINCT email FROM artisans WHERE email IS NOT NULL AND email != ''").fetchall()
            u_rows = db_execute(conn, "SELECT email FROM users WHERE email IS NOT NULL AND email != ''").fetchall()
            rows = list({r['email'] for r in a_rows + u_rows if r.get('email')})
            rows = [{'email': e} for e in rows]
        conn.close()
        sent = 0
        for r in rows:
            try:
                send_email(r['email'], subject, body)
                sent += 1
            except Exception:
                pass
        msg = f"Newsletter sent to {sent} recipient(s)."
    return render_template('admin_newsletter.html', msg=msg)


# ── Admin: flagged reviews ─────────────────────────────────────────────────────
@app.route('/admin/flagged-reviews')
@admin_required
def admin_flagged_reviews():
    conn = get_db_connection()
    flags = db_execute(conn, """
        SELECT f.*, r.comment, r.rating, r.artisan_id,
               a.name as artisan_name
        FROM flagged_reviews f
        JOIN reviews r ON r.id=f.review_id
        LEFT JOIN artisans a ON a.id=r.artisan_id
        ORDER BY f.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin_flagged.html', flags=flags)


@app.route('/admin/flagged-reviews/<int:flag_id>/dismiss', methods=['POST'])
@admin_required
def dismiss_flag(flag_id):
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM flagged_reviews WHERE id=?", (flag_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_flagged_reviews'))


@app.route('/admin/flagged-reviews/<int:flag_id>/delete-review', methods=['POST'])
@admin_required
def delete_flagged_review(flag_id):
    conn = get_db_connection()
    flag = db_execute(conn, "SELECT review_id FROM flagged_reviews WHERE id=?", (flag_id,)).fetchone()
    if flag:
        db_execute(conn, "DELETE FROM reviews WHERE id=?", (flag['review_id'],))
        db_execute(conn, "DELETE FROM flagged_reviews WHERE review_id=?", (flag['review_id'],))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_flagged_reviews'))


# ── Admin: featured week + promote ────────────────────────────────────────────
@app.route('/admin/featured/<int:id>', methods=['POST'])
@admin_required
def set_featured_week(id):
    conn = get_db_connection()
    db_execute(conn, "UPDATE artisans SET is_featured_week=0")
    db_execute(conn, "UPDATE artisans SET is_featured_week=1 WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/admin/promote/<int:id>', methods=['POST'])
@admin_required
def toggle_promote(id):
    conn = get_db_connection()
    a = db_execute(conn, "SELECT is_promoted FROM artisans WHERE id=?", (id,)).fetchone()
    db_execute(conn, "UPDATE artisans SET is_promoted=? WHERE id=?", (0 if a and a['is_promoted'] else 1, id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


# ── Unread notifications count API ────────────────────────────────────────────
@app.route('/api/unread-count')
def unread_count():
    if 'user_id' not in session:
        return jsonify({'count': 0})
    conn = get_db_connection()
    my_artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    count = 0
    if my_artisan:
        count = db_execute(conn, "SELECT COUNT(*) as c FROM messages WHERE artisan_id=?", (my_artisan['id'],)).fetchone()['c']
    conn.close()
    return jsonify({'count': count})


@app.route('/artisan/<int:id>/pay/<int:pkg_id>', methods=['GET', 'POST'])
def pay_package(id, pkg_id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (id,)).fetchone()
    package = db_execute(conn, "SELECT * FROM packages WHERE id=? AND artisan_id=?", (pkg_id, id)).fetchone()
    if artisan is None or package is None or artisan['is_suspended']:
        conn.close()
        return render_template('404.html'), 404
    artisan = dict(artisan)
    package = dict(package)
    conn.close()

    try:
        amount_naira = float(str(package.get('price') or '0').replace(',', '').strip())
    except ValueError:
        amount_naira = 0

    if request.method == 'POST':
        if not PAYSTACK_SECRET_KEY:
            return render_template('pay.html', artisan=artisan, package=package,
                                    error="Online payments aren't set up yet. Please contact the artisan directly.")
        if amount_naira <= 0:
            return render_template('pay.html', artisan=artisan, package=package,
                                    error="This package doesn't have a valid price yet.")

        client_name  = request.form.get('client_name', '').strip()
        client_phone = request.form.get('client_phone', '').strip()
        client_email = request.form.get('client_email', '').strip()
        if not client_name or not client_phone or not client_email:
            return render_template('pay.html', artisan=artisan, package=package,
                                    error="Please fill in all fields.")

        amount_kobo = int(round(amount_naira * 100))
        reference = f"AC-{uuid.uuid4().hex[:20]}"
        conn = get_db_connection()
        db_execute(conn,
            "INSERT INTO payments (reference, artisan_id, package_id, user_id, client_name, client_email, client_phone, amount_kobo) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (reference, id, pkg_id, session.get('user_id'), client_name, client_email, client_phone, amount_kobo))
        conn.commit()
        conn.close()

        auth_url = paystack_initialize(
            email=client_email,
            amount_kobo=amount_kobo,
            reference=reference,
            callback_url=url_for('payment_callback', _external=True),
            metadata={"artisan_id": id, "package_id": pkg_id},
        )
        if not auth_url:
            return render_template('pay.html', artisan=artisan, package=package,
                                    error="Couldn't start the payment. Please try again shortly.")
        return redirect(auth_url)

    return render_template('pay.html', artisan=artisan, package=package, amount_naira=amount_naira)


@app.route('/payment/callback')
def payment_callback():
    reference = request.args.get('reference', '')
    payment = finalize_payment(reference) if reference else None
    if payment and payment.get('status') == 'success':
        return render_template('payment_result.html', success=True, payment=payment)
    return render_template('payment_result.html', success=False, payment=payment)


@app.route('/paystack/webhook', methods=['POST'])
@csrf.exempt
def paystack_webhook():
    if not PAYSTACK_SECRET_KEY:
        return '', 400
    signature = request.headers.get('X-Paystack-Signature', '')
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode('utf-8'), request.get_data(), hashlib.sha512).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return '', 401
    event = request.get_json(silent=True) or {}
    if event.get('event') == 'charge.success':
        reference = (event.get('data') or {}).get('reference', '')
        if reference:
            finalize_payment(reference)
    return '', 200


@app.route('/book/<int:id>', methods=['GET', 'POST'])
def book_artisan(id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (id,)).fetchone()
    if artisan is None or artisan['is_suspended']:
        conn.close()
        return render_template('404.html'), 404
    if request.method == 'POST':
        client_name  = request.form.get('client_name', '').strip()
        client_phone = request.form.get('client_phone', '').strip()
        client_email = request.form.get('client_email', '').strip()
        service_date = request.form.get('service_date', '').strip()
        note         = request.form.get('note', '').strip()
        db_execute(conn,
            "INSERT INTO bookings (artisan_id, client_name, client_phone, client_email, service_date, note) VALUES (?,?,?,?,?,?)",
            (id, client_name, client_phone, client_email, service_date, note))
        conn.commit()
        artisan = dict(artisan)
        conn.close()
        if artisan.get('email'):
            send_email(
                artisan['email'],
                f"New booking request on Artisaan's Crib",
                f"Hello {artisan['name']},\n\nYou have a new booking request.\n\n"
                f"From: {client_name}\nPhone: {client_phone}\nDate: {service_date}\nNote: {note}\n\n"
                f"Log in to manage it: https://artisans-crib.vercel.app/dashboard"
            )
        return redirect(url_for('artisan_profile', id=id) + '?booked=1')
    conn.close()
    return render_template('book.html', artisan=artisan)


@app.route('/my-bookings')
@login_required
def my_bookings():
    conn = get_db_connection()
    my_artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    bookings = []
    if my_artisan:
        bookings = db_execute(conn, """
            SELECT b.*, a.name as artisan_name, a.skill as artisan_skill, a.image as artisan_image
            FROM bookings b JOIN artisans a ON a.id=b.artisan_id
            WHERE b.artisan_id=? ORDER BY b.created_at DESC
        """, (my_artisan['id'],)).fetchall()
    conn.close()
    return render_template('my_bookings.html', bookings=bookings)


@app.route('/booking/<int:booking_id>/status', methods=['POST'])
@login_required
def update_booking_status(booking_id):
    status = request.form.get('status', '')
    if status not in ('pending', 'confirmed', 'declined', 'completed'):
        return redirect(url_for('my_bookings'))
    conn = get_db_connection()
    booking = db_execute(conn, "SELECT b.*, a.name as artisan_name, a.skill as artisan_skill FROM bookings b JOIN artisans a ON a.id=b.artisan_id WHERE b.id=?", (booking_id,)).fetchone()
    db_execute(conn, "UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
    conn.commit()
    conn.close()
    if booking and booking.get('client_email') and status in ('confirmed', 'declined'):
        status_word = 'confirmed' if status == 'confirmed' else 'declined'
        send_email(booking['client_email'],
            f"Booking {status_word} — {booking['artisan_name']}",
            f"Hello {booking['client_name']},\n\nYour booking with {booking['artisan_name']} ({booking['artisan_skill']}) has been {status_word}.\n\n"
            f"Service date: {booking.get('service_date','')}\n\nVisit Artisaan's Crib: https://artisaans-crib.vercel.app")
    return redirect(url_for('my_bookings'))


@app.route('/compare')
def compare():
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return render_template('compare.html', artisans=[], ids='')
    try:
        ids = [int(i) for i in ids_param.split(',') if i.strip().isdigit()][:3]
    except ValueError:
        ids = []
    artisans = []
    if ids:
        conn = get_db_connection()
        placeholders = ','.join(['?' for _ in ids])
        rows = db_execute(conn, f"""
            SELECT a.*,
                COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
                COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count,
                COALESCE((SELECT COUNT(*) FROM bookmarks bm WHERE bm.artisan_id=a.id), 0) as saves_count
            FROM artisans a WHERE a.id IN ({placeholders})
        """, tuple(ids)).fetchall()
        conn.close()
        id_order = {v: i for i, v in enumerate(ids)}
        artisans = sorted(rows, key=lambda r: id_order.get(r['id'], 99))
    return render_template('compare.html', artisans=artisans, ids=ids_param)


@app.route('/map')
def map_view():
    conn = get_db_connection()
    rows = db_execute(conn, """
        SELECT id, name, skill, location, image, is_available,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a WHERE name IS NOT NULL AND name != '' AND location IS NOT NULL AND location != '' AND COALESCE(is_suspended, 0) = 0
    """).fetchall()
    conn.close()
    return render_template('map.html', artisans=rows)


@app.route('/dashboard/toggle-availability', methods=['POST'])
@login_required
def dashboard_toggle_availability():
    conn = get_db_connection()
    a = db_execute(conn, "SELECT id, is_available FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    if a:
        db_execute(conn, "UPDATE artisans SET is_available=? WHERE id=?", (0 if a['is_available'] else 1, a['id']))
        conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/category/<skill>')
def category_page(skill):
    conn = get_db_connection()
    op = "ILIKE" if DATABASE_URL else "LIKE"
    rows = db_execute(conn, f"""
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a
        WHERE a.skill {op} ? AND a.name IS NOT NULL AND a.name != '' AND COALESCE(a.is_suspended, 0) = 0
        ORDER BY a.is_promoted DESC, avg_rating DESC, a.view_count DESC
    """, ('%' + skill + '%',)).fetchall()
    conn.close()
    return render_template('category.html', artisans=rows, skill=skill)


@app.route('/leaderboard')
def leaderboard():
    conn = get_db_connection()
    top_viewed = db_execute(conn, """
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a WHERE a.name IS NOT NULL AND a.name != '' AND COALESCE(a.is_suspended, 0) = 0
        ORDER BY a.view_count DESC LIMIT 10
    """).fetchall()
    top_rated = db_execute(conn, """
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a WHERE a.name IS NOT NULL AND a.name != '' AND COALESCE(a.is_suspended, 0) = 0
        HAVING review_count >= 1
        ORDER BY avg_rating DESC, review_count DESC LIMIT 10
    """).fetchall() if not DATABASE_URL else db_execute(conn, """
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a WHERE a.name IS NOT NULL AND a.name != '' AND COALESCE(a.is_suspended, 0) = 0
        ORDER BY avg_rating DESC, review_count DESC LIMIT 10
    """).fetchall()
    conn.close()
    return render_template('leaderboard.html', top_viewed=top_viewed, top_rated=top_rated)


@app.route('/artisan/<int:id>/widget')
def profile_widget(id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (id,)).fetchone()
    if not artisan:
        conn.close()
        return "Not found", 404
    artisan = dict(artisan)
    reviews = db_execute(conn, "SELECT rating FROM reviews WHERE artisan_id=?", (id,)).fetchall()
    conn.close()
    review_count = len(reviews)
    avg_rating = round(sum(r['rating'] for r in reviews) / review_count, 1) if reviews else 0
    profile_url = request.host_url.rstrip('/') + f'/artisan/{id}'
    return render_template('widget.html', artisan=artisan, avg_rating=avg_rating,
                           review_count=review_count, profile_url=profile_url)


@app.route('/artisan/<int:id>/calendar', methods=['GET', 'POST'])
@login_required
def artisan_calendar(id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT id, user_id, name FROM artisans WHERE id=?", (id,)).fetchone()
    if not artisan or artisan['user_id'] != session['user_id']:
        conn.close()
        return redirect(url_for('artisan_profile', id=id))
    if request.method == 'POST':
        slot_date   = request.form.get('slot_date', '')
        is_available = int(request.form.get('is_available', 1))
        action = request.form.get('action', 'toggle')
        if slot_date:
            if action == 'remove':
                db_execute(conn, "DELETE FROM availability_slots WHERE artisan_id=? AND slot_date=?", (id, slot_date))
            else:
                op_sql = "INSERT INTO availability_slots (artisan_id, slot_date, is_available) VALUES (?,?,?) ON CONFLICT(artisan_id, slot_date) DO UPDATE SET is_available=?" if DATABASE_URL else "INSERT OR REPLACE INTO availability_slots (artisan_id, slot_date, is_available) VALUES (?,?,?)"
                if DATABASE_URL:
                    db_execute(conn, op_sql, (id, slot_date, is_available, is_available))
                else:
                    db_execute(conn, op_sql, (id, slot_date, is_available))
            conn.commit()
        conn.close()
        return redirect(url_for('artisan_calendar', id=id))
    slots = db_execute(conn, "SELECT slot_date, is_available FROM availability_slots WHERE artisan_id=? ORDER BY slot_date", (id,)).fetchall()
    conn.close()
    return render_template('availability_calendar.html', artisan=artisan, slots=slots)


@app.route('/artisan/<int:id>/verify-request', methods=['POST'])
@login_required
def verification_request(id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT id, user_id FROM artisans WHERE id=?", (id,)).fetchone()
    if artisan and artisan['user_id'] == session['user_id']:
        existing = db_execute(conn, "SELECT id FROM verification_requests WHERE artisan_id=? AND status='pending'", (id,)).fetchone()
        if not existing:
            db_execute(conn, "INSERT INTO verification_requests (artisan_id, id_type, notes) VALUES (?,?,?)",
                (id, request.form.get('id_type',''), request.form.get('notes','')))
            conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '?verify_requested=1')


@app.route('/admin/verification-requests')
@admin_required
def admin_verification_requests():
    conn = get_db_connection()
    reqs = db_execute(conn, """
        SELECT vr.*, a.name as artisan_name, a.skill as artisan_skill, a.id as artisan_id
        FROM verification_requests vr JOIN artisans a ON a.id=vr.artisan_id
        WHERE vr.status='pending' ORDER BY vr.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin_verify.html', reqs=reqs)


@app.route('/admin/verification-requests/<int:req_id>/approve', methods=['POST'])
@admin_required
def approve_verification(req_id):
    conn = get_db_connection()
    vr = db_execute(conn, "SELECT artisan_id FROM verification_requests WHERE id=?", (req_id,)).fetchone()
    if vr:
        db_execute(conn, "UPDATE artisans SET verified=1 WHERE id=?", (vr['artisan_id'],))
        db_execute(conn, "UPDATE verification_requests SET status='approved' WHERE id=?", (req_id,))
        conn.commit()
    conn.close()
    return redirect(url_for('admin_verification_requests'))


@app.route('/admin/verification-requests/<int:req_id>/reject', methods=['POST'])
@admin_required
def reject_verification(req_id):
    conn = get_db_connection()
    db_execute(conn, "UPDATE verification_requests SET status='rejected' WHERE id=?", (req_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_verification_requests'))


@app.route('/chat/<int:artisan_id>', methods=['GET', 'POST'])
def chat(artisan_id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (artisan_id,)).fetchone()
    if not artisan:
        conn.close()
        return redirect(url_for('artisans'))
    artisan = dict(artisan)

    # identify client
    client_user_id = session.get('user_id')
    client_name  = session.get('chat_name', '')
    client_phone = session.get('chat_phone', '')

    # If client is logged in, fetch their name from users table
    if client_user_id and not client_name:
        u = db_execute(conn, "SELECT name FROM users WHERE id=?", (client_user_id,)).fetchone()
        if u:
            client_name = u['name']

    error = ''
    conv = None

    if request.method == 'POST':
        action = request.form.get('action', 'identify')
        if action == 'identify':
            client_name  = request.form.get('client_name', '').strip()
            client_phone = request.form.get('client_phone', '').strip()
            if not client_name or not client_phone:
                error = 'Please enter your name and phone number.'
            else:
                session['chat_name']  = client_name
                session['chat_phone'] = client_phone
        elif action == 'send':
            body = request.form.get('body', '').strip()
            if body and client_name and client_phone:
                # find or create conversation
                if client_user_id:
                    conv = db_execute(conn, "SELECT * FROM conversations WHERE artisan_id=? AND client_user_id=?", (artisan_id, client_user_id)).fetchone()
                else:
                    conv = db_execute(conn, "SELECT * FROM conversations WHERE artisan_id=? AND client_phone=?", (artisan_id, client_phone)).fetchone()
                if not conv:
                    db_execute(conn, "INSERT INTO conversations (artisan_id, client_name, client_phone, client_user_id) VALUES (?,?,?,?)",
                        (artisan_id, client_name, client_phone, client_user_id))
                    conn.commit()
                    if client_user_id:
                        conv = db_execute(conn, "SELECT * FROM conversations WHERE artisan_id=? AND client_user_id=?", (artisan_id, client_user_id)).fetchone()
                    else:
                        conv = db_execute(conn, "SELECT * FROM conversations WHERE artisan_id=? AND client_phone=?", (artisan_id, client_phone)).fetchone()
                conv_id = conv['id']
                db_execute(conn, "INSERT INTO chat_messages (conversation_id, sender, body) VALUES (?,?,?)", (conv_id, 'client', body))
                ts_sql = "NOW()" if DATABASE_URL else "CURRENT_TIMESTAMP"
                db_execute(conn, f"UPDATE conversations SET last_message_at={ts_sql} WHERE id=?", (conv_id,))
                conn.commit()
                # notify artisan
                if artisan.get('email'):
                    send_email(artisan['email'], f"New message from {client_name} | Artisaan's Crib",
                        f"Hi {artisan['name']},\n\n{client_name} sent you a message:\n\n\"{body}\"\n\nReply from your inbox: artisan-service-app.vercel.app/inbox\n\nArtisaan's Crib")
        conn.close()
        return redirect(url_for('chat', artisan_id=artisan_id))

    # Load conversation and messages
    messages = []
    if client_name and client_phone:
        if client_user_id:
            conv = db_execute(conn, "SELECT * FROM conversations WHERE artisan_id=? AND client_user_id=?", (artisan_id, client_user_id)).fetchone()
        else:
            conv = db_execute(conn, "SELECT * FROM conversations WHERE artisan_id=? AND client_phone=?", (artisan_id, client_phone)).fetchone()
        if conv:
            messages = db_execute(conn, "SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY created_at ASC", (conv['id'],)).fetchall()
            db_execute(conn, "UPDATE chat_messages SET is_read=1 WHERE conversation_id=? AND sender='artisan'", (conv['id'],))
            conn.commit()
    conn.close()
    return render_template('chat.html', artisan=artisan, messages=messages,
                           client_name=client_name, client_phone=client_phone, error=error)


@app.route('/inbox')
@login_required
def inbox():
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    if not artisan:
        conn.close()
        return redirect(url_for('dashboard'))
    convs = db_execute(conn, """
        SELECT c.*,
            (SELECT body FROM chat_messages WHERE conversation_id=c.id ORDER BY created_at DESC LIMIT 1) as last_msg,
            (SELECT COUNT(*) FROM chat_messages WHERE conversation_id=c.id AND sender='client' AND is_read=0) as unread
        FROM conversations c WHERE c.artisan_id=?
        ORDER BY c.last_message_at DESC
    """, (artisan['id'],)).fetchall()
    conn.close()
    return render_template('inbox.html', convs=convs)


@app.route('/inbox/<int:conv_id>', methods=['GET', 'POST'])
@login_required
def inbox_thread(conv_id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT id FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    if not artisan:
        conn.close()
        return redirect(url_for('dashboard'))
    conv = db_execute(conn, "SELECT * FROM conversations WHERE id=? AND artisan_id=?", (conv_id, artisan['id'])).fetchone()
    if not conv:
        conn.close()
        return redirect(url_for('inbox'))
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        if body:
            db_execute(conn, "INSERT INTO chat_messages (conversation_id, sender, body) VALUES (?,?,?)", (conv_id, 'artisan', body))
            ts_sql = "NOW()" if DATABASE_URL else "CURRENT_TIMESTAMP"
            db_execute(conn, f"UPDATE conversations SET last_message_at={ts_sql} WHERE id=?", (conv_id,))
            conn.commit()
        conn.close()
        return redirect(url_for('inbox_thread', conv_id=conv_id))
    messages = db_execute(conn, "SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY created_at ASC", (conv_id,)).fetchall()
    db_execute(conn, "UPDATE chat_messages SET is_read=1 WHERE conversation_id=? AND sender='client'", (conv_id,))
    conn.commit()
    conn.close()
    return render_template('thread.html', conv=conv, messages=messages)


@app.route('/api/chat-poll/<int:id_param>')
def chat_poll(id_param):
    after    = request.args.get('after', 0)
    client   = request.args.get('client', '')
    is_conv  = request.args.get('conv', '')
    conn     = get_db_connection()
    if is_conv:
        conv_id = id_param  # id_param is conv_id (artisan inbox thread)
    elif client:
        conv = db_execute(conn, "SELECT id FROM conversations WHERE artisan_id=? AND client_phone=?", (id_param, client)).fetchone()
        conv_id = conv['id'] if conv else None
    else:
        conv_id = None
    if not conv_id:
        conn.close()
        return jsonify([])
    msgs = db_execute(conn, "SELECT * FROM chat_messages WHERE conversation_id=? AND id>? ORDER BY created_at ASC", (conv_id, after)).fetchall()
    conn.close()
    return jsonify([{'id': m['id'], 'sender': m['sender'], 'body': m['body'], 'created_at': str(m['created_at'])} for m in msgs])


@app.route('/quote/<int:id>', methods=['GET', 'POST'])
def request_quote(id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (id,)).fetchone()
    if not artisan or artisan['is_suspended']:
        conn.close()
        return redirect(url_for('artisans'))
    artisan = dict(artisan)
    success = False
    if request.method == 'POST':
        client_name    = request.form.get('client_name', '').strip()
        client_phone   = request.form.get('client_phone', '').strip()
        client_email   = request.form.get('client_email', '').strip()
        service_needed = request.form.get('service_needed', '').strip()
        budget         = request.form.get('budget', '').strip()
        message        = request.form.get('message', '').strip()
        db_execute(conn, """INSERT INTO quote_requests
            (artisan_id, client_name, client_phone, client_email, service_needed, budget, message)
            VALUES (?,?,?,?,?,?,?)""",
            (id, client_name, client_phone, client_email, service_needed, budget, message))
        conn.commit()
        if artisan.get('email'):
            send_email(artisan['email'],
                f"New Quote Request from {client_name} | Artisaan's Crib",
                f"Hi {artisan['name']},\n\nYou have a new quote request on Artisaan's Crib.\n\n"
                f"Client: {client_name}\nPhone: {client_phone}\nEmail: {client_email}\n"
                f"Service Needed: {service_needed}\nBudget: {budget}\nMessage: {message}\n\n"
                f"Log in to your dashboard to respond.\n\nArtisaan's Crib")
        success = True
    conn.close()
    return render_template('quote.html', artisan=artisan, success=success)


@app.errorhandler(500)
def internal_error(e):
    return render_template('500.html'), 500

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


_tables_ready = False

@app.before_request
def ensure_tables():
    global _tables_ready
    if not _tables_ready:
        try:
            create_table()
            _tables_ready = True
        except Exception as exc:
            app.logger.error("DB table init failed: %s", exc)


if __name__ == "__main__":
    create_table()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
