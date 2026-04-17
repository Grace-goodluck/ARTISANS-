import sqlite3
import os
import bcrypt
from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get("SECRET_KEY", "mysecretkey")

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

UPLOAD_FOLDER = "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def create_table():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            password TEXT
        )
    ''') 

    conn.execute('''
        CREATE TABLE IF NOT EXISTS artisans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            skill TEXT,
            location TEXT,
            experience INTEGER,
            phone TEXT,
            whatsapp TEXT,
            description TEXT,
            image TEXT
        )
    ''')

    conn.execute('''
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artisan_id INTEGER,
        rating INTEGER,
        comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (artisan_id) REFERENCES artisans(id)
    )
    ''')

    conn.commit()
    conn.close()  

def clear_artisans():
    conn = get_db_connection()
    conn.execute("DELETE FROM artisans")
    conn.commit()
    conn.close()

def insert_sample_artisans():
    conn = get_db_connection()

    conn.execute("INSERT INTO artisans (name, skill, location) VALUES (?, ?, ?)",
                 ("John Electrician", "Electrician", "Lagos"))

    conn.execute("INSERT INTO artisans (name, skill, location) VALUES (?, ?, ?)",
                 ("Mary Tailor", "Fashion Designer", "Abuja"))

    conn.execute("INSERT INTO artisans (name, skill, location) VALUES (?, ?, ?)",
                 ("James Plumber", "Plumber", "Port Harcourt")) 

    conn.commit()
    conn.close()                                      

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/artisans")
def artisans():
    search = request.args.get('search')

    conn = get_db_connection()

    if search:
        artisans = conn.execute(
            "SELECT * FROM artisans WHERE skill LIKE ?",
            ('%' + search + '%',)
        ).fetchall()
    else:
        artisans = conn.execute("SELECT * FROM artisans WHERE name IS NOT NULL AND name != ''").fetchall()

    conn.close()

    return render_template("artisans.html", artisans=artisans) 

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
        skill = request.form["skill"]

        city = request.form["city"]
        state = request.form["state"]

        location = city + ", " + state

        experience = request.form["experience"]
        phone = request.form.get("phone", "")
        whatsapp = request.form.get("whatsapp", "")
        description = request.form["description"]

        conn = get_db_connection()

        existing = conn.execute(
            "SELECT * FROM artisans WHERE LOWER(name)=LOWER(?) AND LOWER(skill)=LOWER(?) AND phone = ?",
            (name.strip(), skill.strip(), phone.strip())
        ).fetchone()

        if existing:
            conn.close()
            return render_template("add_artisan.html", error="An artisan with that name, skill, and phone number already exists.")

        conn.execute(
            "INSERT INTO artisans (name, email, skill, location, experience, phone, whatsapp, description, image) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, email, skill, location, experience, phone, whatsapp, description, filename)
        )
        conn.commit()
        conn.close()

        return redirect(url_for('artisans'))
    return render_template("add_artisan.html")    

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        fullname = request.form["fullname"]
        email = request.form["email"]
        password = request.form["password"].encode('utf-8')
        hashed_password = bcrypt.hashpw(password, bcrypt.gensalt())

        conn = get_db_connection()
        conn.execute(
            "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
            (fullname, email, hashed_password)
        )
        conn.commit()
        conn.close()

        return redirect(url_for('home'))

    return render_template("register.html")       

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        conn.close()

        if user:
            if bcrypt.checkpw(password.encode('utf-8'), user["password"]):
                session["user_id"] = user["id"]
                return redirect(url_for('show_users', message="Login successful"))
            else:
                return render_template("login.html", error="Incorrect password")
        else:
                return render_template("login.html", error="User not found")

    return render_template("login.html")

@app.route("/users")
def show_users():
    message = request.args.get("message")
    
    if "user_id" not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    users = conn.execute("SELECT * FROM users").fetchall()

    conn.close()
    return render_template("users.html", users=users, message=message)

@app.route("/delete/<int:id>", methods=["POST"])
def delete_user(id):
    if "user_id" not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    return redirect(url_for('show_users'))

@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_user(id):
    conn = get_db_connection()

    # Get the current user
    user = conn.execute("SELECT * FROM users WHERE id = ?", (id, )).fetchone()

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]

        conn.execute(
            "UPDATE users SET name = ?, email = ? WHERE id = ?",
            (name, email, id)
        )
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
        rating = request.form['rating']
        comment = request.form['comment']
        conn.execute(
            "INSERT INTO reviews (artisan_id, rating, comment) VALUES (?, ?, ?)",
            (id, rating, comment)
        )
        conn.commit()
        conn.close()
        return redirect(url_for('artisan_profile', id=id))

    artisan_row = conn.execute("SELECT * FROM artisans WHERE id = ?", (id,)).fetchone()
    if artisan_row is None:
        conn.close()
        return render_template('404.html'), 404
    artisan = dict(artisan_row)

    reviews = conn.execute(
        "SELECT * FROM reviews WHERE artisan_id = ? ORDER BY created_at DESC", (id,)
    ).fetchall()
    conn.close()

    review_count = len(reviews)
    average_rating = round(sum(r['rating'] for r in reviews) / review_count, 1) if reviews else 0

    return render_template(
        'artisan_profile.html',
        artisan=artisan,
        reviews=reviews,
        average_rating=average_rating,
        review_count=review_count
    )

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    message = ""

    if request.method == 'POST':
        email = request.form.get('email')
        new_password = request.form.get('new_password')

        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if user:
            hashed_password = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
            conn.execute("UPDATE users SET password = ? WHERE email = ?", (hashed_password, email))
            conn.commit()
            message = "Password updated successfully!"
        else:
            message = "Email not found!"

        conn.close()

    return render_template('forgot_password.html', message=message)


if __name__ == "__main__":
    create_table()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")