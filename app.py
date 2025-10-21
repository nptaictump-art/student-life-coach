import os
import io
import json
import pickle
from datetime import date, timedelta, datetime

import pandas as pd
from dateutil import parser
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_file
)
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from flask_sqlalchemy import SQLAlchemy

# ==== OpenAI (API v1.x) ====
try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except Exception:
    _OPENAI_AVAILABLE = False
    OpenAI = None  # type: ignore


def _openai_client_or_none():
    """Khởi tạo client OpenAI nếu có API key hợp lệ."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not _OPENAI_AVAILABLE or not api_key:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


# =========================
# CẤU HÌNH ỨNG DỤNG
# =========================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "student_coach_final_2025")

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

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
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = DATABASE_URL or "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
print("✅ DATABASE_URL in use:", DATABASE_URL)


# =========================
# MODEL
# =========================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# =========================
# GOOGLE CALENDAR TIỆN ÍCH
# =========================
def _redirect_base() -> str:
    host = os.getenv("PUBLIC_BASE_URL")
    if not host:
        host = request.host_url.rstrip("/")
    if "app.github.dev" in host and not host.startswith("https://"):
        host = host.replace("http://", "https://", 1)
    return f"{host}/oauth2callback"


def build_flow(redirect_uri: str, state: str | None = None) -> Flow | None:
    if not GOOGLE_ENABLED:
        return None
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, redirect_uri=redirect_uri)
    if state:
        flow.oauth2session._state = state
    return flow


def get_token_filename(email: str | None) -> str:
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
            try:
                os.remove(file)
            except Exception:
                pass


# =========================
# DASHBOARD
# =========================
@app.route("/")
def dashboard():
    user = {
        "streak": 5,
        "total_points": 120,
        "email": session.get("google_email") or "student@example.com",
    }
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
                orderBy="startTime",
            ).execute()
            events = results.get("items", [])

    return render_template(
        "dashboard.html",
        user=user,
        completion_rate=completion_rate,
        days=days,
        counts=counts,
        google_enabled=GOOGLE_ENABLED,
        authenticated=("google_email" in session),
        events=events,
    )


# =========================
# GOOGLE AUTH
# =========================
@app.route("/authorize")
def authorize():
    if not GOOGLE_ENABLED:
        flash("⚠️ Thiếu credentials.json — không thể xác thực Google Calendar.", "error")
        return redirect(url_for("dashboard"))
    clear_old_tokens()
    flow = build_flow(redirect_uri=_redirect_base())
    authorization_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
    )
    session["state"] = state
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    if not GOOGLE_ENABLED:
        flash("⚠️ Thiếu credentials.json — không thể xác thực Google Calendar.", "error")
        return redirect(url_for("dashboard"))
    try:
        state = session.get("state")
        flow = build_flow(redirect_uri=_redirect_base(), state=state)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        oauth2 = build("oauth2", "v2", credentials=creds)
        user_info = oauth2.userinfo().get().execute()
        email = user_info.get("email")
        session["google_email"] = email
        with open(get_token_filename(email), "wb") as f:
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
# THÊM LỊCH HỌC & UPLOAD FILE
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
        try:
            start_dt = parser.parse(f"{date_str} {start_time}")
            end_dt = parser.parse(f"{date_str} {end_time}")
            event = {
                "summary": title,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
                "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
            }
            service.events().insert(calendarId="primary", body=event).execute()
            flash(f"✅ Đã tạo sự kiện: {title}", "success")
        except Exception as e:
            flash(f"❌ Lỗi khi tạo sự kiện: {str(e)}", "error")
        return redirect(url_for("dashboard"))
    return render_template("add_event.html", google_enabled=GOOGLE_ENABLED, authenticated=("google_email" in session))


@app.route("/add_event_form")
def add_event_form():
    return redirect(url_for("add_event"))


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("⚠️ Chưa chọn file.", "warning")
            return redirect(url_for("upload"))
        try:
            filename = file.filename.lower()
            df = pd.read_excel(file) if filename.endswith(".xlsx") else pd.read_csv(file)
        except Exception as e:
            flash(f"❌ Không đọc được file: {str(e)}", "error")
            return redirect(url_for("upload"))
        service = get_google_calendar_service()
        if not service:
            flash("⚠️ Hãy kết nối Google trước.", "warning")
            return redirect(url_for("authorize"))
        norm_cols = {c.strip().lower(): c for c in df.columns}
        required = ["ngày", "tháng", "năm", "giờ", "nội dung nhắc nhở", "thời gian nhắc nhở", "thời gian kết thúc"]
        for col in required:
            if col not in norm_cols:
                flash(f"❌ Thiếu cột bắt buộc: {col}", "error")
                return redirect(url_for("upload"))
        successes, failures = 0, 0
        tz = "Asia/Ho_Chi_Minh"
        for _, row in df.iterrows():
            try:
                day = int(row[norm_cols["ngày"]])
                month = int(row[norm_cols["tháng"]])
                year = int(row[norm_cols["năm"]])
                title = str(row[norm_cols["nội dung nhắc nhở"]]).strip()
                start_dt = parser.parse(f"{year}-{month:02d}-{day:02d} {row[norm_cols['giờ']]}")
                end_dt = parser.parse(f"{year}-{month:02d}-{day:02d} {row[norm_cols['thời gian kết thúc']]}")
                minutes_before = int(row[norm_cols["thời gian nhắc nhở"]])
                event = {
                    "summary": title,
                    "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
                    "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
                    "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": minutes_before}]},
                }
                service.events().insert(calendarId="primary", body=event).execute()
                successes += 1
            except Exception:
                failures += 1
        flash(f"✅ Import xong! Thành công: {successes}, lỗi: {failures}.", "success")
        return redirect(url_for("dashboard"))
    return render_template("upload.html", google_enabled=GOOGLE_ENABLED, authenticated=("google_email" in session))


@app.route("/download-template")
def download_template():
    cols = ["ngày", "tháng", "năm", "giờ", "nội dung nhắc nhở", "thời gian nhắc nhở", "thời gian kết thúc"]
    sample = [[20, 10, 2025, "08:00", "Ôn tập Toán", 15, "09:00"]]
    df = pd.DataFrame(sample, columns=cols)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="mau_import_lich_hoc.xlsx")


# =========================
# AI QUIZ GENERATOR
# =========================
@app.route("/generate_quiz", methods=["GET", "POST"])
def generate_quiz():
    if request.method == "POST":
        topic = request.form.get("topic", "").strip()
        if not topic:
            flash("⚠️ Vui lòng nhập chủ đề/bài học.", "warning")
            return redirect(url_for("generate_quiz"))
        client = _openai_client_or_none()
        if not client:
            quiz = [{"question": f"[FAKE AI] Câu {i} về {topic}?", "options": [f"{opt}. Lựa chọn" for opt in "ABCD"], "correct_answer": "A"} for i in range(1, 11)]
        else:
            prompt = f"""
            Tạo 10 câu hỏi trắc nghiệm tiếng Việt về chủ đề "{topic}".
            Mỗi câu có 4 lựa chọn A-D, 1 đáp án đúng.
            Trả JSON chuẩn:
            [{{"question":"...","options":["A. ...","B. ...","C. ...","D. ..."],"correct_answer":"A"}}]
            """
            try:
                resp = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.5)
                quiz = json.loads(resp.choices[0].message.content)
            except Exception:
                quiz = [{"question": f"[Fallback] Câu {i} về {topic}?", "options": [f"{opt}. Đáp án" for opt in "ABCD"], "correct_answer": "A"} for i in range(1, 11)]
        session["quiz"], session["topic"] = quiz, topic
        return render_template("quiz.html", quiz=quiz, topic=topic)
    return render_template("generate_quiz.html")


@app.route("/submit_quiz", methods=["POST"])
def submit_quiz():
    quiz, topic = session.get("quiz", []), session.get("topic", "Bài học")
    score = sum(1 for i, q in enumerate(quiz, 1) if request.form.get(f"q{i}", "").startswith(q["correct_answer"]))
    client = _openai_client_or_none()
    if client:
        try:
            prompt = f"Đánh giá khi học sinh đạt {score}/10 điểm chủ đề '{topic}', bằng tiếng Việt, ngắn, có khích lệ."
            resp = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.4)
            feedback = resp.choices[0].message.content
        except Exception:
            feedback = f"Bạn đạt {score}/10. Hãy xem lại những câu sai và ôn lại nhé."
    else:
        feedback = f"[Fallback] Bạn đạt {score}/10. Tiếp tục cố gắng nhé!"
    return render_template("quiz_result.html", score=score, feedback=feedback, topic=topic)


# =========================
# TIỆN ÍCH KHÁC
# =========================
@app.route("/mark_complete")
def mark_complete():
    flash("🎯 Bạn đã đánh dấu hoàn thành buổi học hôm nay!", "success")
    return redirect(url_for("dashboard"))


@app.route("/healthz")
def healthz():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.route("/upload_form")
def upload_form():
    return redirect(url_for("upload"))


# =========================
# KHỞI TẠO
# =========================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Server running on port {port}")
    app.run(host="0.0.0.0", port=port)
