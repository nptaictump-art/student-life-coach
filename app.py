import os
import pickle
from datetime import date, timedelta, datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session
from dateutil import parser
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from flask_sqlalchemy import SQLAlchemy

# === CẤU HÌNH CƠ BẢN ===
app = Flask(__name__)
app.secret_key = 'student_coach_final_2025'
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # Cho phép HTTP khi dev

# === GOOGLE OAUTH2 CONFIG ===
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'openid'
]

CREDENTIALS_FILE = 'credentials.json'
GOOGLE_ENABLED = os.path.exists(CREDENTIALS_FILE)

flow = None
if GOOGLE_ENABLED:
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri='https://glowing-space-giggle-jjp4wj54p7pjhj7j5-5001.app.github.dev/oauth2callback'
    )

# === CẤU HÌNH DATABASE ===
DATABASE_URL = os.getenv("DATABASE_URL")
print("🧠 DATABASE_URL from environment:", DATABASE_URL)


if DATABASE_URL:
    # Chuẩn hóa URL và thêm driver psycopg
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    # Chỉ dùng SQLite khi local dev
    DATABASE_URL = "sqlite:///local.db"
    print("⚠️ Using SQLite fallback!")


app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# === MODEL MẪU (có thể mở rộng) ===
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# === HỖ TRỢ GOOGLE CALENDAR ===
def get_token_filename(email):
    safe_email = email.replace('@', '_').replace('.', '_')
    return f'token_{safe_email}.pickle'


def get_google_calendar_service():
    if not GOOGLE_ENABLED:
        return None

    email = session.get('google_email')
    token_file = get_token_filename(email) if email else 'token.pickle'
    creds = None

    if os.path.exists(token_file):
        with open(token_file, 'rb') as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flash("⚠️ Bạn chưa xác thực với Google Calendar.", "warning")
            return None

    return build('calendar', 'v3', credentials=creds)


# === DASHBOARD ===
@app.route('/')
def dashboard():
    user = {'streak': 5, 'total_points': 120, 'email': 'student@example.com'}
    completion_rate = 85
    days = [(date.today() - timedelta(days=i)).strftime("%d/%m") for i in range(6, -1, -1)]
    counts = [2, 1, 3, 2, 0, 2, 3]

    events = []
    if 'google_email' in session:
        service = get_google_calendar_service()
        if service:
            now = datetime.utcnow().isoformat() + 'Z'
            week_ahead = (datetime.utcnow() + timedelta(days=7)).isoformat() + 'Z'
            results = service.events().list(
                calendarId='primary',
                timeMin=now,
                timeMax=week_ahead,
                maxResults=50,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = results.get('items', [])

    return render_template(
        'dashboard.html',
        user=user,
        completion_rate=completion_rate,
        days=days,
        counts=counts,
        google_enabled=GOOGLE_ENABLED,
        authenticated=('google_email' in session),
        events=events
    )


@app.route('/add_event')
def add_event_form():
    return render_template('add_event.html', google_enabled=GOOGLE_ENABLED)


@app.route('/add_event', methods=['POST'])
def add_event():
    title = request.form['title']
    date_str = request.form['date']
    start_time = request.form['start_time']
    end_time = request.form['end_time']
    description = request.form.get('description', '')

    if not GOOGLE_ENABLED:
        flash('ℹ️ Google Calendar chỉ hoạt động khi có credentials.json.', 'info')
        return redirect(url_for('add_event_form'))

    try:
        start_dt = parser.parse(f"{date_str} {start_time}")
        end_dt = parser.parse(f"{date_str} {end_time}")
        service = get_google_calendar_service()
        if not service:
            return redirect(url_for('authorize'))

        event = {
            'summary': title,
            'description': description,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Ho_Chi_Minh'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Ho_Chi_Minh'},
        }

        created_event = service.events().insert(calendarId='primary', body=event).execute()
        flash(f'✅ Đã tạo sự kiện "{title}" thành công! Xem tại: {created_event.get("htmlLink")}', 'success')

    except Exception as e:
        flash(f'❌ Lỗi khi tạo sự kiện: {str(e)}', 'error')
        print("Chi tiết lỗi:", e)

    return redirect(url_for('add_event_form'))


@app.route('/authorize')
def authorize():
    if not GOOGLE_ENABLED:
        flash("⚠️ Thiếu credentials.json — không thể xác thực Google Calendar.", "error")
        return redirect(url_for('dashboard'))

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    session['state'] = state
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    global flow
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        service = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()

        email = user_info.get('email')
        session['google_email'] = email

        token_file = get_token_filename(email)
        with open(token_file, 'wb') as token:
            pickle.dump(creds, token)

        # Lưu user vào DB nếu chưa tồn tại
        if not User.query.filter_by(email=email).first():
            db.session.add(User(email=email))
            db.session.commit()

        flash(f"✅ Đăng nhập thành công với {email}!", "success")
    except Exception as e:
        flash(f"❌ Google authentication error: {str(e)}", "error")
        print("Chi tiết lỗi:", e)

    return redirect(url_for('dashboard'))


@app.route('/logout_google')
def logout_google():
    session.pop('google_email', None)
    flash("👋 Đã ngắt kết nối Google Calendar.", "info")
    return redirect(url_for('dashboard'))


@app.route('/mark_complete')
def mark_complete():
    flash('🔥 Đã đánh dấu hoàn thành hôm nay!', 'success')
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
