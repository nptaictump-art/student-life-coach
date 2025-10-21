import os
import io
import json
import pickle
import pandas as pd
from datetime import date, timedelta, datetime
from dateutil import parser
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_file
)
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from flask_sqlalchemy import SQLAlchemy
import openai

# =========================
# CẤU HÌNH ỨNG DỤNG
# =========================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "student_coach_final_2025")

# Cookie cho HTTPS (Render)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"

# Cho phép HTTP khi dev local
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# =========================
# OPENAI CONFIG
# =========================
openai.api_key = os.getenv("OPENAI_API_KEY", "")

# =========================
# GOOGLE OAUTH2 CONFIG
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]
CREDENTIALS_FILE = "credentials.json"
GOOGLE_ENABLED = os.path.exists(CREDENTIALS_FILE)

# =========================
# DATABASE CONFIG
# =========================
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# =========================
# GOOGLE CALENDAR HỖ TRỢ
# =========================
def build_flow(redirect_uri, state=None):
    if not GOOGLE_ENABLED:
        return None
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, redirect_uri=redirect_uri)
    if state:
        flow.oauth2session._state = state
    return flow


def get_token_filename(email):
    safe_email = (email or "anonymous").replace("@", "_").replace(".", "_")
    return f"token_{safe_email}.pickle"


def get_google_calendar_service():
    if not GOOGLE_ENABLED:
        return None
    email = session.get("google_email")
    if not email:
        return None
    token_file = get_token_filename(email)
    creds = None
    if os.path.exists(token_file):
        with open(token_file, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return None
    return build("calendar", "v3", credentials=creds)


def clear_old_tokens():
    for file in os.listdir("."):
        if file.startswith("token_") and file.endswith(".pickle"):
            os.remove(file)


# =========================
# DASHBOARD
# =========================
@app.route("/")
def dashboard():
    user = {"streak": 5, "total_points": 120, "email": session.get("google_email") or "student@example.com"}
    completion_rate = 85
    days = [(date.today() - timedelta(days=i)).strftime("%d/%m") for i in range(6, -1, -1)]
    counts = [2, 1, 3, 2, 0, 2, 3]

    events = []
    if session.get("google_email"):
        service = get_google_calendar_service()
        if service:
            now = datetime.utcnow().isoformat() + "Z"
            week_ahead = (datetime.utcnow() + timedelta(days=7)).isoformat() + "Z"
            results = service.events().list(
                calendarId="primary",
                timeMin=now,
                timeMax=week_ahead,
                maxResults=50,
                singleEvents=True,
                orderBy="startTime"
            ).execute()
            events = results.get("items", [])

    return render_template("dashboard.html",
                           user=user,
                           completion_rate=completion_rate,
                           days=days,
                           counts=counts,
                           google_enabled=GOOGLE_ENABLED,
                           authenticated=("google_email" in session),
                           events=events)


# =========================
# GOOGLE AUTH
# =========================
def _redirect_base():
    host = os.getenv("PUBLIC_BASE_URL")
    if not host:
        host = request.host_url.rstrip("/")
    return f"{host}/oauth2callback"


@app.route("/authorize")
def authorize():
    if not GOOGLE_ENABLED:
        flash("⚠️ Thiếu credentials.json — không thể xác thực Google Calendar.", "error")
        return redirect(url_for("dashboard"))

    clear_old_tokens()  # 🔥 Xoá token cũ khi đổi scope
    flow = build_flow(redirect_uri=_redirect_base())
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    session["state"] = state
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    try:
        state = session.get("state")
        flow = build_flow(redirect_uri=_redirect_base(), state=state)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        service = build("oauth2", "v2", credentials=creds)
        user_info = service.userinfo().get().execute()
        email = user_info.get("email")

        session["google_email"] = email
        token_file = get_token_filename(email)
        with open(token_file, "wb") as f:
            pickle.dump(creds, f)

        if not User.query.filter_by(email=email).first():
            db.session.add(User(email=email))
            db.session.commit()

        flash(f"✅ Đăng nhập thành công với {email}!", "success")
    except Exception as e:
        flash(f"❌ Google authentication error: {str(e)}", "error")

    return redirect(url_for("dashboard"))


@app.route("/logout_google")
def logout_google():
    session.pop("google_email", None)
    flash("👋 Đã ngắt kết nối Google Calendar.", "info")
    return redirect(url_for("dashboard"))


# =========================
# THÊM LỊCH HỌC THỦ CÔNG
# =========================
@app.route("/add_event", methods=["GET", "POST"])
def add_event():
    if request.method == "POST":
        title = request.form["title"]
        date_str = request.form["date"]
        start_time = request.form["start_time"]
        end_time = request.form["end_time"]

        service = get_google_calendar_service()
        if not service:
            flash("⚠️ Bạn cần kết nối Google Calendar trước.", "warning")
            return redirect(url_for("authorize"))

        start_dt = parser.parse(f"{date_str} {start_time}")
        end_dt = parser.parse(f"{date_str} {end_time}")

        event = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
        }
        service.events().insert(calendarId="primary", body=event).execute()
        flash(f"✅ Đã tạo sự kiện: {title}", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_event.html")


# =========================
# UPLOAD FILE EXCEL
# =========================
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        file = request.files["file"]
        if not file or file.filename == "":
            flash("⚠️ Chưa chọn file.", "warning")
            return redirect(url_for("upload"))

        df = pd.read_excel(file)
        service = get_google_calendar_service()
        if not service:
            flash("⚠️ Hãy kết nối Google trước.", "warning")
            return redirect(url_for("authorize"))

        for _, row in df.iterrows():
            title = str(row.get("nội dung nhắc nhở", "Lịch học"))
            day, month, year = int(row["ngày"]), int(row["tháng"]), int(row["năm"])
            hour = row["giờ"]
            remind = int(row.get("thời gian nhắc nhở", 10))
            end_time = row.get("thời gian kết thúc", "")

            start_dt = parser.parse(f"{day}/{month}/{year} {hour}")
            end_dt = parser.parse(end_time) if end_time else start_dt + timedelta(minutes=remind)

            event = {
                "summary": title,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
            }
            service.events().insert(calendarId="primary", body=event).execute()

        flash("✅ Đã import lịch thành công!", "success")
        return redirect(url_for("dashboard"))

    return render_template("upload.html")


@app.route("/download-template")
def download_template():
    cols = ["Số thứ tự", "ngày", "tháng", "năm", "giờ", "nội dung nhắc nhở", "thời gian nhắc nhở", "thời gian kết thúc"]
    df = pd.DataFrame([[1, 20, 10, 2025, "08:00", "Ôn tập Toán", 15, "09:00"]], columns=cols)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="mau_import_lich_hoc.xlsx")


# =========================
# AI QUIZ GENERATOR
# =========================
def generate_quiz_from_ai(topic):
    prompt = f"""
    Tạo 10 câu hỏi trắc nghiệm tiếng Việt về chủ đề "{topic}".
    Mỗi câu hỏi có 4 lựa chọn (A, B, C, D) và một đáp án đúng.
    Trả kết quả JSON: 
    [
      {{
        "question": "Câu hỏi...",
        "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
        "correct_answer": "A"
      }}
    ]
    """
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    return json.loads(response.choices[0].message.content)


@app.route("/generate_quiz", methods=["GET", "POST"])
def generate_quiz():
    if request.method == "POST":
        topic = request.form["topic"]
        try:
            quiz = generate_quiz_from_ai(topic)
            session["quiz"] = quiz
            session["topic"] = topic
            return render_template("quiz.html", quiz=quiz, topic=topic)
        except Exception as e:
            flash(f"❌ Lỗi tạo câu hỏi: {str(e)}", "error")
            return redirect(url_for("generate_quiz"))
    return render_template("generate_quiz.html")


@app.route("/submit_quiz", methods=["POST"])
def submit_quiz():
    quiz = session.get("quiz", [])
    topic = session.get("topic", "Bài học")
    score = 0

    for i, q in enumerate(quiz, 1):
        ans = request.form.get(f"q{i}")
        if ans and ans.startswith(q["correct_answer"]):
            score += 1

    feedback_prompt = f"Đánh giá năng suất học tập khi đạt {score}/10 điểm trong chủ đề '{topic}' bằng tiếng Việt."
    feedback_resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": feedback_prompt}]
    )
    feedback = feedback_resp.choices[0].message.content
    return render_template("quiz_result.html", score=score, feedback=feedback)


# =========================
# KHỞI TẠO
# =========================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
