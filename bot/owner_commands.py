"""
פקודות הבעלים בצ'אט הבוט-הבן (‏ROADMAP T3.3).

**איפה זה קורה:** לא בעדכוני ה-Business. הפקודות מגיעות כ-`message`
רגיל, בצ'אט הפרטי בין הבעלים לבוט שלו — אותו צ'אט שאליו נשלחות
ההתראות (‏`user_chat_id`). זו הסיבה ש-`allowed_updates` כולל `message`.

**הבחנה קריטית:** הצ'אט הזה **אינו** צ'אט של לקוח. אין כאן
`business_connection_id`, ולכן הכללים של הערוץ ("ללקוח לעולם אין
פקודות") לא נסתרים — הלקוח לא רואה מזה כלום.

**זיהוי:** ‏`from.id` חייב להיות ה-`owner_user_id` הרשום ל-tenant.
משתמש אחר — **שתיקה מוחלטת**, לא הודעת שגיאה: מי שמצא את הבוט לא צריך
לדעת שהוא בוט עסקי של מישהו, ומה הפקודות שלו.

**בלי עיצוב:** ההודעות נשלחות בלי `parse_mode` — זו המוסכמה בכל
הריפו, והיא מה שמבטיח שתו מיוחד או טוקן שדלף יוצג כטקסט גולמי
במקום לשבור את ההודעה או להיעלם. לכן אין כאן `**הדגשה**`: היא הייתה
מוצגת ללקוח כככוכביות. ההדגשה נעשית בניסוח ובאימוג'י.

**‏`/pause` ממוקד:** הבעלים עונה `/pause` **בתגובה** להתראה על לקוח
מסוים ⇒ מושתקת רק אותה שיחה. בלי reply ⇒ ה-autopilot הגלובלי נכבה.
המיפוי מהתראה ללקוח יושב ב-`owner_alert_targets` (‏`owner_channel`
כותב אותו בשליחה).
"""

from __future__ import annotations

import logging

import database as db
from services import takeover_service

logger = logging.getLogger(__name__)


def _resolve_owner_connection(user_id: int) -> dict | None:
    """החיבור של ה-tenant הנוכחי, אם המשתמש הזה הוא הבעלים שלו.

    ‏fail closed: אין חיבור, או שהמזהה אינו תואם — ‏None, והקורא שותק.
    """
    try:
        import control_plane as cp
        from tenancy import get_current_tenant

        conn = cp.get_business_connection_for_tenant(get_current_tenant())
    except Exception:
        logger.error("owner_commands: כשל בשליפת החיבור", exc_info=True)
        return None
    if not conn:
        return None
    if int(conn.get("owner_user_id") or 0) != int(user_id):
        return None
    return conn


def _reply_target(msg) -> dict | None:
    """הלקוח שההודעה שהבעלים הגיב לה עסקה בו, או None.

    ‏None גם כשאין reply וגם כשההודעה שהוגב עליה אינה התראה שלנו —
    שני המקרים מובילים לאותה התנהגות (פעולה גלובלית).
    """
    replied = getattr(msg, "reply_to_message", None)
    if replied is None:
        return None
    message_id = getattr(replied, "message_id", None)
    if not message_id:
        return None
    # ה-chat_id הוא חצי מהמפתח: ‏message_id של טלגרם ייחודי פר-צ'אט
    # בלבד, ובלעדיו תגובה בצ'אט אחד הייתה יכולה להתאים לרשומה של צ'אט
    # אחר — כלומר להשתיק את הלקוח הלא נכון.
    owner_chat_id = getattr(getattr(replied, "chat", None), "id", None)
    if owner_chat_id is None:
        return None
    try:
        return db.get_owner_alert_target(message_id, owner_chat_id=str(owner_chat_id))
    except Exception:
        logger.error("owner_commands: כשל בקריאת יעד ההתראה", exc_info=True)
        return None


async def on_owner_command(update, context) -> None:
    """נקודת הכניסה היחידה לפקודות הבעלים.

    ה-tenant כבר נקבע ב-`dispatch_update` לפי ה-route של ה-webhook.
    """
    msg = getattr(update, "message", None)
    if msg is None or not (msg.text or "").startswith("/"):
        return
    user = getattr(msg, "from_user", None)
    if user is None:
        return

    conn = _resolve_owner_connection(user.id)
    if conn is None:
        logger.info("owner_commands: פקודה ממי שאינו הבעלים — מתעלמים")
        return

    command = (msg.text or "").split()[0].lstrip("/").split("@")[0].lower()
    handler = {
        "pause": _cmd_pause,
        "resume": _cmd_resume,
        "status": _cmd_status,
    }.get(command)
    if handler is None:
        return

    try:
        reply = handler(msg, conn)
    except Exception:
        logger.error("owner_commands: הפקודה %s נכשלה", command, exc_info=True)
        reply = "משהו השתבש אצלי. נסה שוב עוד רגע."

    if reply:
        try:
            await msg.reply_text(reply)
        except Exception:
            logger.error("owner_commands: שליחת התשובה לבעלים נכשלה", exc_info=True)


def _cmd_pause(msg, conn: dict) -> str:
    """השתקת הבוט — בשיחה אחת (בתגובה להתראה) או בכולן."""
    target = _reply_target(msg)
    if target:
        takeover_service.on_owner_message(
            target["chat_id"], user_id=target["user_id"],
        )
        return (
            "שקט בשיחה הזו. אני לא אענה שם עד שתסיים — או אחרי "
            f"{takeover_service.timeout_minutes()} דקות בלי הודעה ממך.\n"
            "הלקוח לא ראה שום דבר."
        )

    db.set_autopilot_enabled(False)
    return (
        "⏸️ כיביתי את המענה האוטומטי — בכל השיחות. אני עדיין קורא ושומר "
        "הכול, אבל לא עונה לאף אחד.\n"
        "‏/resume כדי להחזיר אותי.\n\n"
        "רק רצית להשתיק שיחה אחת? ענה /pause בתגובה להתראה על אותו לקוח."
    )


def _cmd_resume(msg, conn: dict) -> str:
    """החזרת הבוט — לשיחה אחת (בתגובה להתראה) או בכולן."""
    target = _reply_target(msg)
    if target:
        takeover_service.resume(target["chat_id"])
        return "חזרתי לענות בשיחה הזו."

    was_off = not db.is_autopilot_enabled()
    db.set_autopilot_enabled(True)
    if not was_off:
        return "המענה האוטומטי כבר היה דלוק — לא שיניתי כלום."
    return "חזרתי לענות. שיחות שהשתקת בנפרד נשארו מושתקות עד ה-timeout שלהן."


def _cmd_status(msg, conn: dict) -> str:
    """תמונת מצב: חיבור, הרשאות, ומה קורה בשיחות."""
    lines = ["📊 מצב:"]

    if conn.get("is_enabled"):
        lines.append("• מחובר לחשבון שלך ✅")
    else:
        lines.append("• לא מחובר ❌ — החיבור בוטל בהגדרות טלגרם")

    if conn.get("can_reply"):
        lines.append("• מותר לי לענות בשמך ✅")
    else:
        lines.append(
            "• אין לי הרשאה לענות ❌ — הגדרות טלגרם ← Chatbots ← "
            "לבחור אותי ← לאשר מענה להודעות"
        )

    try:
        autopilot = db.is_autopilot_enabled()
        counts = db.get_activity_counts(hours=24)
    except Exception:
        logger.error("owner_commands: כשל בקריאת המונים", exc_info=True)
        return "\n".join(lines) + "\n\nלא הצלחתי לקרוא את המונים כרגע."

    lines.append(
        "• מענה אוטומטי: " + ("דלוק ✅" if autopilot else "כבוי ⏸️ (‏/resume)")
    )
    lines.append("")
    lines.append("ב-24 השעות האחרונות:")
    lines.append(f"• עניתי על {counts['answered']} הודעות")
    lines.append(f"• {counts['customers']} לקוחות כתבו")
    if counts["waiting"]:
        lines.append(f"• ⏳ {counts['waiting']} שיחות ממתינות לך")
    if counts["silenced"] and counts["silenced"] != counts["waiting"]:
        lines.append(f"• {counts['silenced']} שיחות מושתקות בסך הכול")
    if counts["gaps"]:
        lines.append(f"• {counts['gaps']} שאלות שלא ידעתי לענות עליהן")
    return "\n".join(lines)
