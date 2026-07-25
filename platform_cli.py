"""
‏CLI לניהול הפלטפורמה — יצירת לקוחות, סודות ומשתמשי אדמין.

שימוש:
    python -m platform_cli list-tenants
    python -m platform_cli create-tenant --id salon-dana --name "סלון דנה"
    python -m platform_cli set-secret --tenant salon-dana --name telegram_bot_token --value 123:ABC
    python -m platform_cli create-admin --email a@b.com --tenant salon-dana
    python -m platform_cli set-status --tenant salon-dana --status suspended
    python -m platform_cli gen-key            # מפתח Fernet ל-SECRETS_ENCRYPTION_KEY
    python -m platform_cli gen-route-key      # מפתח webhook לבוט-בן

הסודות **לעולם לא מודפסים** בחזרה — הפקודות מדווחות על הצלחה בלבד
(דפוס קריטי #6). הסיסמאות נקראות מ-stdin בלי הד למסך.
"""

import argparse
import getpass
import logging
import sys

import control_plane as cp

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def cmd_list_tenants(args) -> int:
    tenants = cp.list_tenants()
    if not tenants:
        print("אין לקוחות רשומים.")
        return 0
    bots = {b["tenant_id"]: b for b in cp.list_managed_bots()}
    for t in tenants:
        bot = bots.get(t["tenant_id"])
        bot_desc = f"@{bot['bot_username']} ({bot['status']})" if bot else "—"
        print(f"{t['tenant_id']:<20} {t['status']:<10} {t['display_name']:<25} {bot_desc}")
    return 0


def cmd_create_tenant(args) -> int:
    cp.create_tenant(args.id, args.name)
    try:
        # מפתח ה-webhook נוצר מיד — הוא הזהות של ה-route של הבוט-הבן
        route_key = cp.generate_route_key()
        cp.set_route("telegram_webhook_key", route_key, args.id)
    except Exception:
        # בלי הגלגול אחורה, כשל כאן היה משאיר לקוח רשום בלי route:
        # הרצה חוזרת נופלת על slug תפוס, והמפעיל תקוע במצב חלקי.
        logger.error("רישום ה-route נכשל — מבטלים את יצירת הלקוח")
        cp.delete_tenant(args.id, backup=False)
        raise
    print(f"נוצר לקוח '{args.id}'.")
    print(f"נתיב ה-webhook שלו: /telegram/webhook/t/{route_key}")
    return 0


def cmd_set_secret(args) -> int:
    value = args.value
    if value is None:
        value = getpass.getpass(f"ערך עבור {args.name} (לא יוצג): ")
    cp.set_tenant_secret(args.tenant, args.name, value)
    print(f"הסוד '{args.name}' נשמר עבור '{args.tenant}'." if value
          else f"הסוד '{args.name}' נמחק מ-'{args.tenant}'.")
    return 0


def cmd_list_secrets(args) -> int:
    """שמות הסודות בלבד — הערכים לעולם לא מוחזרים."""
    names = cp.list_tenant_secret_names(args.tenant)
    print("\n".join(names) if names else "(אין סודות)")
    return 0


def cmd_create_admin(args) -> int:
    # ה-help מבטיח ש---tenant חובה ל-owner. האכיפה עצמה יושבת
    # ב-control_plane (הוא זורק UnknownTenantError), אבל בדיקה מוקדמת
    # נותנת הודעה מובנת במקום traceback על שדה שהמשתמש פשוט שכח.
    if not args.platform_admin and not args.tenant:
        print("--tenant חובה עבור משתמש owner.", file=sys.stderr)
        return 1
    password = getpass.getpass("סיסמה (לא תוצג): ")
    if password != getpass.getpass("אימות סיסמה: "):
        print("הסיסמאות אינן תואמות.", file=sys.stderr)
        return 1
    role = "platform_admin" if args.platform_admin else "owner"
    cp.create_admin_user(
        args.email, password, role=role,
        tenant_id=None if args.platform_admin else args.tenant,
    )
    print(f"נוצר משתמש אדמין ({role}).")
    return 0


def cmd_set_status(args) -> int:
    cp.set_tenant_status(args.tenant, args.status)
    print(f"'{args.tenant}' → {args.status}")
    return 0


def cmd_delete_tenant(args) -> int:
    if input(f"למחוק לצמיתות את '{args.tenant}' ואת כל נתוניו? הקלד את המזהה לאישור: ") != args.tenant:
        print("בוטל.")
        return 1
    summary = cp.delete_tenant(args.tenant)
    print(f"נמחק. גיבוי: {summary['backup_ok']} · קבצים הוסרו: {summary['files_removed']}")
    return 0


def cmd_offboard(args) -> int:
    """ניתוק לקוח — ניטרול הבוט, מחיקת הסודות והשעיה (idempotent)."""
    import asyncio

    from services.offboarding import offboard_tenant

    if input(f"לנתק את '{args.tenant}'? הבוט שלו ינוטרל. הקלד את המזהה לאישור: ") != args.tenant:
        print("בוטל.")
        return 1
    summary = asyncio.run(offboard_tenant(args.tenant))
    for key in ("webhook_removed", "token_revoked", "secret_deleted",
                "bot_marked_revoked", "tenant_suspended"):
        print(f"{'✓' if summary[key] else '✗'} {key}")
    if summary["errors"]:
        print("שגיאות:", ", ".join(summary["errors"]))
        print("הרצה חוזרת תשלים את מה שנותר.")
        return 1
    print("הבוט עצמו נשאר קיים בטלגרם (אין מתודת מחיקה ב-API) — "
          "הלקוח יכול למחוק אותו ב-BotFather.")
    return 0


def cmd_pair(args) -> int:
    """יצירת קוד צימוד ולינק ההצטרפות ללקוח."""
    import config as _cfg
    from bot.manager_bot import build_pairing_link

    manager = (_cfg.MANAGER_BOT_USERNAME or "").lstrip("@")
    if not manager:
        print("MANAGER_BOT_USERNAME לא מוגדר.", file=sys.stderr)
        return 1
    code = cp.create_pairing_code(args.tenant)
    print(build_pairing_link(manager, code))
    print("הקישור חד-פעמי ופג תוך שעה.")
    return 0


def cmd_gen_key(args) -> int:
    from utils.crypto import generate_new_key

    print(generate_new_key())
    return 0


def cmd_gen_route_key(args) -> int:
    print(cp.generate_route_key())
    return 0


def cmd_migrate(args) -> int:
    print(cp.migrate_all_tenants())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ניהול הפלטפורמה")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-tenants", help="רשימת הלקוחות").set_defaults(func=cmd_list_tenants)

    p = sub.add_parser("create-tenant", help="יצירת לקוח חדש")
    p.add_argument("--id", required=True, help="מזהה (slug): אותיות קטנות, ספרות ומקף")
    p.add_argument("--name", required=True, help="שם העסק לתצוגה")
    p.set_defaults(func=cmd_create_tenant)

    p = sub.add_parser("set-secret", help="שמירת סוד מוצפן")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True, choices=cp.KNOWN_SECRET_NAMES)
    p.add_argument("--value", help="אם לא סופק — ייקרא מ-stdin בלי הד")
    p.set_defaults(func=cmd_set_secret)

    p = sub.add_parser("list-secrets", help="שמות הסודות (בלי ערכים)")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_list_secrets)

    p = sub.add_parser("create-admin", help="יצירת משתמש לפאנל")
    p.add_argument("--email", required=True)
    p.add_argument("--tenant", help="חובה ל-owner")
    p.add_argument("--platform-admin", action="store_true", help="מנהל פלטפורמה (חוצה לקוחות)")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("set-status", help="שינוי סטטוס לקוח")
    p.add_argument("--tenant", required=True)
    p.add_argument("--status", required=True, choices=cp.TENANT_STATUSES)
    p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("delete-tenant", help="מחיקה מלאה של לקוח")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_delete_tenant)

    p = sub.add_parser("offboard", help="ניתוק לקוח וניטרול הבוט שלו")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_offboard)

    p = sub.add_parser("pair", help="קוד צימוד ולינק הצטרפות ללקוח")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_pair)

    sub.add_parser("gen-key", help="מפתח Fernet ל-SECRETS_ENCRYPTION_KEY").set_defaults(
        func=cmd_gen_key
    )
    sub.add_parser("gen-route-key", help="מפתח webhook אקראי").set_defaults(
        func=cmd_gen_route_key
    )
    sub.add_parser("migrate", help="עדכון סכימה לכל הלקוחות").set_defaults(func=cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cp.init_platform_db()
    try:
        return args.func(args)
    except Exception as exc:
        # שגיאה צפויה (slug תפוס, tenant לא רשום) — הודעה קריאה, לא traceback
        logger.error("%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
