import sqlite3
import os
import ssl
import urllib.parse
import bcrypt
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, request, redirect, url_for, session, make_response
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get("SECRET_KEY", "mysecretkey")

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

    for col, definition in [
        ("verified", "INTEGER DEFAULT 0"),
        ("user_id", "INTEGER"),
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
    recent = db_execute(conn, "SELECT * FROM artisans WHERE name IS NOT NULL AND name != '' ORDER BY id DESC LIMIT 4").fetchall()
    categories = db_execute(conn, "SELECT DISTINCT skill FROM artisans WHERE skill IS NOT NULL AND skill != '' ORDER BY skill LIMIT 16").fetchall()
    conn.close()
    return render_template("home.html", artisan_count=artisan_count, recent_artisans=recent, categories=categories)


@app.route("/artisans")
def artisans():
    search = request.args.get('search', '').strip()
    conn = get_db_connection()
    rating_sql = """
        SELECT a.*,
            COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.artisan_id=a.id), 0) as avg_rating,
            COALESCE((SELECT COUNT(*) FROM reviews r WHERE r.artisan_id=a.id), 0) as review_count
        FROM artisans a
    """
    if search:
        op = "ILIKE" if DATABASE_URL else "LIKE"
        like = '%' + search + '%'
        rows = db_execute(conn,
            rating_sql + f" WHERE (a.skill {op} ? OR a.location {op} ? OR a.name {op} ?) AND a.name IS NOT NULL AND a.name != '' ORDER BY a.id DESC",
            (like, like, like)
        ).fetchall()
    else:
        rows = db_execute(conn,
            rating_sql + " WHERE a.name IS NOT NULL AND a.name != '' ORDER BY a.id DESC"
        ).fetchall()
    conn.close()
    return render_template("artisans.html", artisans=rows, search=search)


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
        price_range  = request.form.get("price_range", "")
        service_area = request.form.get("service_area", "")
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
            "INSERT INTO artisans (name,email,dob,gender,languages,skill,experience,certifications,availability,price_range,location,service_area,phone,whatsapp,instagram,facebook,tiktok,twitter,youtube,website,description,custom_orders,marketing,image,user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name,email,dob,gender,languages,skill,experience,certifications,availability,price_range,location,service_area,phone,whatsapp,instagram,facebook,tiktok,twitter,youtube,website,description,custom_orders,marketing,image_url,session.get('user_id'))
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
        db_execute(conn, "INSERT INTO reviews (artisan_id, rating, comment) VALUES (?, ?, ?)",
                   (id, request.form['rating'], request.form['comment']))
        conn.commit()
        conn.close()
        return redirect(url_for('artisan_profile', id=id))

    artisan_row = db_execute(conn, "SELECT * FROM artisans WHERE id = ?", (id,)).fetchone()
    if artisan_row is None:
        conn.close()
        return render_template('404.html'), 404
    artisan = dict(artisan_row)

    reviews   = db_execute(conn, "SELECT * FROM reviews WHERE artisan_id=? ORDER BY created_at DESC", (id,)).fetchall()
    portfolio = db_execute(conn, "SELECT image FROM portfolios WHERE artisan_id=?", (id,)).fetchall()
    is_bookmarked = False
    if 'user_id' in session:
        bm = db_execute(conn, "SELECT id FROM bookmarks WHERE user_id=? AND artisan_id=?", (session['user_id'], id)).fetchone()
        is_bookmarked = bool(bm)
    conn.close()

    review_count   = len(reviews)
    average_rating = round(sum(r['rating'] for r in reviews) / review_count, 1) if reviews else 0
    contacted = request.args.get('contacted') == '1'

    return render_template('artisan_profile_new.html',
        artisan=artisan, reviews=reviews,
        average_rating=average_rating, review_count=review_count,
        portfolio=portfolio, is_bookmarked=is_bookmarked, contacted=contacted)


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
                  'youtube','website','description','custom_orders']
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
            youtube=?,website=?,description=?,custom_orders=?,marketing=?,image=?
            WHERE id=?''',
            (u['name'],u['email'],u['dob'],u['gender'],u['languages'],u['skill'],u['experience'],
             u['certifications'],u['availability'],u['price_range'],u['location'],u['service_area'],
             u['phone'],u['whatsapp'],u['instagram'],u['facebook'],u['tiktok'],u['twitter'],
             u['youtube'],u['website'],u['description'],u['custom_orders'],u['marketing'],u['image'],id))

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
    conn.close()
    return render_template('account.html', user=user, my_artisan=my_artisan, message=message, msg_type=msg_type)


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
    conn = get_db_connection()
    db_execute(conn, "INSERT INTO messages (artisan_id, sender_name, sender_phone, message) VALUES (?,?,?,?)",
        (id, request.form.get('sender_name',''), request.form.get('sender_phone',''), request.form.get('message','')))
    conn.commit()
    conn.close()
    return redirect(url_for('artisan_profile', id=id) + '?contacted=1')


@app.route('/admin')
def admin_panel():
    if not is_admin():
        return redirect(url_for('home'))
    conn = get_db_connection()
    artisans = db_execute(conn, "SELECT * FROM artisans ORDER BY id DESC").fetchall()
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
