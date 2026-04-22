import sqlite3
import os
import bcrypt
from flask import Flask, render_template, request, redirect, url_for, session, make_response
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get("SECRET_KEY", "mysecretkey")

# ── Database config ────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")  # set on Vercel → PostgreSQL
UPLOAD_FOLDER = "/tmp/uploads" if os.environ.get('VERCEL') else "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def get_db_connection():
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


def db_execute(conn, sql, params=()):
    """Run a query that does not need to return a new-row ID."""
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute(sql.replace('?', '%s'), params)
        return cur
    return conn.execute(sql, params)


def db_insert(conn, sql, params=()):
    """Run an INSERT and return the new row's id."""
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute(sql.replace('?', '%s') + ' RETURNING id', params)
        return cur.fetchone()['id']
    conn.execute(sql, params)
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


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

    conn.commit()
    conn.close()


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/sw.js")
def service_worker():
    response = make_response(app.send_static_file("sw.js"))
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/artisans")
def artisans():
    search = request.args.get('search')
    conn = get_db_connection()
    if search:
        rows = db_execute(conn,
            "SELECT * FROM artisans WHERE skill ILIKE ?",
            ('%' + search + '%',)
        ).fetchall() if DATABASE_URL else db_execute(conn,
            "SELECT * FROM artisans WHERE skill LIKE ?",
            ('%' + search + '%',)
        ).fetchall()
    else:
        rows = db_execute(conn,
            "SELECT * FROM artisans WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
    conn.close()
    return render_template("artisans.html", artisans=rows)


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

        filename = secure_filename(profile_pic.filename)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        profile_pic.save(os.path.join(UPLOAD_FOLDER, filename))

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
            "INSERT INTO artisans (name,email,dob,gender,languages,skill,experience,certifications,availability,price_range,location,service_area,phone,whatsapp,instagram,facebook,tiktok,twitter,youtube,website,description,custom_orders,marketing,image) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name,email,dob,gender,languages,skill,experience,certifications,availability,price_range,location,service_area,phone,whatsapp,instagram,facebook,tiktok,twitter,youtube,website,description,custom_orders,marketing,filename)
        )

        for photo in request.files.getlist("portfolio")[:5]:
            if photo and allowed_file(photo.filename):
                pf = secure_filename(photo.filename)
                photo.save(os.path.join(UPLOAD_FOLDER, pf))
                db_execute(conn, "INSERT INTO portfolios (artisan_id, image) VALUES (?, ?)", (artisan_id, pf))

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
    conn.close()

    review_count   = len(reviews)
    average_rating = round(sum(r['rating'] for r in reviews) / review_count, 1) if reviews else 0

    return render_template('artisan_profile_new.html',
        artisan=artisan, reviews=reviews,
        average_rating=average_rating, review_count=review_count, portfolio=portfolio)


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
        if pic and allowed_file(pic.filename):
            fn = secure_filename(pic.filename)
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            pic.save(os.path.join(UPLOAD_FOLDER, fn))
            u['image'] = fn
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
                pf = secure_filename(photo.filename)
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                photo.save(os.path.join(UPLOAD_FOLDER, pf))
                db_execute(conn, "INSERT INTO portfolios (artisan_id, image) VALUES (?,?)", (id, pf))

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


@app.errorhandler(500)
def internal_error(e):
    return render_template('500.html'), 500

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


create_table()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
