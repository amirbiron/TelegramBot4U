"""
פאנל הניהול — ‏Flask + ‏HTMX + ‏Jinja2, ‏RTL עברית.

הועתקה חבילת ה-KB מ-`ai-business-bot/admin/app.py` (‏ROADMAP T0.9):
התחברות דו-מסלולית + ‏CSRF (כולל HTMX) + ‏rate limit + ‏audit log, קשירת
ה-tenant ב-`before_request`, ה-routes של בסיס הידע ופערי הידע, הפילטרים
ו-`style.css`.

מה שהשתנה מול המקור:
- **אין `/kb/rebuild`** ואין אינדקס — אין RAG (‏PLAN §3.2).
- **`/kb/search` הוא LIKE ב-SQL** עם escaping של `_` ו-`%` (דפוס קריטי #8),
  במקום חיפוש סמנטי.
- ה-sidebar נבנה מחדש לניווט של הפרויקט הזה.
"""

import hmac
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

# הערכים של config נקראים דרך המודול ולא כקבועים מיובאים: ‏import-time
# binding קופא, ולכן ערך שנטען מאוחר יותר (‏DATA_DIR/.env) או שנדרס
# בטסטים לא היה נתפס. אותו כלל שחל על BUSINESS_NAME (CLAUDE.md).
import config as _cfg
import database as db
from config import get_business_config
from tenancy import DEFAULT_TENANT, reset_current_tenant, set_current_tenant

logger = logging.getLogger(__name__)

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

VALID_UNANSWERED_STATUSES = {"open", "resolved", "not_relevant"}

# מזהה משתמש תקין בערוץ הזה — מזהה טלגרם מספרי. משמש לוולידציה של
# פרמטרים שמגיעים מה-URL לפני שאילתה.
_USER_ID_RE = re.compile(r"^\d{1,20}$")

# ─── ‏rate limit להתחברות ─────────────────────────────────────────────────
# בזיכרון, פר-IP. **חשוב:** ה-IP נקרא מ-`request.remote_addr` אחרי
# ProxyFix — לעולם לא מ-X-Forwarded-For ישירות (דפוס קריטי #2).
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_TRACKED_IPS = 1000
_login_attempts: dict[str, list[float]] = {}
# הפאנל רץ threaded=True: בלי מנעול, שני ניסיונות מקבילים דורסים זה את
# רשימת השני (`_login_attempts[ip] = fresh`), המונה יורד מתחת לסף,
# והגנת ה-brute force נחלשת בדיוק תחת התנאים שהיא נועדה להם.
_login_lock = threading.Lock()


# ─── פילטרים של Jinja ────────────────────────────────────────────────────
# ערכי datetime מה-DB (‏UTC, ‏`YYYY-MM-DD HH:MM:SS`) חייבים לעבור דרך
# פילטר לפני הצגה. אסור `{{ value }}` חשוף (CLAUDE.md → Templates).


def _format_il_datetime(value: str) -> str:
    """‏UTC מה-DB ⇒ ‏DD/MM/YYYY HH:MM בשעון ישראל."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ISRAEL_TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return value


def _format_il_datetime_local(value: str) -> str:
    """ערך שכבר בשעון ישראל — הצגה בלבד, בלי המרת אזור זמן."""
    if not value:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).strftime("%d/%m/%Y %H:%M")
        except (ValueError, TypeError):
            continue
    return value


def _format_il_date(value: str) -> str:
    """‏YYYY-MM-DD ⇒ ‏DD/MM/YYYY."""
    if not value:
        return ""
    parts = str(value).split(" ")[0].split("-")
    if len(parts) == 3:
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return value


def _format_relative_time(value: str) -> str:
    """זמן יחסי בעברית עד שבוע, ומעבר לזה — פורמט מלא."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ISRAEL_TZ)
    except (ValueError, TypeError):
        return value

    diff = datetime.now(ISRAEL_TZ) - dt
    total_seconds = int(diff.total_seconds())
    if total_seconds < 0:
        return _format_il_datetime(value)
    if total_seconds < 60:
        return "עכשיו"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"לפני {minutes} דקות" if minutes > 1 else "לפני דקה"
    hours = total_seconds // 3600
    if hours < 24:
        return f"לפני {hours} שעות" if hours > 1 else "לפני שעה"
    days = diff.days
    if days == 1:
        return f"אתמול בשעה {dt.strftime('%H:%M')}"
    if days < 7:
        return f"לפני {days} ימים"
    return _format_il_datetime(value)


def _reply_window_state(last_inbound_at: str) -> str:
    """מצב חלון 24 השעות לתצוגה: ‏open / ‏closing / ‏closed / ‏unknown.

    ‏closing = נותרו פחות מ-4 שעות. המצב מוצג בפאנל כדי שבעל העסק יידע
    אילו שיחות עוד ניתנות למענה (‏PLAN §1.4).
    """
    if not last_inbound_at:
        return "unknown"
    try:
        dt = datetime.strptime(last_inbound_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "unknown"
    elapsed = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
    if elapsed >= timedelta(hours=24):
        return "closed"
    if elapsed >= timedelta(hours=20):
        return "closing"
    return "open"


# ─── עזרי אבטחה ──────────────────────────────────────────────────────────


def _validate_admin_security_config() -> None:
    """‏fail-fast בעלייה: בלי מפתח session אין CSRF ואין הגנה על ה-session."""
    if not _cfg.ADMIN_SECRET_KEY:
        raise RuntimeError(
            "ADMIN_SECRET_KEY חייב להיות מוגדר (חתימת session + הגנת CSRF)."
        )
    if not _cfg.ADMIN_USERNAME:
        raise RuntimeError("ADMIN_USERNAME חייב להיות מוגדר.")
    if not (_cfg.ADMIN_PASSWORD_HASH or _cfg.ADMIN_PASSWORD):
        raise RuntimeError(
            "יש להגדיר ADMIN_PASSWORD_HASH (מומלץ) או ADMIN_PASSWORD."
        )


def _verify_admin_credentials(username: str, password: str) -> bool:
    """מסלול ה-env (בעל העסק של ה-tenant של ברירת המחדל).

    בדיקת הסיסמה רצה תמיד — גם כששם המשתמש שגוי — כדי לא לייצר timing
    oracle שמבחין בין "שם משתמש לא נכון" ל"סיסמה לא נכונה".
    """
    if not username or not password:
        return False
    # ‏compare_digest על `str` תומך ב-ASCII בלבד ומרים TypeError על כל תו
    # אחר. שם משתמש או סיסמה בעברית היו מפילים את ה-route ל-500 במקום
    # "פרטים שגויים" — כלומר גם באג פונקציונלי וגם oracle על הקלט.
    # השוואה על bytes שומרת על זמן קבוע ומקבלת כל תו.
    username_ok = hmac.compare_digest(
        str(username).encode("utf-8"), str(_cfg.ADMIN_USERNAME).encode("utf-8")
    )
    if _cfg.ADMIN_PASSWORD_HASH:
        try:
            password_ok = check_password_hash(_cfg.ADMIN_PASSWORD_HASH, str(password))
        except Exception:
            logger.error("בדיקת ADMIN_PASSWORD_HASH נכשלה", exc_info=True)
            password_ok = False
    else:
        password_ok = hmac.compare_digest(
            str(password).encode("utf-8"), str(_cfg.ADMIN_PASSWORD).encode("utf-8")
        )
    return username_ok and password_ok


def _check_login_rate_limit(ip: str) -> bool:
    """‏True אם ה-IP חרג ממגבלת ניסיונות ההתחברות."""
    import time

    cutoff = time.time() - _LOGIN_WINDOW_SECONDS
    with _login_lock:
        attempts = _login_attempts.get(ip)
        if not attempts:
            return False
        fresh = [ts for ts in attempts if ts > cutoff]
        if not fresh:
            _login_attempts.pop(ip, None)
            return False
        _login_attempts[ip] = fresh
        return len(fresh) >= _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    """רישום ניסיון התחברות כושל, עם פינוי לפי גיל.

    הפינוי הוא לפי הניסיון **הישן ביותר** ולא לפי סדר ההכנסה: מילון
    שומר סדר הכנסה, אבל ה-IP שהוכנס ראשון אינו בהכרח זה שלא נראה הכי
    הרבה זמן.
    """
    import time

    now = time.time()
    with _login_lock:
        if ip not in _login_attempts and len(_login_attempts) >= _LOGIN_MAX_TRACKED_IPS:
            stale_ip = min(_login_attempts, key=lambda k: _login_attempts[k][-1])
            _login_attempts.pop(stale_ip, None)
        _login_attempts.setdefault(ip, []).append(now)


def _audit_log(action: str, details: str = "") -> None:
    """רישום פעולת אדמין ללוג — ‏IP, נתיב ופרטים. בלי PII (דפוס #7)."""
    logger.info(
        "AUDIT | ip=%s | path=%s | action=%s | %s",
        request.remote_addr or "unknown", request.path, action, details,
    )


def _safe_redirect_back(default_url: str) -> str:
    """יעד redirect בטוח (same-origin) מתוך Referer, או ברירת מחדל."""
    ref = request.referrer
    if not ref:
        return default_url
    try:
        parsed = urlparse(ref)
        if parsed.netloc and parsed.netloc != urlparse(request.host_url).netloc:
            return default_url
        return parsed.path or default_url
    except Exception:
        logger.error("כשל בפענוח Referer", exc_info=True)
        return default_url


# ─── האפליקציה ───────────────────────────────────────────────────────────


def create_admin_app() -> Flask:
    """בניית אפליקציית ה-Flask של הפאנל."""
    _validate_admin_security_config()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    # מאחורי ה-proxy של הפלטפורמה. רמת trust אחת בלבד — בלי זה
    # request.remote_addr הוא ה-proxy, ו-rate limit ההתחברות היה חסר משמעות
    # (וקריאה ישירה של X-Forwarded-For היא דפוס קריטי #2).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.secret_key = _cfg.ADMIN_SECRET_KEY
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    # ‏Secure כברירת מחדל: ה-ProxyFix עם x_proto מעיד על פריסה מאחורי TLS,
    # ובלי הדגל עוגיית הסשן (ואיתה טוקן ה-CSRF) נשלחת גם על HTTP.
    # פיתוח מקומי על http:// מכבה עם ADMIN_COOKIE_SECURE=false.
    app.config.setdefault("SESSION_COOKIE_SECURE", _cfg.ADMIN_COOKIE_SECURE)

    csrf = CSRFProtect()
    csrf.init_app(app)

    # ── קשירת tenant context לכל בקשה ──
    # זה מה שהופך את כל ה-routes לפר-tenant בלי לגעת באף אחד מהם.
    @app.before_request
    def _bind_tenant_context():
        tenant = DEFAULT_TENANT
        chosen = session.get("tenant_id") or session.get("acting_tenant")
        if chosen:
            from control_plane import get_tenant_status_cached
            from tenancy import InvalidTenantSlug, validate_tenant_id

            try:
                validate_tenant_id(chosen)
                status = get_tenant_status_cached(chosen)
            except InvalidTenantSlug:
                status = None
            except Exception:
                logger.error("בדיקת סטטוס tenant נכשלה", exc_info=True)
                status = None
            if status == "active":
                tenant = chosen
            else:
                # ה-tenant הושעה/נמחק בזמן שה-session חי — ניתוק מסודר
                logger.warning("session קשור ל-tenant לא פעיל — מתנתקים")
                session.clear()
                flash("החשבון אינו פעיל כרגע. פנו לתמיכה.", "warning")
                return redirect(url_for("login"))
        g._tenant_token = set_current_tenant(tenant)

    @app.teardown_request
    def _release_tenant_context(exc):
        token = g.pop("_tenant_token", None)
        if token is not None:
            try:
                reset_current_tenant(token)
            except Exception:
                logger.error("שחזור tenant context נכשל", exc_info=True)

    app.jinja_env.filters["il_datetime"] = _format_il_datetime
    app.jinja_env.filters["il_datetime_local"] = _format_il_datetime_local
    app.jinja_env.filters["il_date"] = _format_il_date
    app.jinja_env.filters["relative_time"] = _format_relative_time
    app.jinja_env.filters["reply_window"] = _reply_window_state

    @app.context_processor
    def _inject_globals():
        try:
            business_name = get_business_config().name
        except Exception:
            logger.error("כשל בקריאת הזהות העסקית לתבנית", exc_info=True)
            business_name = ""
        return {
            "business_name": business_name,
            "is_platform_admin": session.get("admin_role") == "platform_admin",
        }

    @app.errorhandler(CSRFError)
    def _handle_csrf_error(e):
        logger.warning(
            "CSRF error | ip=%s | path=%s | method=%s",
            request.remote_addr, request.path, request.method,
        )
        if request.headers.get("HX-Request"):
            # 403 קליל כדי ש-HTMX לא יחליף תוכן בעמוד redirect שלם
            resp = app.make_response(("", 403))
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = "csrfExpired"
            return resp
        flash("פג תוקף הטופס. נסו שוב.", "danger")
        default = url_for("dashboard") if session.get("logged_in") else url_for("login")
        return redirect(_safe_redirect_back(default))

    # ── ‏auth ──

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("logged_in"):
                if request.headers.get("HX-Request"):
                    resp = app.make_response(("", 401))
                    resp.headers["HX-Redirect"] = url_for("login")
                    return resp
                return redirect(url_for("login"))
            return f(*args, **kwargs)

        return decorated

    def platform_admin_required(f):
        """גישה ל-platform admins בלבד. לאחרים — 404 (לא חושפים קיום)."""

        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get("admin_role") != "platform_admin":
                return ("", 404)
            return f(*args, **kwargs)

        return decorated

    @app.route("/health")
    def health():
        """בדיקת חיות — לא דורשת אימות, לא חושפת מידע."""
        return {"status": "ok"}, 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            client_ip = request.remote_addr or "unknown"
            if _check_login_rate_limit(client_ip):
                logger.warning("חריגה ממגבלת ניסיונות התחברות")
                flash("יותר מדי ניסיונות התחברות. נסו שוב בעוד מספר דקות.", "danger")
                return render_template("login.html")

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            remember = bool(request.form.get("remember_me"))

            # מסלול ה-env — בעל העסק של ה-tenant של ברירת המחדל
            if _verify_admin_credentials(username, password):
                session.clear()  # מניעת session fixation
                if remember:
                    session.permanent = True
                session["logged_in"] = True
                flash("ברוכים השבים!", "success")
                _audit_log("login_success", "env_user")
                return redirect(url_for("dashboard"))

            # מסלול הפלטפורמה — admin_users מה-control plane
            try:
                from control_plane import verify_admin_login

                platform_user = verify_admin_login(username, password)
            except Exception:
                # דפוס קריטי #10: כשל תשתיתי אינו "פרטים שגויים"
                logger.error("מסלול ההתחברות של הפלטפורמה נכשל", exc_info=True)
                flash("השירות אינו זמין כרגע — נסו שוב בעוד רגע.", "danger")
                return render_template("login.html")

            if platform_user:
                session.clear()
                if remember:
                    session.permanent = True
                session["logged_in"] = True
                session["admin_email"] = platform_user["email"]
                session["admin_role"] = platform_user["role"]
                if platform_user["role"] == "owner":
                    session["tenant_id"] = platform_user["tenant_id"]
                flash("ברוכים השבים!", "success")
                # בלי email בלוג (דפוס #7) — role + tenant מספיקים לאודיט
                _audit_log(
                    "login_success",
                    f"role={platform_user['role']} "
                    f"tenant={platform_user.get('tenant_id') or '-'}",
                )
                if platform_user["role"] == "platform_admin":
                    return redirect(url_for("platform_home"))
                return redirect(url_for("dashboard"))

            _record_login_attempt(client_ip)
            logger.warning("ניסיון התחברות כושל")
            flash("פרטי התחברות שגויים.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("התנתקת בהצלחה.", "info")
        return redirect(url_for("login"))

    # ── לוח בקרה ──

    @app.route("/")
    @login_required
    def dashboard():
        import kb_service

        counts = db.get_dashboard_counts()
        kb_ctx = kb_service.get_kb_context()
        return render_template("dashboard.html", counts=counts, kb=kb_ctx)

    # ── בסיס ידע ──

    @app.route("/kb")
    @login_required
    def kb_list():
        category_filter = request.args.get("category") or None
        entries = db.get_all_kb_entries(category=category_filter, active_only=False)
        return render_template(
            "kb_list.html",
            entries=entries,
            categories=db.get_kb_categories(),
            current_category=category_filter,
        )

    @app.route("/kb/add", methods=["GET", "POST"])
    @login_required
    def kb_add():
        if request.method == "POST":
            category = request.form.get("category", "").strip()
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            gap_id = request.form.get("gap_id", "").strip()

            if not all([category, title, content]):
                flash("כל השדות הם חובה.", "danger")
            else:
                db.add_kb_entry(category, title, content)
                _audit_log("kb_add", f"category={category}")
                # סגירת פער הידע שממנו הגיעה הרשומה
                if gap_id:
                    try:
                        db.update_unanswered_question_status(int(gap_id), "resolved")
                    except (ValueError, TypeError):
                        logger.error("gap_id לא תקין בטופס הוספת רשומה")
                    except Exception:
                        logger.error("סגירת פער הידע נכשלה", exc_info=True)
                flash(f"הרשומה '{title}' נוספה. הבוט ישתמש בה כבר בהודעה הבאה.", "success")
                return redirect(url_for("kb_list"))

        return render_template(
            "kb_form.html",
            entry=None,
            categories=db.get_kb_categories(),
            prefill_question=request.args.get("question", ""),
            gap_id=request.args.get("gap_id", ""),
        )

    @app.route("/kb/edit/<int:entry_id>", methods=["GET", "POST"])
    @login_required
    def kb_edit(entry_id):
        entry = db.get_kb_entry(entry_id)
        if not entry:
            flash("הרשומה לא נמצאה.", "danger")
            return redirect(url_for("kb_list"))

        if request.method == "POST":
            category = request.form.get("category", "").strip()
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            if not all([category, title, content]):
                flash("כל השדות הם חובה.", "danger")
            else:
                db.update_kb_entry(entry_id, category, title, content)
                _audit_log("kb_edit", f"entry_id={entry_id}")
                flash(f"הרשומה '{title}' עודכנה.", "success")
                return redirect(url_for("kb_list"))

        return render_template(
            "kb_form.html", entry=entry, categories=db.get_kb_categories(),
        )

    @app.route("/kb/delete/<int:entry_id>", methods=["POST"])
    @login_required
    def kb_delete(entry_id):
        db.delete_kb_entry(entry_id)
        _audit_log("kb_delete", f"entry_id={entry_id}")
        if request.headers.get("HX-Request"):
            # כשהטבלה התרוקנה — מחליפים את כל הקונטיינר במצב הריק, כדי
            # שלא תישאר טבלה עם כותרות בלי שורות (HTMX — DOM consistency)
            if db.count_kb_entries(active_only=False) == 0:
                resp = app.make_response(render_template("partials/kb_empty.html"))
                resp.headers["HX-Retarget"] = "#kb-table-wrapper"
                resp.headers["HX-Reswap"] = "outerHTML"
                return resp
            return app.make_response("")
        flash("הרשומה נמחקה.", "success")
        return redirect(url_for("kb_list"))

    @app.route("/kb/search")
    @login_required
    def kb_search():
        """חיפוש טקסטואלי בבסיס הידע (LIKE, עם escaping ב-database)."""
        query = request.args.get("q", "").strip()
        if not query:
            if request.headers.get("HX-Request"):
                return ""
            return redirect(url_for("kb_list"))

        try:
            entries = db.search_kb_entries(query, limit=20)
        except Exception:
            logger.error("חיפוש בבסיס הידע נכשל", exc_info=True)
            entries = []

        if request.headers.get("HX-Request"):
            return render_template(
                "partials/kb_search_results.html", entries=entries, query=query,
            )
        return redirect(url_for("kb_list"))

    # ── פערי ידע ──

    @app.route("/knowledge-gaps")
    @login_required
    def knowledge_gaps():
        status_filter = request.args.get("status") or None
        return render_template(
            "knowledge_gaps.html",
            questions=db.get_unanswered_questions(status=status_filter),
            current_status=status_filter,
            open_count=db.count_unanswered_questions(status="open"),
        )

    @app.route("/knowledge-gaps/<int:question_id>/resolve", methods=["POST"])
    @login_required
    def resolve_question(question_id):
        status = request.form.get("status", "resolved")
        if status not in VALID_UNANSWERED_STATUSES:
            if request.headers.get("HX-Request"):
                resp = app.make_response(("", 422))
                resp.headers["HX-Trigger"] = json.dumps(
                    {"showToast": {"message": "סטטוס לא חוקי.", "type": "danger"}}
                )
                return resp
            flash("סטטוס לא חוקי.", "danger")
            return redirect(url_for("knowledge_gaps"))

        db.update_unanswered_question_status(question_id, status)
        _audit_log("gap_resolve", f"question_id={question_id} status={status}")

        if request.headers.get("HX-Request"):
            q = db.get_unanswered_question(question_id)
            if q:
                return render_template("partials/knowledge_gap_row.html", q=q)
            return ""
        flash("הפער עודכן.", "success")
        return redirect(url_for("knowledge_gaps"))

    # ── שיחות ──

    @app.route("/conversations")
    @login_required
    def conversations():
        """רשימת השיחות + מצב חלון 24 השעות של כל אחת."""
        users = db.get_unique_users()
        selected = (request.args.get("user_id") or "").strip()
        # ולידציה לפני שאילתה — מזהה טלגרם הוא מספרי (דפוס אוניברסלי #3)
        if selected and not _USER_ID_RE.match(selected):
            logger.warning("conversations: user_id לא תקין בפרמטר")
            selected = ""
        messages = db.get_conversation_history(selected, limit=200) if selected else []
        return render_template(
            "conversations.html",
            users=users,
            selected_user=selected,
            messages=messages,
        )

    # ── הבוט שלי ──

    @app.route("/my-bot")
    @login_required
    def my_bot():
        """סטטוס הבוט-הבן והחיבור של ה-tenant הנוכחי."""
        from tenancy import get_current_tenant

        tenant = get_current_tenant()
        bot_row = connection = None
        if tenant != DEFAULT_TENANT:
            try:
                import control_plane as cp

                bot_row = cp.get_managed_bot_for_tenant(tenant)
                connection = cp.get_business_connection_for_tenant(tenant)
            except Exception:
                logger.error("כשל בקריאת סטטוס הבוט", exc_info=True)
        rights = {}
        if connection and connection.get("rights_json"):
            try:
                rights = json.loads(connection["rights_json"])
            except (ValueError, TypeError):
                logger.error("rights_json לא תקין")
        return render_template(
            "my_bot.html",
            tenant=tenant,
            bot=bot_row,
            connection=connection,
            rights=rights,
        )

    # ── ניהול פלטפורמה ──

    @app.route("/platform")
    @login_required
    @platform_admin_required
    def platform_home():
        import control_plane as cp

        tenants = cp.list_tenants()
        bots = {b["tenant_id"]: b for b in cp.list_managed_bots()}
        connections = {
            t["tenant_id"]: cp.get_business_connection_for_tenant(t["tenant_id"])
            for t in tenants
        }
        return render_template(
            "platform.html", tenants=tenants, bots=bots, connections=connections,
        )

    @app.route("/platform/new", methods=["GET", "POST"])
    @login_required
    @platform_admin_required
    def platform_new_tenant():
        """אשף לקוח חדש — יצירת tenant, מפתח webhook וקוד צימוד."""
        import control_plane as cp

        if request.method == "POST":
            tenant_id = request.form.get("tenant_id", "").strip().lower()
            display_name = request.form.get("display_name", "").strip()
            if not tenant_id or not display_name:
                flash("צריך גם מזהה וגם שם עסק.", "danger")
                return render_template("platform_new_tenant.html")
            try:
                cp.create_tenant(tenant_id, display_name)
            except Exception as exc:
                logger.error("יצירת tenant נכשלה", exc_info=True)
                flash(f"היצירה נכשלה: {exc}", "danger")
                return render_template("platform_new_tenant.html")

            # מפתח ה-webhook נוצר יחד עם הלקוח — הוא הזהות של ה-route שלו
            cp.set_route("telegram_webhook_key", cp.generate_route_key(), tenant_id)
            _audit_log("tenant_create", f"tenant={tenant_id}")
            return redirect(url_for("platform_onboarding", tenant_id=tenant_id))

        return render_template("platform_new_tenant.html")

    @app.route("/platform/onboarding/<tenant_id>")
    @login_required
    @platform_admin_required
    def platform_onboarding(tenant_id: str):
        """מסך ההקמה: קוד צימוד + סטטוסים חיים של שלבי החיבור."""
        import control_plane as cp

        tenant = cp.get_tenant(tenant_id)
        if tenant is None:
            flash("הלקוח לא נמצא.", "danger")
            return redirect(url_for("platform_home"))
        return render_template(
            "platform_onboarding.html", tenant=tenant,
            pairing_link=session.pop(f"pairing_link_{tenant_id}", ""),
        )

    @app.route("/platform/onboarding/<tenant_id>/code", methods=["POST"])
    @login_required
    @platform_admin_required
    def platform_pairing_code(tenant_id: str):
        """יצירת קוד צימוד חדש (תפוגה שעה) והצגת הלינק ללקוח."""
        import control_plane as cp
        from bot.manager_bot import build_pairing_link

        manager = (_cfg.MANAGER_BOT_USERNAME or "").lstrip("@")
        if not manager:
            flash("MANAGER_BOT_USERNAME לא מוגדר — אי אפשר לבנות לינק צימוד.", "danger")
            return redirect(url_for("platform_onboarding", tenant_id=tenant_id))
        try:
            code = cp.create_pairing_code(tenant_id)
        except Exception as exc:
            logger.error("יצירת קוד צימוד נכשלה", exc_info=True)
            flash(f"יצירת הקוד נכשלה: {exc}", "danger")
            return redirect(url_for("platform_onboarding", tenant_id=tenant_id))

        _audit_log("pairing_code_create", f"tenant={tenant_id}")
        # הלינק נשמר ב-session ולא ב-URL: הוא credential חד-פעמי, ולא
        # אמור להישאר בהיסטוריית הדפדפן או ב-Referer.
        session[f"pairing_link_{tenant_id}"] = build_pairing_link(manager, code)
        return redirect(url_for("platform_onboarding", tenant_id=tenant_id))

    @app.route("/platform/onboarding/<tenant_id>/status")
    @login_required
    @platform_admin_required
    def platform_onboarding_status(tenant_id: str):
        """סטטוסי ההקמה — נטען ב-HTMX polling מהמסך."""
        import control_plane as cp

        bot_row = cp.get_managed_bot_for_tenant(tenant_id)
        connection = cp.get_business_connection_for_tenant(tenant_id)
        paired = any(
            r["used_by_user_id"] for r in _pairing_rows(tenant_id)
        )
        return render_template(
            "partials/onboarding_status.html",
            paired=paired, bot=bot_row, connection=connection,
        )

    def _pairing_rows(tenant_id: str) -> list[dict]:
        import control_plane as cp

        with cp.get_platform_connection() as conn:
            rows = conn.execute(
                "SELECT used_by_user_id FROM pairing_codes WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    @app.route("/platform/<tenant_id>/offboard", methods=["POST"])
    @login_required
    @platform_admin_required
    def platform_offboard(tenant_id: str):
        """ניתוק לקוח — ניטרול הבוט, מחיקת הסודות והשעיה."""
        import asyncio

        from services.offboarding import offboard_tenant

        loop = app.config.get("_bot_loop")
        if loop is None:
            flash("לולאת הבוטים אינה זמינה — נסו מה-CLI.", "danger")
            return redirect(url_for("platform_home"))
        try:
            future = asyncio.run_coroutine_threadsafe(offboard_tenant(tenant_id), loop)
            summary = future.result(timeout=30)
        except Exception:
            logger.error("ניתוק הלקוח נכשל", exc_info=True)
            flash("הניתוק נכשל. בדקו את הלוג.", "danger")
            return redirect(url_for("platform_home"))

        _audit_log("tenant_offboard", f"tenant={tenant_id}")
        if summary["errors"]:
            flash(
                f"הניתוק בוצע חלקית: {', '.join(summary['errors'])}. "
                "הרצה חוזרת תשלים את השאר.",
                "warning",
            )
        else:
            flash("הלקוח נותק והבוט נוטרל.", "success")
        return redirect(url_for("platform_home"))

    @app.route("/my-bot/resend-instructions", methods=["POST"])
    @login_required
    def resend_connection_instructions():
        """שליחה חוזרת של הוראות החיבור לבעל העסק."""
        import asyncio

        from tenancy import get_current_tenant

        tenant = get_current_tenant()
        loop = app.config.get("_bot_loop")
        if loop is None or tenant == DEFAULT_TENANT:
            flash("אי אפשר לשלוח כרגע.", "danger")
            return redirect(url_for("my_bot"))

        async def _send():
            import control_plane as cp
            from bot.manager_bot import _onboarding_instructions
            from bot.registry import ensure_manager_application

            bot_row = cp.get_managed_bot_for_tenant(tenant)
            if not bot_row:
                return False
            manager = await ensure_manager_application()
            if manager is None:
                return False
            await manager.bot.send_message(
                chat_id=bot_row["owner_user_id"],
                text=_onboarding_instructions(bot_row["bot_username"]),
            )
            return True

        try:
            sent = asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=20)
        except Exception:
            logger.error("שליחת הוראות החיבור נכשלה", exc_info=True)
            sent = False
        flash("ההוראות נשלחו." if sent else "השליחה נכשלה.", "success" if sent else "danger")
        return redirect(url_for("my_bot"))

    return app


def run_admin(flask_app: Flask | None = None) -> None:
    """הרצת הפאנל (‏threaded — כל בקשה ב-thread משלה)."""
    if flask_app is None:
        flask_app = create_admin_app()
    logger.info("פאנל הניהול עולה על http://%s:%s", _cfg.ADMIN_HOST, _cfg.ADMIN_PORT)
    flask_app.run(host=_cfg.ADMIN_HOST, port=_cfg.ADMIN_PORT, debug=False, threaded=True)
