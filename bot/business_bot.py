"""
חיווט הבוט-הבן — בניית ה-Application ורישום ה-webhook.

‏ROADMAP T1.2. שתי נקודות שאסור לפספס בהן:

1. **‏`allowed_updates` חייב לכלול את ארבעת סוגי עדכוני ה-Business.**
   ברירת המחדל של טלגרם **אינה** כוללת אותם, ושכחה מייצרת דממה מוחלטת
   בלי שום שגיאה — הבוט פשוט לא מקבל כלום ואין מה לדבג.
2. **סוד ה-webhook נשמר לפני שהוא נרשם מול טלגרם** (דפוס קריטי #9 —
   credential נשמר לפני שנשלח). אם השמירה נכשלה, לא רושמים: עדיף בלי
   webhook מאשר webhook שאי אפשר לאמת את הבקשות אליו.
"""

from __future__ import annotations

import logging
import secrets

from telegram.ext import (
    Application,
    ApplicationBuilder,
    BusinessConnectionHandler,
    BusinessMessagesDeletedHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


def register_handlers(app: Application) -> None:
    """רישום ה-handlers של הערוץ (‏PLAN §4.2).

    הסדר משמעותי: ‏PTB מריץ את ה-handler הראשון שמתאים בכל group.
    """
    from bot.business_handlers import (
        on_business_connection,
        on_business_message,
        on_deleted_business_messages,
        on_edited_business_message,
    )

    app.add_handler(BusinessConnectionHandler(on_business_connection))
    # ‏business_message כולל מדיה: המסנן הוא סוג העדכון, לא סוג התוכן.
    # ‏`on_business_message` מזהה הודעה בלי טקסט ומטפל בה כמדיה (T1.6) —
    # אחרת הודעת מדיה הייתה נעלמת בשקט והלקוח לא היה מקבל כלום.
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))
    app.add_handler(
        MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, on_edited_business_message)
    )
    app.add_handler(BusinessMessagesDeletedHandler(on_deleted_business_messages))

    # פקודות הבעלים — **אחרי** ה-business handlers, וב-`message` רגיל
    # בלבד. הצ'אט הזה הוא בוט↔בעלים, לא צ'אט לקוח: אין בו
    # `business_connection_id`, ולכן הכלל "ללקוח אין פקודות" נשמר.
    # ‏`filters.ChatType.PRIVATE` — בקבוצה שמישהו הוסיף את הבוט אליה
    # אין לנו מה לעשות.
    from bot.owner_commands import on_owner_command

    app.add_handler(
        CommandHandler(
            ["pause", "resume", "status", "delete"], on_owner_command,
            filters=filters.ChatType.PRIVATE,
        )
    )


def create_business_application(token: str) -> Application:
    """בניית Application לבוט-בן, בלי JobQueue.

    אין JobQueue פר-בוט: העבודות המתוזמנות (‏digest, ‏retention) רצות
    ב-schedulers פלטפורמתיים שמאתרים על פני כל ה-tenants — אחרת כל
    בוט-בן היה מריץ עותק משלו.
    """
    app = ApplicationBuilder().token(token).job_queue(None).build()
    register_handlers(app)
    return app


def webhook_path(route_key: str) -> str:
    """הנתיב של הבוט-הבן. מפתח ה-route הוא סוד דה-פקטו (24 בייט אקראיים)."""
    return f"/telegram/webhook/t/{route_key}"


def webhook_url(base_url: str, route_key: str) -> str:
    """ה-URL המלא לרישום מול טלגרם."""
    return f"{base_url.rstrip('/')}{webhook_path(route_key)}"


async def setup_tenant_webhook(tenant_id: str, base_url: str | None = None) -> str:
    """רישום ה-webhook של הבוט-הבן מול טלגרם.

    מייצר route key וסוד אם עוד אין, שומר אותם, ורק אחר כך קורא
    ל-`setWebhook`. מחזיר את שם המשתמש של הבוט (מ-`getMe`), או '' אם
    לא הוחזר.

    ‏`Bot` שנוצר מחוץ ל-Application דורש `initialize()` לפני שימוש
    ו-`shutdown()` בסיום (‏PTB v20+). ה-cleanup עטוף בנפרד כדי שכשל בו
    לא ידרוס את תוצאת הפעולה העיקרית.
    """
    import config as _cfg
    import control_plane as cp
    from bot.registry import resolve_telegram_token

    base_url = (base_url or getattr(_cfg, "WEBHOOK_BASE_URL", "") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("WEBHOOK_BASE_URL לא מוגדר — אין לאן לרשום webhook")

    token = resolve_telegram_token(tenant_id)
    if not token:
        raise RuntimeError(f"ל-tenant '{tenant_id}' אין טוקן בוט רשום")

    route_key = cp.get_tenant_route_key(tenant_id, "telegram_webhook_key")
    if not route_key:
        route_key = cp.generate_route_key()
        cp.set_route("telegram_webhook_key", route_key, tenant_id)

    # הסוד נשמר **לפני** הרישום מול טלגרם (fail closed): אם השמירה
    # נכשלה, ה-route היה מקבל בקשות שאי אפשר לאמת.
    secret = cp.get_tenant_secret(tenant_id, "telegram_webhook_secret")
    if not secret:
        secret = secrets.token_urlsafe(32)
        cp.set_tenant_secret(tenant_id, "telegram_webhook_secret", secret)

    from telegram import Bot

    bot = Bot(token=token)
    await bot.initialize()
    try:
        await bot.set_webhook(
            url=webhook_url(base_url, route_key),
            secret_token=secret,
            allowed_updates=_cfg.BUSINESS_ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        logger.info("webhook נרשם ל-tenant %s", tenant_id)
        try:
            me = await bot.get_me()
            username = (me.username or "") if me else ""
        except Exception:
            logger.error("getMe נכשל אחרי set_webhook", exc_info=True)
            username = ""
        if username:
            cp.set_tenant_secret(tenant_id, "telegram_bot_username", username)
        return username
    finally:
        try:
            await bot.shutdown()
        except Exception:
            logger.error("bot.shutdown נכשל אחרי set_webhook", exc_info=True)


async def remove_tenant_webhook(tenant_id: str) -> None:
    """ביטול ה-webhook — לפני מחיקת הטוקן ב-offboarding.

    אחרי שהטוקן נמחק אין דרך לבטל (הטוקן הוא ההרשאה), ולכן הסדר קריטי.
    """
    from bot.registry import resolve_telegram_token

    token = resolve_telegram_token(tenant_id)
    if not token:
        return
    from telegram import Bot

    bot = Bot(token=token)
    await bot.initialize()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    finally:
        try:
            await bot.shutdown()
        except Exception:
            logger.error("bot.shutdown נכשל אחרי delete_webhook", exc_info=True)
