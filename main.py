"""
נקודת הכניסה — מרימה את הפאנל, את לולאת ה-asyncio ואת הבוטים.

טופולוגיה (‏PLAN §4.7): **תהליך יחיד**. ‏Flask ב-main thread, לולאת
asyncio ב-thread נפרד, וגשר `run_coroutine_threadsafe` ביניהם. ה-webhooks
של טלגרם נכנסים כ-routes של Flask ומועברים ללולאה.

שימוש:
    python main.py            # הכל: פאנל + בוטים
    python main.py --admin    # פאנל בלבד
    python main.py --seed     # tenant דמו + בסיס ידע לדוגמה
"""

import argparse
import asyncio
import atexit
import logging
import os
import sys
import threading

from tenancy import DEFAULT_TENANT, tenant_context

# ─── ניטור שגיאות (אופציונלי) ────────────────────────────────────────────
# ‏sentry-sdk הוא תלות רשומה, אבל היעדרה לא אמור להפיל את ה-boot
# (דפוס אוניברסלי #3 — אתחול SDK מוריד פיצ'ר, לא מקריס).
_sentry_dsn = os.getenv("SENTRY_DSN", "")
if _sentry_dsn:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=_sentry_dsn,
            traces_sample_rate=0.2,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        )
    except Exception:
        logging.getLogger(__name__).error("אתחול Sentry נכשל — ממשיכים בלעדיו", exc_info=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def bootstrap() -> None:
    """אתחול שרץ בכל עליית תהליך, לפני שמרימים שרתים.

    1. סכימת ה-control plane.
    2. סכימת ה-DB של ה-tenant של ברירת המחדל.
    3. **מיגרציה לכל ה-tenants הפעילים** — בלי זה, עמודה שנוספה
       ב-migration אחרי שה-tenant נוצר חסרה מה-DB שלו, וכל כתיבה שמפנה
       אליה זורקת "no such column" (‏CLAUDE.md → DB).
    """
    import control_plane as cp
    import database as db

    cp.init_platform_db()
    with tenant_context(DEFAULT_TENANT):
        logger.info("מאתחל את ה-DB של ברירת המחדל...")
        db.init_db()

    try:
        result = cp.migrate_all_tenants()
        logger.info("עדכון סכימה ל-tenants: %s", result)
    except Exception:
        # כשל במיגרציה של לקוח אחד לא מפיל את העלייה
        logger.error("עדכון הסכימה ל-tenants נכשל (ממשיכים)", exc_info=True)

    try:
        removed = cp.purge_expired_pairing_codes()
        if removed:
            logger.info("נוקו %d קודי צימוד שפג תוקפם", removed)
    except Exception:
        logger.error("ניקוי קודי הצימוד נכשל", exc_info=True)


def cleanup_takeovers() -> None:
    """סגירת השתקות שנשארו מהריצה הקודמת, בכל tenant פעיל.

    לולאת I/O על רשימת לקוחות — ‏try/except פר-פריט (‏CLAUDE.md): כשל
    ב-DB של לקוח אחד לא מונע את הניקוי אצל האחרים.
    """
    import control_plane as cp
    from services import takeover_service

    for tenant_id in cp.list_schedulable_tenant_ids():
        try:
            with tenant_context(tenant_id):
                takeover_service.cleanup_on_boot()
        except Exception:
            logger.error(
                "ניקוי ההשתקות נכשל ל-tenant=%s (ממשיכים)", tenant_id, exc_info=True,
            )


def run_seed() -> None:
    """יצירת tenant דמו + בסיס ידע לדוגמה."""
    from seed_data import seed_demo_tenant

    tenant_id = seed_demo_tenant()
    logger.info(
        "‏seed הושלם. הלקוח '%s' מוכן — התחברו לפאנל ובדקו את 'בסיס ידע'.", tenant_id,
    )


def start_bot_loop(flask_app) -> asyncio.AbstractEventLoop:
    """הרמת לולאת asyncio ב-thread נפרד, ושיתופה עם Flask.

    הלולאה היא הבית של כל אפליקציות ה-PTB (הבוט המנהל והבוטים-הבנים).
    ה-routes של ה-webhook רצים ב-threads של Flask ומעבירים אליה עבודה
    דרך `run_coroutine_threadsafe`.
    """
    loop = asyncio.new_event_loop()
    flask_app.config["_bot_loop"] = loop
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True, name="bot-loop")
    thread.start()
    # ממתינים שהלולאה תרוץ בפועל — אחרת בקשה מוקדמת עלולה לפגוש לולאה
    # שעוד לא התחילה, ו-run_coroutine_threadsafe ייתקע
    ready.wait(timeout=10)
    logger.info("לולאת הבוטים עלתה ב-thread נפרד")

    def _shutdown() -> None:
        try:
            from bot.registry import shutdown_all_applications

            future = asyncio.run_coroutine_threadsafe(shutdown_all_applications(), loop)
            future.result(timeout=10)
        except Exception as e:
            logger.error("כיבוי הבוטים ביציאה נכשל: %s", e)
        finally:
            # ה-cleanup עטוף בנפרד כדי שכשל בו לא ידרוס את תוצאת הכיבוי
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                logger.error("עצירת לולאת הבוטים נכשלה", exc_info=True)

    atexit.register(_shutdown)
    return loop


def main() -> None:
    parser = argparse.ArgumentParser(description="בוט ה-Secretary")
    parser.add_argument("--admin", action="store_true", help="פאנל בלבד, בלי בוטים")
    parser.add_argument("--seed", action="store_true", help="יצירת tenant דמו")
    args = parser.parse_args()

    bootstrap()

    if args.seed:
        run_seed()
        return

    from admin.app import create_admin_app, run_admin
    from config import validate_config

    for err in validate_config(require_bot=not args.admin, require_admin=True):
        logger.warning("⚠ תצורה: %s", err)

    flask_app = create_admin_app()

    if not args.admin:
        # ניקוי השתקות שנשארו מהריצה הקודמת — אחרת לקוחות נשארים בלי
        # מענה אחרי קריסה, והם אפילו לא יודעים שיש בוט שאמור לענות.
        cleanup_takeovers()
        start_bot_loop(flask_app)
        from bot.webhook import register_webhook_routes

        register_webhook_routes(flask_app)

    run_admin(flask_app)


if __name__ == "__main__":
    main()
