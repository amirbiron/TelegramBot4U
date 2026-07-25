"""
‏webhook — נקודת הכניסה של עדכוני טלגרם ל-Flask.

הזרימה: טלגרם ⇒ ‏POST ל-`/telegram/webhook/t/<key>` ⇒ ‏resolve של
ה-tenant לפי ה-key ⇒ אימות הכותרת ⇒ העברה ללולאת הבוטים ⇒ ‏200 מיידי.

**למה 200 מיידי:** טלגרם חוזרת על עדכון שלא קיבל 2xx. אם נמתין לסיום
העיבוד (שכולל קריאת LLM), נחזיר תשובה אחרי שניות, וטלגרם עלולה לשלוח
את אותו עדכון שוב — והלקוח יקבל שתי תשובות. לכן מוסרים את העבודה
ללולאה ומחזירים מיד.

שני שלבי אימות, ושניהם נדרשים:
1. **מפתח ה-route** (‏24 בייט אקראיים) — הוא שקובע לאיזה tenant העדכון
   שייך. מי שלא מכיר אותו לא מגיע ל-tenant הנכון.
2. **‏`X-Telegram-Bot-Api-Secret-Token`** — מוכיח שהבקשה באמת מטלגרם.
   בלעדיו, מי שמצא את ה-URL בלוגים יכול להזריק עדכונים מזויפים ולגרום
   לבוט לענות ללקוחות בשם הבעלים.
"""

from __future__ import annotations

import asyncio
import hmac
import logging

from flask import request

logger = logging.getLogger(__name__)


def _verify_secret(tenant_id: str) -> bool:
    """השוואת הכותרת לסוד השמור, בזמן קבוע.

    ‏fail closed: ‏tenant בלי סוד רשום ⇒ דחייה. סוד ריק אינו "בלי
    אימות" — הוא תצורה חסרה, ובלעדיה ה-route פתוח להזרקה.
    """
    from bot.registry import resolve_webhook_secret

    expected = resolve_webhook_secret(tenant_id)
    if not expected:
        logger.error("ל-tenant %s אין סוד webhook רשום — הבקשה נדחית", tenant_id)
        return False
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return hmac.compare_digest(str(received), str(expected))


def register_webhook_routes(flask_app) -> None:
    """רישום ה-route של הבוטים-הבנים על אפליקציית ה-Flask.

    ה-route פטור מ-CSRF: הוא אינו טופס דפדפן אלא API של שרת-לשרת,
    והאימות שלו הוא הסוד בכותרת.
    """
    from flask_wtf.csrf import CSRFProtect  # noqa: F401 — נטען כבר ע"י האפליקציה

    @flask_app.route("/telegram/webhook/t/<route_key>", methods=["POST"])
    def telegram_business_webhook(route_key: str):
        import control_plane as cp

        tenant_id = cp.resolve_route("telegram_webhook_key", route_key)
        if not tenant_id:
            # לא חושפים אם המפתח קיים ולא מוכר או שגוי לגמרי
            logger.warning("webhook: מפתח route לא מוכר")
            return ("", 404)

        if not _verify_secret(tenant_id):
            logger.warning("webhook: סוד שגוי עבור tenant=%s", tenant_id)
            return ("", 403)

        update_data = request.get_json(force=True, silent=True)
        if not isinstance(update_data, dict):
            logger.warning("webhook: גוף בקשה שאינו JSON תקין")
            return ("", 400)

        loop = flask_app.config.get("_bot_loop")
        if loop is None:
            logger.error("webhook: לולאת הבוטים לא עלתה — העדכון נזרק")
            return ("", 503)

        from bot.registry import dispatch_update

        future = asyncio.run_coroutine_threadsafe(
            dispatch_update(tenant_id, update_data), loop,
        )
        # ‏Future מ-run_coroutine_threadsafe לעולם לא נזרק בלי callback:
        # בלעדיו חריגה בעיבוד נבלעת בשקט. בודקים cancelled() **לפני**
        # exception() — קריאה ל-exception() על future מבוטל זורקת.
        future.add_done_callback(_log_dispatch_result)
        return ("", 200)

    @flask_app.route("/telegram/webhook/manager", methods=["POST"])
    def telegram_manager_webhook():
        """הבוט המנהל — צימוד ויצירת בוטים-בנים.

        אין כאן route key: הבוט המנהל יחיד, וההפרדה היא בנתיב עצמו.
        האימות זהה — הסוד בכותרת, ‏fail closed בלעדיו.
        """
        import config as _cfg

        expected = getattr(_cfg, "MANAGER_WEBHOOK_SECRET", "") or ""
        if not expected:
            logger.error("MANAGER_WEBHOOK_SECRET לא מוגדר — הבקשה נדחית")
            return ("", 403)
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(str(received), str(expected)):
            logger.warning("webhook מנהל: סוד שגוי")
            return ("", 403)

        update_data = request.get_json(force=True, silent=True)
        if not isinstance(update_data, dict):
            return ("", 400)

        loop = flask_app.config.get("_bot_loop")
        if loop is None:
            logger.error("webhook מנהל: לולאת הבוטים לא עלתה — העדכון נזרק")
            return ("", 503)

        from bot.registry import dispatch_manager_update

        future = asyncio.run_coroutine_threadsafe(
            dispatch_manager_update(update_data), loop,
        )
        future.add_done_callback(_log_dispatch_result)
        return ("", 200)

    # ה-routes פטורים מ-CSRF (שרת-לשרת). ה-extension נשמר ב-app.extensions
    # ע"י create_admin_app.
    csrf = flask_app.extensions.get("csrf")
    if csrf is not None:
        csrf.exempt(telegram_business_webhook)
        csrf.exempt(telegram_manager_webhook)
    else:
        logger.error(
            "webhook: CSRFProtect לא נמצא על האפליקציה — ה-routes לא פוטרו "
            "מ-CSRF ועלולים להידחות"
        )


def _log_dispatch_result(future) -> None:
    """רישום כשל בעיבוד עדכון (רץ על ה-bot loop, אחרי שכבר החזרנו 200)."""
    if future.cancelled():
        logger.warning("webhook: עיבוד העדכון בוטל")
        return
    exc = future.exception()
    if exc is not None:
        logger.error("webhook: עיבוד העדכון נכשל: %s", exc, exc_info=exc)
