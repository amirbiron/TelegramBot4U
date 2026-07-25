"""
ערוץ הבעלים — התראות לבעל העסק בצ'אט שלו עם הבוט.

‏`user_chat_id` שמגיע ב-`BusinessConnection` הוא הצ'אט הישיר בוט↔בעלים,
וקיים עוד לפני שהבעלים שלח לבוט הודעה אחת (‏PLAN §4.5). במודל B זה הצ'אט
של הבעלים עם **הבוט שלו**, ממותג בשם העסק.

**ההבחנה הקריטית:** הודעה ללקוח נושאת `business_connection_id` (יוצאת
מהחשבון האישי של הבעלים); הודעה לבעלים בצ'אט הזה **לא** נושאת אותו —
היא יוצאת מהבוט, כי זה הצ'אט של הבוט עצמו. ערבוב בין השניים שולח ללקוח
הודעה שנראית כמו לוג פנימי.

הערוץ הזה משרת: התראות rate limit, חוסר הרשאה, חלון סגור, ‏handoff,
מדיה שהגיעה, ושינויי חיבור. כל אלה **במקום** הודעת מערכת ללקוח.

דה-דופ: התראה מאותו סוג לא נשלחת פעמיים בחלון זמן. בלי זה, לקוח אחד
שמציף הודעות היה מייצר עשרות התראות זהות והבעלים היה מכבה את הבוט.
"""

from __future__ import annotations

import logging
import threading
import time

import database as db

logger = logging.getLogger(__name__)

# חלון הדה-דופ פר-סוג התראה (שניות). ‏handoff לא נכנס לכאן — כל פנייה
# שדורשת את הבעלים היא אירוע נפרד ואסור לבלוע אותה.
DEDUP_WINDOW_SECONDS = {
    "rate_limited": 900,      # רבע שעה — הצפה מאותו לקוח
    "missing_permission": 3600,  # שעה — עד שהבעלים יתקן את ההרשאה
    "window_closed": 3600,    # שעה פר-לקוח
    "media": 300,             # חמש דקות — סדרת תמונות היא אירוע אחד
    "connection_changed": 60,
    # כשל שליחה לא מסווג. ממופתח לפי לקוח, ולכן חלון קצר יחסית: תקלה
    # מתמשכת תצוף שוב, אבל סדרת כשלים באותה שיחה היא אירוע אחד. בלי
    # השורה הזו ה-kind לא מוכר, `_should_send` מחזיר True תמיד,
    # והבעלים מקבל התראה על כל ניסיון.
    "send_failed": 900,
}

# ‏state ברמת מודול — ממופתח לפי tenant מהיום הראשון (CLAUDE.md).
# המפתח המלא: (tenant, connection_id, kind, subject)
_last_sent: dict[tuple, float] = {}
_lock = threading.Lock()
_MAX_TRACKED = 5000


def _should_send(kind: str, connection_id: str, subject: str = "") -> bool:
    """‏True אם ההתראה מחוץ לחלון הדה-דופ (ומסמן אותה כנשלחה)."""
    window = DEDUP_WINDOW_SECONDS.get(kind)
    if not window:
        return True
    from tenancy import get_current_tenant

    key = (get_current_tenant(), connection_id, kind, subject)
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < window:
            return False
        if len(_last_sent) >= _MAX_TRACKED:
            # פינוי גס: מסירים את הרשומות הישנות ביותר
            for stale_key in sorted(_last_sent, key=_last_sent.get)[: _MAX_TRACKED // 5]:
                _last_sent.pop(stale_key, None)
        _last_sent[key] = now
    return True


def _subject(user_id: str, display_name: str) -> str:
    """מפתח הדה-דופ של הלקוח — מזהה, לא שם.

    שם תצוגה אינו ייחודי: שתי "דנה" שונות היו חולקות מפתח, וההתראה על
    השנייה הייתה נבלעת בחלון של הראשונה — כלומר הבעלים לא היה יודע
    שלקוח שלם לא קיבל תשובה. השם נשאר בגוף ההודעה, כי שם הוא מה
    שמובן לבעלים; המפתח הוא ה-user_id.

    ‏fallback לשם כשאין מזהה: עדיף דה-דופ גס על היעדר דה-דופ.
    """
    return str(user_id).strip() or display_name


def reset_dedup() -> None:
    """איפוס מצב הדה-דופ — לטסטים בלבד."""
    with _lock:
        _last_sent.clear()


async def _send(
    bot, conn: dict, text: str, target: tuple[str, str] | None = None,
) -> bool:
    """שליחה בפועל לצ'אט הבעלים. מחזיר האם הצליח.

    **בלי** `business_connection_id` — זה הצ'אט של הבוט עם הבעלים.
    כשל אינו מפיל את הזרימה: התראה שלא נשלחה נרשמת ללוג, אבל ההודעה
    ללקוח (או השתיקה) כבר קרתה ואינה תלויה בה.

    ‏`target` = ‏(user_id, chat_id) של הלקוח שההתראה עוסקת בו. כשהוא
    נמסר, ה-message_id שחזר נשמר במיפוי — וזה מה שמאפשר לבעלים לענות
    `/pause` בתגובה להתראה ולהשתיק את אותה שיחה בלבד.
    """
    chat_id = conn.get("user_chat_id")
    if not chat_id:
        logger.warning("owner_channel: אין user_chat_id לחיבור — ההתראה לא נשלחה")
        return False
    try:
        sent = await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.error("owner_channel: שליחת ההתראה לבעלים נכשלה", exc_info=True)
        return False

    # רישום היעד נעשה **אחרי** שליחה מוצלחת ובנפרד: כשל בו לא הופך
    # התראה שכבר הגיעה לבעלים ל"נכשלה". התוצאה של כשל כאן היא ש-`/pause`
    # בתגובה לאותה הודעה ייפול ל-autopilot הגלובלי, לא שקט.
    if target is not None:
        message_id = getattr(sent, "message_id", None)
        if message_id:
            # ה-chat_id נלקח מההודעה שחזרה ולא מ-`conn`: זה הצ'אט
            # שטלגרם באמת שלחה אליו, והוא חצי מהמפתח.
            sent_chat = getattr(getattr(sent, "chat", None), "id", None) or chat_id
            try:
                db.record_owner_alert_target(
                    message_id, target[0], target[1], owner_chat_id=str(sent_chat),
                )
            except Exception:
                logger.error("owner_channel: רישום יעד ההתראה נכשל", exc_info=True)
    return True


async def notify(
    bot, conn: dict, text: str, kind: str = "", subject: str = "",
    target: tuple[str, str] | None = None,
) -> bool:
    """שליחת התראה לבעלים, בכפוף לדה-דופ לפי `kind`.

    ‏kind ריק ⇒ תמיד נשלח (אירוע ייחודי כמו handoff).
    """
    if kind and not _should_send(kind, conn.get("connection_id", ""), subject):
        logger.info("owner_channel: התראה מסוג %s דוכאה (דה-דופ)", kind)
        return False
    return await _send(bot, conn, text, target=target)


# ─── ההתראות הקונקרטיות ──────────────────────────────────────────────────
#
# הניסוח פונה לבעל העסק כאדם, לא כמפעיל מערכת: בלי "שגיאה", בלי קודים,
# ועם המשפט שאומר לו מה לעשות עכשיו.


async def notify_rate_limited(
    bot, conn: dict, display_name: str, window: str, user_id: str = "",
) -> bool:
    """לקוח חרג ממגבלת הקצב — הוא לא קיבל שום הודעה."""
    window_he = {"minute": "בדקה", "hour": "בשעה", "day": "ביום"}.get(window, "")
    return await notify(
        bot, conn,
        f"⏸️ {display_name} שלח הרבה הודעות ברצף ({window_he}), "
        "ולכן הפסקתי לענות לו לעכשיו. הוא לא קיבל שום הודעה על כך. "
        "אם זה נראה לך לגיטימי — כדאי שתענה לו בעצמך.",
        kind="rate_limited", subject=_subject(user_id, display_name),
    )


async def notify_missing_permission(bot, conn: dict) -> bool:
    """אין `can_reply` — הבוט קורא אבל לא יכול לענות."""
    return await notify(
        bot, conn,
        "⚠️ אני מקבל את ההודעות שנכנסות אליך, אבל אין לי הרשאה לענות "
        "בשמך — אז לקוחות שכותבים לך לא מקבלים ממני כלום.\n"
        "לתיקון: הגדרות טלגרם ← Chatbots ← לבחור אותי ← לאשר לי לענות "
        "להודעות.",
        kind="missing_permission",
    )


async def notify_window_closed(
    bot, conn: dict, display_name: str, user_id: str = "",
) -> bool:
    """חלון 24 השעות נסגר — אי אפשר לענות עד שהלקוח יכתוב שוב."""
    return await notify(
        bot, conn,
        f"🕐 לא הצלחתי לענות ל{display_name} — עברו יותר מ-24 שעות מאז "
        "ההודעה האחרונה שלו, וטלגרם לא מרשה לי לכתוב בשמך בצ'אט שלא היה "
        "פעיל. זו מגבלה של טלגרם, לא תקלה.\n"
        "אם זה דחוף — כתוב לו בעצמך מהצ'אט.",
        kind="window_closed", subject=_subject(user_id, display_name),
    )


async def notify_send_failed(
    bot, conn: dict, display_name: str, reason: str, user_id: str = "",
) -> bool:
    """כשל שליחה שלא סווג — הבעלים צריך לדעת שהלקוח לא קיבל תשובה."""
    return await notify(
        bot, conn,
        f"⚠️ ניסיתי לענות ל{display_name} וזה לא עבר ({reason}). "
        "הוא לא קיבל תשובה — כדאי שתסתכל.",
        kind="send_failed", subject=_subject(user_id, display_name),
    )


async def notify_handoff(
    bot, conn: dict, display_name: str, question: str,
    target: tuple[str, str] | None = None,
) -> bool:
    """הבוט לא ידע לענות והעביר לבעלים. **בלי דה-דופ** — כל פנייה נפרדת.

    זו ההתראה שהבעלים הכי סביר יגיב עליה, ולכן `target` נמסר כאן:
    ‏`/pause` בתגובה משתיק בדיוק את השיחה הזו.
    """
    quoted = question.strip()
    if len(quoted) > 300:
        quoted = quoted[:300].rstrip() + "…"
    return await notify(
        bot, conn,
        f"🔔 {display_name} שאל משהו שאין לי עליו תשובה:\n"
        f"«{quoted}»\n\n"
        "עניתי לו שאבדוק ואחזור. תענה לו ישירות בצ'אט — אני אשתוק שם.",
        target=target,
    )


async def notify_media(
    bot, conn: dict, display_name: str, user_id: str = "",
) -> bool:
    """הגיעה הודעת מדיה — לא שומרים אותה ולא מנסים להבין (מזעור)."""
    return await notify(
        bot, conn,
        f"📎 {display_name} שלח לך קובץ או הקלטה. אני לא יודע לקרוא כאלה, "
        "אז עניתי לו משפט קצר שתחזור אליו.",
        kind="media", subject=_subject(user_id, display_name),
    )


async def notify_connection_changed(bot, conn: dict, is_enabled: bool, can_reply: bool) -> bool:
    """שינוי במצב החיבור או בהרשאות."""
    if not is_enabled:
        text = (
            "🔌 החיבור שלי לחשבון שלך נותק. מעכשיו אני לא רואה הודעות "
            "ולא עונה לאף אחד.\n"
            "לחיבור מחדש: הגדרות טלגרם ← Chatbots."
        )
    elif not can_reply:
        text = (
            "✅ החיבור פעיל, אבל בלי הרשאה לענות — אני רק קורא.\n"
            "להפעלה מלאה: הגדרות טלגרם ← Chatbots ← לאשר לי לענות להודעות."
        )
    else:
        text = "✅ מחובר ומוכן. מעכשיו אני עונה ללקוחות שכותבים לך."
    return await notify(bot, conn, text, kind="connection_changed")
