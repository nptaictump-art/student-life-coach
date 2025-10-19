import os
import io
import pickle
from datetime import date, timedelta, datetime
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, send_file
)
from dateutil import parser
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from flask_sqlalchemy import SQLAlchemy

# =========================
# CẤU HÌNH ỨNG DỤNG & SESSION
# =========================
app = Flask(__name__)

# Secret key phải cố định để session không mất giữa 2 request OAuth
app.secret_key = os.getenv("FLASK_SECRET_KEY", "student_coach_final_2025")

# Cookie cấu hình cho HTTPS (Render) để tránh mất session -> lỗi CSRF state
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"

# Cho phép HTTP khi dev (local/Codespaces)
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
# KẾT NỐI DATABASE (PostgreSQL trên Render)
# =========================
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    # Chuẩn hóa: postgres:// -> postgresql+psycopg://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    # Fallback local
    DATABASE_URL = "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# =========================
# MODEL ĐƠN GIẢN
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
# HỖ TRỢ GOOGLE CALENDAR
# =========================
def build_flow(redirect_uri: str, state: str | None = None) -> Flow:
    """Tạo Flow mới từ credentials.json mỗi lần cần (tránh lỗi state)."""
    if not GOOGLE_ENABLED:
        return None
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, redirect_uri=redirect_uri)
    if state:
        flow.oauth2session._state = state  # gán state cũ vào lại flow
    return flow

def get_token_filename(email):
    safe_email = (email or "anonymous").replace("@", "_").replace(".", "_")
    return f"token_{safe_email}.pickle"

def get_google_calendar_service():
    """Trả về đối tượng Calendar API nếu đã xác thực."""
    if not GOOGLE_ENABLED:
        return None
    email = session.get("google_email")
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
# TRANG CHÍNH
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
# FORM TẠO SỰ KIỆN ĐƠN LẺ
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

        created_event = service.events().insert(calendarId="primary", body=event).execute()
        flash(f'✅ Đã tạo sự kiện "{title}"! Xem tại: {created_event.get("htmlLink")}', "success")
    except Exception as e:
        flash(f"❌ Lỗi khi tạo sự kiện: {str(e)}", "error")
    return redirect(url_for("add_event_form"))

# =========================
# OAUTH FLOW
# =========================
def _redirect_base():
    # Tự suy luận domain hiện tại cho redirect_uri
    # Ưu tiên biến môi trường, nếu không thì lấy từ request.host_url
    host = os.getenv("PUBLIC_BASE_URL")
    if not host:
        # request.host_url ví dụ: https://student-life-coach-mwsd.onrender.com/
        host = request.host_url.rstrip("/")
    return f"{host}/oauth2callback"

@app.route("/authorize")
def authorize():
    if not GOOGLE_ENABLED:
        flash("⚠️ Thiếu credentials.json — không thể xác thực Google Calendar.", "error")
        return redirect(url_for("dashboard"))

    flow = build_flow(redirect_uri=_redirect_base())
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["state"] = state
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    if not GOOGLE_ENABLED:
        flash("⚠️ Thiếu credentials.json — không thể xác thực Google Calendar.", "error")
        return redirect(url_for("dashboard"))

    try:
        # Rebuild Flow với state cũ để tránh mismatching_state
        state = session.get("state")
        flow = build_flow(redirect_uri=_redirect_base(), state=state)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        # Lấy info user
        oauth2 = build("oauth2", "v2", credentials=creds)
        user_info = oauth2.userinfo().get().execute()
        email = user_info.get("email")
        session["google_email"] = email

        # Lưu token theo email
        token_file = get_token_filename(email)
        with open(token_file, "wb") as f:
            pickle.dump(creds, f)

        # Lưu user vào DB
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
# UPLOAD EXCEL -> TẠO SỰ KIỆN GOOGLE CALENDAR
# =========================
# Yêu cầu: pandas + openpyxl trong requirements.txt
import pandas as pd

@app.route("/upload")
def upload_form():
    # Trang upload
    return render_template(
        "upload.html",
        google_enabled=GOOGLE_ENABLED,
        authenticated=("google_email" in session)
    )

@app.route("/upload", methods=["POST"])
def upload_process():
    if "google_email" not in session:
        flash("⚠️ Hãy đăng nhập Google trước khi import lịch.", "warning")
        return redirect(url_for("upload_form"))

    if "file" not in request.files or request.files["file"].filename == "":
        flash("❌ Vui lòng chọn file Excel (.xlsx) hoặc CSV.", "error")
        return redirect(url_for("upload_form"))

    file = request.files["file"]
    filename = file.filename.lower()

    try:
        # Đọc dữ liệu
        if filename.endswith(".xlsx"):
            df = pd.read_excel(file)
        elif filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            flash("❌ Định dạng không hỗ trợ. Chỉ nhận .xlsx hoặc .csv", "error")
            return redirect(url_for("upload_form"))

        # Chuẩn cột: chấp nhận cả hoa/thường
        cols_map = {c.strip().lower(): c for c in df.columns}
        required = [
            "số thứ tự",
            "ngày",
            "tháng",
            "năm",
            "giờ",
            "nội dung nhắc nhở",
            "thời gian nhắc nhở (phút trước)",
            "thời gian kết thúc (hh:mm)",
        ]
        for col in required:
            if col not in [k.strip().lower() for k in df.columns]:
                flash(f"❌ Thiếu cột bắt buộc: {col}", "error")
                return redirect(url_for("upload_form"))

        service = get_google_calendar_service()
        if not service:
            return redirect(url_for("authorize"))

        successes = 0
        failures = 0
        tz = "Asia/Ho_Chi_Minh"

        for _, row in df.iterrows():
            try:
                ngay = int(row[cols_map["ngày"]])
                thang = int(row[cols_map["tháng"]])
                nam = int(row[cols_map["năm"]])

                gio_bat_dau = str(row[cols_map["giờ"]]).strip()  # "HH:MM"
                gio_ket_thuc = str(row[cols_map["thời gian kết thúc (hh:mm)"]]).strip()
                minutes_before = int(row[cols_map["thời gian nhắc nhở (phút trước)"]])

                start_dt = parser.parse(f"{nam}-{thang:02d}-{ngay:02d} {gio_bat_dau}")
                end_dt = parser.parse(f"{nam}-{thang:02d}-{ngay:02d} {gio_ket_thuc}")

                title = str(row[cols_map["nội dung nhắc nhở"]]).strip()

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

        # Lưu log
        db.session.add(ImportLog(
            email=session.get("google_email"),
            total_rows=len(df),
            success=successes,
            failed=failures
        ))
        db.session.commit()

        flash(f"✅ Import xong! Thành công: {successes}, lỗi: {failures}.", "success")
    except Exception as e:
        flash(f"❌ Lỗi khi xử lý file: {str(e)}", "error")

    return redirect(url_for("upload_form"))

# Cho phép tải file mẫu (nếu bạn muốn cung cấp qua server)
@app.route("/download-template")
def download_template():
    # Trả file mẫu giống bản mình đã gửi kèm trong cuộc trò chuyện
    # Nếu bạn muốn ship sẵn từ server: có thể generate động như sau
    import pandas as pd
    cols = [
        "Số thứ tự",
        "ngày",
        "tháng",
        "năm",
        "giờ",
        "nội dung nhắc nhở",
        "thời gian nhắc nhở (phút trước)",
        "thời gian kết thúc (HH:MM)"
    ]
    sample = [
        [1, 20, 10, 2025, "08:30", "Học Toán chương 3", 15, "09:30"],
        [2, 21, 10, 2025, "14:00", "Nộp bài báo cáo môn Lý", 30, "15:00"],
    ]
    df = pd.DataFrame(sample, columns=cols)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="mau_import_lich_hoc.xlsx",
    )

# =========================
# MỘT SỐ NÚT NHANH
# =========================
@app.route("/mark_complete")
def mark_complete():
    flash("🔥 Đã đánh dấu hoàn thành hôm nay!", "success")
    return redirect(url_for("dashboard"))

# =========================
# CHẠY ỨNG DỤNG
# =========================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
