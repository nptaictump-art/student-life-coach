import os
import io
import pickle
import pandas as pd
from datetime import date, timedelta, datetime
from dateutil import parser
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from flask_sqlalchemy import SQLAlchemy

# =========================
# ⚙️ CẤU HÌNH CƠ BẢN
# =========================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "student_coach_final_2025")
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # Cho phép HTTP khi dev

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]

CREDENTIALS_FILE = "credentials.json"
GOOGLE_ENABLED = os.path.exists(CREDENTIALS_FILE)

# =========================
# 🗄️ DATABASE CONFIG
# =========================
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# =========================
# 👤 MODEL
# =========================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ImportLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255))
    total_rows = db.Column(db.Integer, default=0)
    success = db.Column(db.Integer, default=0)
    failed = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# =========================
# 🔑 GOOGLE AUTH HELPER
# =========================
def build_flow(redirect_uri: str, state: str | None = None):
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

# =========================
# 🏠 DASHBOARD
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
# ➕ THÊM SỰ KIỆN THỦ CÔNG
# =========================
@app.route("/add_event")
def add_event_form():
    return render_template("add_event.html", google_enabled=GOOGLE_ENABLED, authenticated=("google_email" in session))

@app.route("/add_event", methods=["POST"])
def add_event():
    title = request.form["title"]
    date_str = request.form["date"]
    start_time = request.form["start_time"]
    end_time = request.form["end_time"]
    description = request.form.get("description", "")

    if not GOOGLE_ENABLED:
        flash("ℹ️ Google Calendar chỉ hoạt động khi có credentials.json.", "info")
        return redirect(url_for("add_event_form"))

    try:
        start_dt = parser.parse(f"{date_str} {start_time}")
        end_dt = parser.parse(f"{date_str} {end_time}")
        service = get_google_calendar_service()
        if not service:
            return redirect(url_for("authorize"))

        event = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
        }

        service.events().insert(calendarId="primary", body=event).execute()
        flash(f'✅ Đã tạo sự kiện "{title}" thành công!', "success")
    except Exception as e:
        flash(f"❌ Lỗi khi tạo sự kiện: {str(e)}", "error")

    return redirect(url_for("add_event_form"))

# =========================
# 🔐 GOOGLE LOGIN / LOGOUT
# =========================
def _redirect_base():
    host = os.getenv("PUBLIC_BASE_URL")
    if not host:
        host = request.host_url.rstrip("/")
    return f"{host}/oauth2callback"

@app.route("/authorize")
def authorize():
    """Khởi tạo luồng OAuth và yêu cầu quyền truy cập Google Calendar."""
    if not GOOGLE_ENABLED:
        flash("⚠️ Thiếu credentials.json — không thể xác thực Google Calendar.", "error")
        return redirect(url_for("dashboard"))

    # Xác định redirect URI phù hợp (Render hoặc local)
    host = os.getenv("PUBLIC_BASE_URL") or request.host_url.rstrip("/")
    redirect_uri = f"{host}/oauth2callback"

    # Tạo flow mới mỗi lần (tránh lỗi state cũ)
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    # 🧩 Xóa token cũ nếu scope đã đổi (tự động)
    email = session.get("google_email")
    if email:
        token_file = get_token_filename(email)
        if os.path.exists(token_file):
            try:
                import pickle
                with open(token_file, "rb") as f:
                    creds = pickle.load(f)
                # So sánh scope hiện tại của creds với SCOPES trong app
                current_scopes = set(creds.scopes or [])
                desired_scopes = set(SCOPES)
                if current_scopes != desired_scopes:
                    os.remove(token_file)
                    print(f"🗑️ Đã xoá token cũ vì scope thay đổi: {current_scopes} → {desired_scopes}")
            except Exception as e:
                print("⚠️ Không thể đọc token cũ:", e)
                try:
                    os.remove(token_file)
                except:
                    pass

    # 🔒 Luôn ép xác thực lại quyền truy cập (dù đã login trước)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",   # ép cấp lại quyền đầy đủ
        prompt="consent"                  # bắt buộc hiện lại màn hình xác nhận
    )

    session["state"] = state
    print(f"🌐 [DEBUG] OAuth redirect URI: {redirect_uri}")
    print(f"📡 [DEBUG] Generated state: {state}")

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

        token_file = get_token_filename(email)
        with open(token_file, "wb") as f:
            pickle.dump(creds, f)

        try:
            if not User.query.filter_by(email=email).first():
                db.session.add(User(email=email))
                db.session.commit()
        except Exception:
            pass

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
# 📤 UPLOAD EXCEL & IMPORT
# =========================
@app.route("/upload")
def upload_form():
    return render_template("upload.html", google_enabled=GOOGLE_ENABLED, authenticated=("google_email" in session))

@app.route("/upload", methods=["POST"])
def upload_process():
    if "google_email" not in session:
        flash("⚠️ Hãy đăng nhập Google trước khi import lịch.", "warning")
        return redirect(url_for("upload_form"))

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("❌ Vui lòng chọn file Excel (.xlsx) hoặc CSV.", "error")
        return redirect(url_for("upload_form"))

    try:
        df = pd.read_excel(file)
        service = get_google_calendar_service()
        if not service:
            return redirect(url_for("authorize"))

        successes = 0
        tz = "Asia/Ho_Chi_Minh"

        for _, row in df.iterrows():
            try:
                ngay, thang, nam = int(row["ngày"]), int(row["tháng"]), int(row["năm"])
                gio = str(row["giờ"])
                title = str(row["nội dung nhắc nhở"])
                nhac_truoc = int(row.get("thời gian nhắc nhở", 10))
                gio_ket_thuc = str(row.get("thời gian kết thúc", "")) or (datetime.strptime(gio, "%H:%M") + timedelta(minutes=60)).strftime("%H:%M")

                start_dt = parser.parse(f"{nam}-{thang}-{ngay} {gio}")
                end_dt = parser.parse(f"{nam}-{thang}-{ngay} {gio_ket_thuc}")

                event = {
                    "summary": title,
                    "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
                    "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
                    "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": nhac_truoc}]},
                }
                service.events().insert(calendarId="primary", body=event).execute()
                successes += 1
            except Exception:
                pass

        flash(f"✅ Đã import {successes} sự kiện từ file Excel!", "success")
    except Exception as e:
        flash(f"❌ Lỗi khi xử lý file: {str(e)}", "error")

    return redirect(url_for("upload_form"))

@app.route("/download_template")
def download_template():
    cols = ["số thứ tự", "ngày", "tháng", "năm", "giờ", "nội dung nhắc nhở", "thời gian nhắc nhở", "thời gian kết thúc"]
    df = pd.DataFrame([[1, 20, 10, 2025, "08:00", "Học toán", 10, "09:00"]], columns=cols)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="mau_import_lich_hoc.xlsx")

# =========================
# 🚀 RUN
# =========================
@app.route("/mark_complete")
def mark_complete():
    flash("🔥 Đã đánh dấu hoàn thành hôm nay!", "success")
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
