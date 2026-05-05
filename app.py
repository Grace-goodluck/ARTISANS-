import sqlite3
import os
import ssl
import urllib.parse
import bcrypt
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, request, redirect, url_for, session, make_response, jsonify
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

import re as _re

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get("SECRET_KEY", "mysecretkey")

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
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "dcalqyzvn"),
    api_key=os.environ.get("CLOUDINARY_API_KEY", "633257313267431"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "li1Nm4wZs9rS7S7I4szeegemFfw"),
    secure=True
)

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


# ── No-cache header ────────────────────────────────────────────────────────────
@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
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

    for col, definition in [
        ("business_hours", "TEXT"),
        ("photo_url", "TEXT"),
        ("referral_code", "TEXT"),
        ("is_promoted", "INTEGER DEFAULT 0"),
        ("is_featured_week", "INTEGER DEFAULT 0"),
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

    conn.commit()
    conn.close()


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/debug-info")
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
        WHERE r.rating >= 4 AND r.comment IS NOT NULL AND r.comment != ''
        ORDER BY r.rating DESC, r.id DESC LIMIT 3
    """).fetchall()
    featured_artisan = db_execute(conn, """
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a WHERE a.is_featured_week=1 LIMIT 1
    """).fetchone()
    conn.close()
    return render_template("home.html", artisan_count=artisan_count, categories=categories,
                           skill_count=skill_count, testimonials=testimonials,
                           featured_artisan=featured_artisan)


@app.route("/artisans")
def artisans():
    search          = request.args.get('search', '').strip()
    sort            = request.args.get('sort', 'newest')
    location_filter = request.args.get('location', '').strip()
    rating_filter   = request.args.get('rating', '').strip()
    avail_filter    = request.args.get('avail', '').strip()
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
    conditions = ["a.name IS NOT NULL", "a.name != ''"]
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
                           skill_list=skill_list)


@app.route("/add-artisan", methods=["GET", "POST"])
def add_artisan():
    if "user_id" not in session:
        return redirect(url_for('login'))

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
def register():
    if request.method == "POST":
        fullname = request.form["fullname"]
        email    = request.form["email"]
        hashed   = bcrypt.hashpw(request.form["password"].encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        conn = get_db_connection()
        try:
            db_execute(conn, "INSERT INTO users (name, email, password) VALUES (?, ?, ?)", (fullname, email, hashed))
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            return render_template("register.html", error="An account with that email already exists.")
        conn.close()
        return redirect(url_for('home'))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form["email"]
        password = request.form["password"]

        conn = get_db_connection()
        user = db_execute(conn, "SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

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
                session["user_id"] = user["id"]
                return redirect(url_for('home'))
            return render_template("login.html", error="Incorrect password")
        return render_template("login.html", error="User not found")

    return render_template("login.html")


@app.route("/users")
def show_users():
    if "user_id" not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    users = db_execute(conn, "SELECT * FROM users").fetchall()
    conn.close()
    return render_template("users.html", users=users, message=request.args.get("message"))


@app.route("/delete/<int:id>", methods=["POST"])
def delete_user(id):
    if "user_id" not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM users WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('show_users'))


@app.route("/edit/<int:id>", methods=["GET", "POST"])
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
        qr_url=qr_url, profile_url=profile_url)


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
def toggle_availability(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def notifications():
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
    conn.close()
    referral_url = request.host_url.rstrip('/') + '/r/' + artisan['referral_code']
    return render_template('dashboard.html', artisan=artisan, msg_count=msg_count,
                           review_count=rev_count, avg_rating=avg_rating, recent_msgs=recent_msgs,
                           saves_count=saves_count, book_count=book_count, response_rate=response_rate,
                           completed_jobs=completed_jobs, referral_url=referral_url)


@app.route('/artisan/<int:id>/edit', methods=['GET', 'POST'])
def edit_artisan(id):
    if "user_id" not in session:
        return redirect(url_for('login'))

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
                  'youtube','website','description','custom_orders','business_hours']
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
            youtube=?,website=?,description=?,custom_orders=?,marketing=?,image=?,business_hours=?
            WHERE id=?''',
            (u['name'],u['email'],u['dob'],u['gender'],u['languages'],u['skill'],u['experience'],
             u['certifications'],u['availability'],u['price_range'],u['location'],u['service_area'],
             u['phone'],u['whatsapp'],u['instagram'],u['facebook'],u['tiktok'],u['twitter'],
             u['youtube'],u['website'],u['description'],u['custom_orders'],u['marketing'],u['image'],
             u['business_hours'],id))

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
def delete_portfolio_photo(id, photo_id):
    if "user_id" not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    photo = db_execute(conn, "SELECT image FROM portfolios WHERE id=? AND artisan_id=?", (photo_id, id)).fetchone()
    if photo:
        db_execute(conn, "DELETE FROM portfolios WHERE id=?", (photo_id,))
        conn.commit()
    conn.close()
    return redirect(url_for('edit_artisan', id=id))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    message = ""
    if request.method == 'POST':
        email        = request.form.get('email')
        new_password = request.form.get('new_password')
        conn = get_db_connection()
        user = db_execute(conn, "SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            hashed = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            db_execute(conn, "UPDATE users SET password=? WHERE email=?", (hashed, email))
            conn.commit()
            message = "Password updated successfully!"
        else:
            message = "Email not found!"
        conn.close()
    return render_template('forgot_password.html', message=message)


@app.route('/account', methods=['GET', 'POST'])
def account():
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def toggle_bookmark(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def saved_artisans():
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def admin_panel():
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    artisans = db_execute(conn, "SELECT * FROM artisans ORDER BY is_promoted DESC, id DESC").fetchall()
    users = db_execute(conn, "SELECT * FROM users ORDER BY id DESC").fetchall()
    msgs = db_execute(conn, """
        SELECT m.*, a.name as artisan_name FROM messages m
        LEFT JOIN artisans a ON a.id = m.artisan_id
        ORDER BY m.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin.html', artisans=artisans, users=users, msgs=msgs)


@app.route('/admin/verify/<int:id>', methods=['POST'])
def verify_artisan(id):
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    a = db_execute(conn, "SELECT verified FROM artisans WHERE id=?", (id,)).fetchone()
    db_execute(conn, "UPDATE artisans SET verified=? WHERE id=?", (0 if a and a['verified'] else 1, id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete-artisan/<int:id>', methods=['POST'])
def admin_delete_artisan(id):
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    for sql in [
        "DELETE FROM reviews WHERE artisan_id=?",
        "DELETE FROM portfolios WHERE artisan_id=?",
        "DELETE FROM bookmarks WHERE artisan_id=?",
        "DELETE FROM messages WHERE artisan_id=?",
        "DELETE FROM artisans WHERE id=?",
    ]:
        db_execute(conn, sql, (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete-user/<int:id>', methods=['POST'])
def admin_delete_user(id):
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM users WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


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
def reply_review(review_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def admin_reports():
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    reports = db_execute(conn, """
        SELECT rp.*, a.name as artisan_name, a.skill as artisan_skill
        FROM reports rp LEFT JOIN artisans a ON a.id=rp.artisan_id
        ORDER BY rp.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin_reports.html', reports=reports)


@app.route('/admin/delete-report/<int:report_id>', methods=['POST'])
def delete_report(report_id):
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM reports WHERE id=?", (report_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_reports'))


# ── Packages ──────────────────────────────────────────────────────────────────
@app.route('/artisan/<int:id>/packages/add', methods=['POST'])
def add_package(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def delete_package(id, pkg_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM packages WHERE id=? AND artisan_id=?", (pkg_id, id))
    conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '#packages')


# ── FAQs ──────────────────────────────────────────────────────────────────────
@app.route('/artisan/<int:id>/faq/add', methods=['POST'])
def add_faq(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    a = db_execute(conn, "SELECT user_id FROM artisans WHERE id=?", (id,)).fetchone()
    if a and a['user_id'] == session['user_id']:
        db_execute(conn, "INSERT INTO faqs (artisan_id, question, answer) VALUES (?,?,?)",
            (id, request.form.get('question',''), request.form.get('answer','')))
        conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '#faq')


@app.route('/artisan/<int:id>/faq/<int:faq_id>/delete', methods=['POST'])
def delete_faq(id, faq_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
    all_jobs = db_execute(conn, "SELECT * FROM jobs WHERE status='open' ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template('jobs.html', jobs=all_jobs, posted=request.args.get('posted'))


@app.route('/post-job', methods=['GET', 'POST'])
def post_job():
    if request.method == 'POST':
        conn = get_db_connection()
        db_execute(conn, "INSERT INTO jobs (title, skill_needed, location, budget, description, client_name, client_phone) VALUES (?,?,?,?,?,?,?)",
            (request.form.get('title',''), request.form.get('skill_needed',''),
             request.form.get('location',''), request.form.get('budget',''),
             request.form.get('description',''), request.form.get('client_name',''),
             request.form.get('client_phone','')))
        conn.commit()
        conn.close()
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
def admin_newsletter():
    if not is_admin():
        return redirect(url_for('home'))
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
def admin_flagged_reviews():
    if not is_admin():
        return redirect(url_for('home'))
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
def dismiss_flag(flag_id):
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    db_execute(conn, "DELETE FROM flagged_reviews WHERE id=?", (flag_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_flagged_reviews'))


@app.route('/admin/flagged-reviews/<int:flag_id>/delete-review', methods=['POST'])
def delete_flagged_review(flag_id):
    if not is_admin():
        return redirect(url_for('home'))
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
def set_featured_week(id):
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    db_execute(conn, "UPDATE artisans SET is_featured_week=0")
    db_execute(conn, "UPDATE artisans SET is_featured_week=1 WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/admin/promote/<int:id>', methods=['POST'])
def toggle_promote(id):
    if not is_admin():
        return redirect(url_for('home'))
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


@app.route('/book/<int:id>', methods=['GET', 'POST'])
def book_artisan(id):
    conn = get_db_connection()
    artisan = db_execute(conn, "SELECT * FROM artisans WHERE id=?", (id,)).fetchone()
    if artisan is None:
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
def my_bookings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
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
def update_booking_status(booking_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    status = request.form.get('status', '')
    if status not in ('pending', 'confirmed', 'declined', 'completed'):
        return redirect(url_for('my_bookings'))
    conn = get_db_connection()
    db_execute(conn, "UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
    conn.commit()
    conn.close()
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
        FROM artisans a WHERE name IS NOT NULL AND name != '' AND location IS NOT NULL AND location != ''
    """).fetchall()
    conn.close()
    return render_template('map.html', artisans=rows)


@app.route('/dashboard/toggle-availability', methods=['POST'])
def dashboard_toggle_availability():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    a = db_execute(conn, "SELECT id, is_available FROM artisans WHERE user_id=?", (session['user_id'],)).fetchone()
    if a:
        db_execute(conn, "UPDATE artisans SET is_available=? WHERE id=?", (0 if a['is_available'] else 1, a['id']))
        conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))


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
