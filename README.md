# Artisan Service App

A web application that connects users with skilled artisans. Users can browse artisans by skill, view profiles, leave reviews, and contact artisans directly.

---

## Features

- Browse and search artisans by skill
- Artisan profiles with ratings, reviews, and contact options (call & WhatsApp)
- User registration and login with secure password hashing (bcrypt)
- Artisan registration with profile picture upload
- Leave star ratings and reviews on artisan profiles
- Password reset via email lookup
- Admin user management (edit/delete users)

---

## Tech Stack

- **Backend:** Python, Flask
- **Database:** SQLite3
- **Frontend:** HTML, CSS, Jinja2 templating, Font Awesome icons
- **Auth:** bcrypt password hashing, Flask sessions

---

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/Grace-goodluck/ARTISANS-.git
cd ARTISANS-
```

### 2. Create a virtual environment

```bash
python -m venv venv
venv\Scripts\activate      # Windows
source venv/bin/activate   # Mac/Linux
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up environment variables

Copy `.env.example` to `.env` and update the values:

```bash
copy .env.example .env
```

Edit `.env`:

```
SECRET_KEY=your-secret-key-here
FLASK_DEBUG=false
```

> Generate a secure secret key with: `python -c "import secrets; print(secrets.token_hex(32))"`

### 5. Run the app

```bash
python app.py
```

Visit **http://127.0.0.1:5000** in your browser.

---

## Project Structure

```
artisan-service-app/
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (not committed)
├── .env.example            # Environment variable template
├── database.db             # SQLite database (auto-created on first run)
├── static/
│   ├── style.css           # Stylesheet
│   ├── logo.jpeg           # App logo
│   └── uploads/            # Uploaded artisan profile pictures
└── templates/
    ├── home.html
    ├── artisans.html
    ├── artisan_profile.html
    ├── add_artisan.html
    ├── register.html
    ├── login.html
    ├── forgot_password.html
    ├── users.html
    ├── edit.html
    └── 404.html
```

---

## Database Schema

**users** — registered app users  
**artisans** — registered artisan profiles  
**reviews** — ratings and comments left on artisan profiles

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Flask session secret key | required, no default |
| `FLASK_DEBUG` | Enable debug mode (`true`/`false`) | `false` |
| `DATABASE_URL` | PostgreSQL connection string (unset = local SQLite) | none |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Cloudinary image hosting credentials | required, no default |
| `PAYSTACK_SECRET_KEY` | Paystack secret key — enables online payments for service packages | none (payments disabled if unset) |
| `PAYSTACK_PUBLIC_KEY` | Paystack public key | none |

To finish Paystack setup, add `https://<your-domain>/paystack/webhook` as the webhook URL in the Paystack dashboard (Settings → API Keys & Webhooks), so payments are confirmed server-side even if a client closes the tab before the redirect back.

---

## Notes

- The `database.db` file and `static/uploads/` folder are excluded from version control via `.gitignore`
- Run the database migration commands if upgrading from an older version that lacks `email`/`whatsapp` columns on the artisans table:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('database.db')
conn.execute('ALTER TABLE artisans ADD COLUMN email TEXT')
conn.execute('ALTER TABLE artisans ADD COLUMN whatsapp TEXT')
conn.commit()
conn.close()
print('Migration complete')
"
```
