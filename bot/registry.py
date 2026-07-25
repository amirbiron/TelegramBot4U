"""
‏Bot Registry — אפליקציית PTB לכל tenant, בתהליך אחד.

כל ה-Applications חיות על **אותו event loop** (זה שנוצר ב-`main.py`).
הבנייה והאתחול עצלים — בהודעה הראשונה של ה-tenant — כך ש-tenant שנרשם
בזמן ריצה עובד בלי restart.

הטוקן של כל tenant מגיע מהסודות המוצפנים ב-control plane. **לעולם לא
נופלים לטוקן של tenant אחר** — זו זהות של עסק אחר, ותשובה שתצא ממנה
תגיע ללקוח הלא נכון בשם הלא נכון.

הפונקציות האסינכרוניות כאן רצות **על ה-bot loop בלבד**, ונשלחות אליו
מה-route של Flask דרך `run_coroutine_threadsafe`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from tenancy import DEFAULT_TENANT, get_current_tenant, tenant_context

logger = logging.getLogger(__name__)

# tenant → Application מאותחל. מנוהל אך ורק מתוך ה-bot loop, ולכן די
# במנעולי asyncio (בלי מנעולי threads).
_apps: dict[str, object] = {}
_init_locks: dict[str, asyncio.Lock] = {}


def resolve_telegram_token(tenant_id: Optional[str] = None) -> str:
    """הטוקן של ה-tenant: ברירת המחדל → env; אחר → `tenant_secrets`.

    מחזיר '' כשאין טוקן — הקוראים מטפלים (אין אפליקציה, העדכון נזרק
    עם לוג). זו קונפיגורציה חסרה, לא באג.
    """
    tenant = tenant_id or get_current_tenant()
    if tenant == DEFAULT_TENANT:
        import config as _cfg

        return getattr(_cfg, "TELEGRAM_BOT_TOKEN", "") or ""
    try:
        from control_plane import get_tenant_secret

        return get_tenant_secret(tenant, "telegram_bot_token") or ""
    except Exception:
        logger.error("קריאת טוקן הבוט נכשלה (tenant=%s)", tenant, exc_info=True)
        return ""


def resolve_webhook_secret(tenant_id: str) -> str:
    """הסוד לאימות הכותרת של טלגרם ('' כשלא הוגדר)."""
    if tenant_id == DEFAULT_TENANT:
        import config as _cfg

        return getattr(_cfg, "MANAGER_WEBHOOK_SECRET", "") or ""
    try:
        from control_plane import get_tenant_secret

        return get_tenant_secret(tenant_id, "telegram_webhook_secret") or ""
    except Exception:
        logger.error("קריאת סוד ה-webhook נכשלה (tenant=%s)", tenant_id, exc_info=True)
        return ""


async def ensure_application(tenant_id: str):
    """ה-Application של ה-tenant — בנייה ואתחול עצלים.

    רץ על ה-bot loop. מחזיר None אם אין טוקן רשום.
    """
    app = _apps.get(tenant_id)
    if app is not None:
        return app

    lock = _init_locks.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _init_locks[tenant_id] = lock

    async with lock:
        # בדיקה חוזרת מתחת למנעול — שני עדכונים שהגיעו יחד
        app = _apps.get(tenant_id)
        if app is not None:
            return app

        token = resolve_telegram_token(tenant_id)
        if not token:
            logger.warning(
                "ל-tenant %s אין טוקן בוט רשום — העדכון נזרק", tenant_id,
            )
            return None

        from telegram.error import InvalidToken

        from bot.business_bot import create_business_application

        app = create_business_application(token)
        try:
            # ‏initialize מבצע getMe מול טלגרם — כאן מתגלה טוקן פסול
            await app.initialize()
        except InvalidToken:
            # תצורה שגויה, לא באג: הטוקן נשלל (offboarding, רוטציה) או
            # הוזן שגוי. שורת שגיאה ברורה במקום traceback בכל הודעה.
            logger.error(
                "הטוקן של tenant=%s נדחה ע\"י טלגרם — העדכונים שלו נזרקים "
                "עד שיוגדר טוקן תקין",
                tenant_id,
            )
            return None
        except Exception:
            logger.error(
                "אתחול אפליקציית הבוט של %s נכשל", tenant_id, exc_info=True,
            )
            return None
        _apps[tenant_id] = app
        logger.info("אפליקציית הבוט של %s אותחלה", tenant_id)
        return app


async def dispatch_update(tenant_id: str, update_data: dict) -> None:
    """עיבוד עדכון של tenant — תחת ה-context שלו, על האפליקציה שלו.

    ה-`tenant_context` כאן הוא **הנקודה היחידה** שקובעת tenant בנתיב
    הזה: ‏contextvars לא עוברים דרך `run_coroutine_threadsafe`, ולכן
    ה-context שנקבע ב-thread של Flask לא מגיע לכאן.
    """
    with tenant_context(tenant_id):
        app = await ensure_application(tenant_id)
        if app is None:
            return
        from telegram import Update

        update = Update.de_json(update_data, app.bot)
        await app.process_update(update)


async def shutdown_all_applications() -> None:
    """כיבוי נקי של כל האפליקציות (נקרא מ-atexit).

    כשל בכיבוי אחת לא עוצר את השאר.
    """
    for tenant_id, app in list(_apps.items()):
        try:
            await app.shutdown()
        except Exception:
            logger.error("כיבוי הבוט של %s נכשל", tenant_id, exc_info=True)
    _apps.clear()
    _init_locks.clear()


def reset_registry() -> None:
    """איפוס — לטסטים בלבד. לא מבצע shutdown (האפליקציות שם הן mocks)."""
    _apps.clear()
    _init_locks.clear()


def reset_tenant(tenant_id: str) -> None:
    """הסרת האפליקציה המטומנת — תיבנה מחדש בעדכון הבא.

    נקרא כשהטוקן מתחלף (רוטציה, ‏offboarding), כדי שהאפליקציה לא תמשיך
    לעבוד עם טוקן שכבר נשלל.
    """
    _apps.pop(tenant_id, None)
    _init_locks.pop(tenant_id, None)
