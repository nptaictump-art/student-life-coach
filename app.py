import os
import pickle
from datetime import datetime, date, timedelta
from flask import Flask, render_template, flash, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --- Cấu hình ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'student_coach_preview'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instance/app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- DB ---
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    streak = db.Column(db.Integer, default=0)
    total_points = db.Column(db.Integer, default=0)
    last_study_date = db.Column(db.Date)

class StudyEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    completed = db.Column(db.Boolean, default=False)

# --- Google Calendar ---
SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_service():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('calendar', 'v3', credentials=creds)

# --- Helper ---
def update_streak_and_points(user):
    today = date.today()
    if user.last_study_date == today:
        return
    elif user.last_study_date == today - timedelta(days=1):
        user.streak += 1
    else:
        user.streak = 1
    user.last_study_date = today
    user.total_points += 10 + user.streak
    db.session.commit()

# --- Routes ---
@app.route('/')
def dashboard():
    user = User.query.first()
    if not user:
        user = User(email="student@example.com", streak=5, total_points=120, last_study_date=date.today())
        db.session.add(user)
        # Thêm dữ liệu mẫu cho biểu đồ
        for i in range(6, -1, -1):
            d = date.today() - timedelta(days=i)
            count = 2 if i % 2 == 0 else 1
            for _ in range(count):
                ev = StudyEvent(
                    user_id=user.id,
                    title="Học Python",
                    start_time=datetime.combine(d, datetime.min.time()),
                    end_time=datetime.combine(d, datetime.min.time()),
                    completed=True
                )
                db.session.add(ev)
        db.session.commit()

    # Tính % hoàn thành (giả lập)
    completion_rate = min(100, 70 + user.streak * 5)

    # Dữ liệu biểu đồ 7 ngày
    days = []
    counts = []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        days.append(d.strftime("%d/%m"))
        cnt = StudyEvent.query.filter(
            StudyEvent.user_id == user.id,
            StudyEvent.completed == True,
            db.func.date(StudyEvent.start_time) == d
        ).count()
        counts.append(cnt)

    return render_template('dashboard.html',
                           user=user,
                           completion_rate=completion_rate,
                           days=days,
                           counts=counts)

@app.route('/mark_complete')
def mark_complete():
    user = User.query.first()
    update_streak_and_points(user)
    flash('🎉 Đã cập nhật streak và điểm thưởng!', 'success')
    return redirect(url_for('dashboard'))

# --- Tạo DB ---
with app.app_context():
    os.makedirs('instance', exist_ok=True)
    db.create_all()
    if not User.query.first():
        user = User(email="student@example.com")
        db.session.add(user)
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=True)