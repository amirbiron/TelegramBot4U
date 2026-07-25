"""
‏Offboarding — ניתוק לקוח מהפלטפורמה.

שני מקרים מובילים לכאן: לקוח שעוזב, ובקשת מחיקה (זכות המחיקה של
ה-tenant). למודל B יש כאן חוב מיוחד: **אין למשתמש UI מובנה לביטול בוט
מנוהל** (מגבלה מתועדת, ‏PLAN §1.6), ולכן אם לא ננטרל את הבוט בעצמנו,
הוא נשאר חי עם טוקן שאנחנו מחזיקים — פער בזכות המחיקה, לא רק חוסר
ניקיון.

**סדר הפעולות קריטי, וכל צעד נסבל לכשל:**

1. ביטול ה-webhook — חייב לקרות **בעוד הטוקן תקף**. אחרי החלפתו אין
   דרך לבטל (הטוקן הוא ההרשאה).
2. `replaceManagedBotToken` — מנטרל את הטוקן שברשותנו. זו הפעולה
   האמיתית שמנתקת: גם אם משהו אחר נכשל, מהרגע הזה אי אפשר לפעול בשם
   הבוט עם מה שהיה שמור אצלנו.
3. מחיקת הסוד מה-control plane.
4. `managed_bots.status = 'revoked'`.
5. השעיית ה-tenant — חוסמת גישה לנתונים.

**אין מתודת מחיקת-בוט ב-Bot API** (נבדק ב-V1), ולכן הבוט עצמו נשאר
קיים בטלגרם. הוא מנוטרל אצלנו והבעלים יכול למחוק אותו ב-BotFather —
זה מצוין בהודעת הסיום אליו.

הפונקציה **אידמפוטנטית**: הרצה חוזרת אחרי כשל אמצעי משלימה את מה שנותר
במקום להיכשל.
"""

from __future__ import annotations

import logging

import control_plane as cp

logger = logging.getLogger(__name__)


async def offboard_tenant(tenant_id: str, *, suspend: bool = True) -> dict:
    """ניתוק מלא של לקוח. מחזיר סיכום של מה שהצליח ומה לא.

    כל צעד עטוף בנפרד ומדווח ב-summary — הקורא (CLI או פאנל) מציג
    לאדם מה נשאר לעשות ידנית, במקום להעמיד פנים שהכול הצליח.
    """
    summary: dict = {
        "tenant_id": tenant_id,
        "webhook_removed": False,
        "token_revoked": False,
        "secret_deleted": False,
        "bot_marked_revoked": False,
        "tenant_suspended": False,
        "errors": [],
    }

    if cp.get_tenant(tenant_id) is None:
        raise cp.UnknownTenantError(f"tenant לא רשום: {tenant_id}")

    bot_row = cp.get_managed_bot_for_tenant(tenant_id)

    # 1 — ביטול webhook, בעוד הטוקן תקף
    try:
        from bot.business_bot import remove_tenant_webhook

        await remove_tenant_webhook(tenant_id)
        summary["webhook_removed"] = True
    except Exception as exc:
        logger.error("offboarding: ביטול ה-webhook נכשל", exc_info=True)
        summary["errors"].append(f"webhook: {type(exc).__name__}")

    # 2 — נטרול הטוקן דרך הבוט המנהל (רק לבוט שנוצר דרכו)
    if bot_row:
        try:
            from bot.registry import ensure_manager_application

            manager = await ensure_manager_application()
            if manager is None:
                summary["errors"].append("manager: הבוט המנהל אינו זמין")
            else:
                await manager.bot.replace_managed_bot_token(user_id=bot_row["bot_id"])
                summary["token_revoked"] = True
        except Exception as exc:
            logger.error("offboarding: נטרול הטוקן נכשל", exc_info=True)
            summary["errors"].append(f"replace_token: {type(exc).__name__}")

    # 3 — מחיקת הסוד. נעשית **גם** אם הנטרול נכשל: הטוקן שברשותנו
    #     כבר לא אמור להיות בשימוש, ואחזקתו היא הסיכון.
    try:
        cp.set_tenant_secret(tenant_id, "telegram_bot_token", "")
        cp.set_tenant_secret(tenant_id, "telegram_webhook_secret", "")
        summary["secret_deleted"] = True
    except Exception as exc:
        logger.error("offboarding: מחיקת הסודות נכשלה", exc_info=True)
        summary["errors"].append(f"secrets: {type(exc).__name__}")

    # 4 — סימון הבוט כמנוטרל
    if bot_row:
        try:
            cp.set_managed_bot_status(bot_row["bot_id"], "revoked")
            summary["bot_marked_revoked"] = True
        except Exception as exc:
            logger.error("offboarding: סימון הבוט נכשל", exc_info=True)
            summary["errors"].append(f"bot_status: {type(exc).__name__}")

    # 5 — ניתוק החיבור והשעיית ה-tenant
    try:
        conn = cp.get_business_connection_for_tenant(tenant_id)
        if conn:
            cp.disable_business_connection(conn["connection_id"])
    except Exception as exc:
        logger.error("offboarding: ניתוק החיבור נכשל", exc_info=True)
        summary["errors"].append(f"connection: {type(exc).__name__}")

    # האפליקציה בזיכרון חייבת ליפול, אחרת היא ממשיכה לעבוד עם הטוקן
    # שכבר נשלל עד לעליית התהליך הבאה.
    try:
        from bot.registry import reset_tenant

        reset_tenant(tenant_id)
    except Exception:
        logger.error("offboarding: איפוס האפליקציה בזיכרון נכשל", exc_info=True)

    if suspend:
        try:
            cp.set_tenant_status(tenant_id, "suspended")
            summary["tenant_suspended"] = True
        except Exception as exc:
            logger.error("offboarding: השעיית ה-tenant נכשלה", exc_info=True)
            summary["errors"].append(f"suspend: {type(exc).__name__}")

    logger.info("offboarding הושלם: %s", summary)
    return summary
