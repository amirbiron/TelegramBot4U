"""
‏dispatch יוצא — שליחת התשובה ללקוח בשם בעל העסק.

כל שליחה ללקוח עוברת דרך `dispatch_result` (‏ROADMAP T1.3). מה שהיא
מבטיחה, ובגלל זה אין דרך אחרת לשלוח:

1. **‏`business_connection_id` בכל קריאה יוצאת.** ה-shortcuts של PTB
   (‏`msg.reply_text`) מעבירים אותו אוטומטית; קריאת `Bot` ידנית — במפורש.
   בלעדיו ההודעה יוצאת מהבוט ולא מהחשבון של הבעלים, ובצ'אט של לקוח זר
   היא פשוט נכשלת.
2. **פיצול מעל 4096 תווים** — המגבלה של טלגרם. בלי פיצול ההודעה נדחית.
3. **סיווג כשל שליחה** — חלון סגור / אין הרשאה / אחר. הסיווג נשמר,
   הבעלים מקבל התראה, והלקוח **לא מקבל כלום**. אין retry עיוור: אם
   החלון נסגר, ניסיון חוזר ייכשל באותה צורה עד שהלקוח יכתוב שוב
   (‏PLAN §1.4, ‏docs/verification_log.md → V5).
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram.error import Forbidden, RetryAfter, TelegramError

import database as db
from services import owner_channel

logger = logging.getLogger(__name__)

# מגבלת האורך של הודעת טקסט בטלגרם
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# סיווג כשלי שליחה. הדפוסים נבדקים על ההודעה ב-lowercase.
#
# **למה סט דפוסים ולא מחרוזת אחת:** נוסח השגיאה המדויק של "חלון סגור"
# טרם נראה בפרודקשן (V5 — התיעוד מגדיר את המגבלה אבל לא את נוסח
# השגיאה). ברירת המחדל בטוחה: כשל לא מזוהה מסווג כ-'other', נרשם ללוג
# המלא ומתריע לבעלים. כשהשגיאה האמיתית תיראה — מוסיפים דפוס אחד.
_WINDOW_CLOSED_PATTERNS = (
    "business_peer_invalid",
    "peer_id_invalid",
    "chat not found",
    "active in the last 24",
    "not active",
    "topic_closed",
)
_NO_PERMISSION_PATTERNS = (
    "can_reply",
    "not enough rights",
    "no rights",
    "business bot rights",
    "business_connection_invalid",
    "bot_business_missing",
)

FAILURE_WINDOW_CLOSED = "window_closed"
FAILURE_NO_PERMISSION = "no_permission"
FAILURE_OTHER = "other"

# ‏typing פרופורציוני: כשהדגל דלוק, ממתינים לפני השליחה כדי שהתשובה לא
# תיחת שלמה אחרי 0.8 שניות (מה שמסגיר אוטומציה בצ'אט אישי). הפעולה
# ‏sendChatAction עצמה נשלחת תמיד — היא זולה ומיידית.
_TYPING_SECONDS_PER_CHAR = 1 / 45
_TYPING_MAX_SECONDS = 6.0


def classify_send_error(exc: BaseException) -> str:
    """סיווג כשל שליחה לאחת משלוש הקטגוריות."""
    message = str(exc).lower()
    if any(p in message for p in _NO_PERMISSION_PATTERNS):
        return FAILURE_NO_PERMISSION
    if any(p in message for p in _WINDOW_CLOSED_PATTERNS):
        return FAILURE_WINDOW_CLOSED
    if isinstance(exc, Forbidden):
        # הלקוח חסם, או שההרשאה נשללה — בשני המקרים אין מה לנסות שוב
        return FAILURE_NO_PERMISSION
    return FAILURE_OTHER


def split_message(text: str, limit: int | None = None) -> list[str]:
    """פיצול טקסט ארוך לחלקים, בגבול פסקה או משפט.

    מנסה לשבור בפסקה, אחר כך בסוף משפט, ורק כמוצא אחרון באמצע — כדי
    שהלקוח לא יראה מילה חתוכה.

    ‏limit נפתר בזמן ריצה ולא כברירת מחדל בחתימה: ברירת מחדל נקשרת
    בזמן הגדרת הפונקציה, ואז שינוי הקבוע (טסט, או תקרה שתשתנה בטלגרם)
    לא היה משפיע בפועל.
    """
    limit = limit or TELEGRAM_MAX_MESSAGE_LENGTH
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 3:
            cut = window.rfind("\n")
        if cut < limit // 3:
            match = None
            for m in re.finditer(r"[.!?]\s", window):
                match = m
            cut = match.end() if match and match.end() >= limit // 3 else -1
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut < limit // 3:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return [p for p in parts if p]


async def _typing_pause(bot, chat_id: int, connection_id: str, text: str) -> None:
    """‏sendChatAction typing, ובדגל דלוק — גם השהיה פרופורציונלית.

    ההשהיה מאחורי `HUMANIZED_DELIVERY` כי היא מוסיפה לטנסי אמיתית.
    היא מה שנשאר מ"ההזרמה האנושית" של PLAN §1.8 אחרי ש-V4 שלל את
    `sendMessageDraft` מעל business connection.
    """
    import config as _cfg

    try:
        await bot.send_chat_action(
            chat_id=chat_id, action="typing", business_connection_id=connection_id,
        )
    except Exception:
        # פעולת typing היא קישוט — כשל בה לא מונע את התשובה עצמה
        logger.debug("send_chat_action נכשל", exc_info=True)

    if getattr(_cfg, "HUMANIZED_DELIVERY", False):
        delay = min(len(text) * _TYPING_SECONDS_PER_CHAR, _TYPING_MAX_SECONDS)
        if delay > 0:
            await asyncio.sleep(delay)


async def send_to_customer(
    bot, chat_id: int, connection_id: str, text: str, user_id: str, display_name: str,
    conn: dict | None = None,
) -> str:
    """שליחת טקסט ללקוח בשם הבעלים. מחזיר את מה **שנמסר בפועל**.

    בכשל: מסווג, מסמן ב-DB, ומתריע לבעלים. **לא** מנסה שוב.

    **למה מחזירים טקסט ולא bool:** הודעה ארוכה מפוצלת לצ'אנקים, וכשל
    בצ'אנק השלישי לא מבטל את השניים שהלקוח כבר קרא. ‏bool היה מחזיר
    False, הקורא לא היה שומר כלום, וההיסטוריה הייתה טוענת שלא ענינו —
    כלומר בפנייה הבאה המודל היה חוזר על אותו תוכן מול לקוח שכבר קיבל
    אותו. הערך הריק ('') עדיין falsy, כך שבדיקות `if sent:` קיימות
    ממשיכות לעבוד.
    """
    chunks = split_message(text)
    if not chunks:
        return ""

    delivered: list[str] = []

    await _typing_pause(bot, chat_id, connection_id, chunks[0])

    for index, chunk in enumerate(chunks):
        try:
            await bot.send_message(
                chat_id=chat_id, text=chunk, business_connection_id=connection_id,
            )
        except RetryAfter as exc:
            # ‏flood control של טלגרם — ההמתנה שהיא מבקשת היא הפתרון,
            # וניסיון אחד חוזר. זה לא retry עיוור: טלגרם אמרה כמה לחכות.
            wait = min(getattr(exc, "retry_after", 1) or 1, 30)
            logger.warning("טלגרם ביקשה להמתין %s שניות — ממתינים ומנסים שוב", wait)
            await asyncio.sleep(wait + 0.5)
            try:
                await bot.send_message(
                    chat_id=chat_id, text=chunk, business_connection_id=connection_id,
                )
            except TelegramError as exc2:
                await _handle_send_failure(bot, exc2, user_id, display_name, conn)
                return "\n\n".join(delivered)
        except TelegramError as exc:
            await _handle_send_failure(bot, exc, user_id, display_name, conn)
            return "\n\n".join(delivered)
        except Exception as exc:
            # כשל שאינו של טלגרם (תקלת תעבורה, timeout, באג אצלנו) —
            # מבחינת הלקוח והבעלים הוא זהה לחלוטין לכשל של טלגרם: הלקוח
            # לא קיבל תשובה. קודם הענף הזה רק כתב ללוג, כלומר ההודעה
            # נעלמה בשקט: ה-DB לא סומן והבעלים לא ידע. ‏`classify_send_error`
            # יסווג אותו כ-'other' — וזו בדיוק המשמעות.
            logger.error("שליחה ללקוח נכשלה מסיבה לא צפויה", exc_info=True)
            await _handle_send_failure(bot, exc, user_id, display_name, conn)
            return "\n\n".join(delivered)

        delivered.append(chunk)
        if index < len(chunks) - 1:
            # רווח קטן בין חלקים — רצף הודעות מיידי נראה מכונתי
            await asyncio.sleep(0.4)
    return text


async def _handle_send_failure(
    bot, exc: BaseException, user_id: str, display_name: str, conn: dict | None,
) -> None:
    """סיווג הכשל, סימון ב-DB והתראה לבעלים."""
    reason = classify_send_error(exc)
    # ה-exception המלא ללוג (בלי תוכן ההודעה — PII)
    logger.warning("כשל שליחה ללקוח: reason=%s error=%s", reason, exc)

    try:
        db.mark_send_failure(user_id, reason)
    except Exception:
        logger.error("סימון כשל השליחה ב-DB נכשל", exc_info=True)

    if conn is None:
        return
    try:
        if reason == FAILURE_WINDOW_CLOSED:
            await owner_channel.notify_window_closed(
                bot, conn, display_name, user_id=user_id,
            )
        elif reason == FAILURE_NO_PERMISSION:
            await owner_channel.notify_missing_permission(bot, conn)
        else:
            await owner_channel.notify_send_failed(
                bot, conn, display_name, reason, user_id=user_id,
            )
    except Exception:
        logger.error("התראת כשל השליחה לבעלים נכשלה", exc_info=True)


async def dispatch_result(bot, result, msg, conn: dict, display_name: str) -> None:
    """ביצוע ה-`MessageResult` מול הלקוח ומול הבעלים.

    - ‏action='reply'   ⇒ שולחים את התשובה.
    - ‏action='handoff' ⇒ שולחים את משפט הגישור **וגם** מתריעים לבעלים.
    - טקסט ריק ⇒ שתיקה מוחלטת (rate limit, השתקה) — בלי הודעת מערכת.
    """
    user_id = str(msg.from_user.id) if msg.from_user else ""
    chat_id = msg.chat.id
    connection_id = msg.business_connection_id

    if result.text:
        sent = await send_to_customer(
            bot, chat_id, connection_id, result.text, user_id, display_name, conn,
        )
        if sent:
            # נשמר מה שנמסר, לא מה שניסינו לשלוח: בשליחה חלקית ההפרש
            # הוא בדיוק מה שהלקוח לא ראה, ואסור שההיסטוריה תטען אחרת.
            if sent != result.text:
                logger.warning(
                    "שליחה חלקית ללקוח — נשמרו %d מתוך %d תווים",
                    len(sent), len(result.text),
                )
            try:
                db.save_message(
                    user_id, display_name, "assistant", sent,
                    authored_by="bot", tg_chat_id=chat_id,
                )
            except Exception:
                logger.error("שמירת התשובה ב-DB נכשלה", exc_info=True)

    if result.action == "handoff":
        try:
            await owner_channel.notify_handoff(
                bot, conn, display_name, result.handoff_reason,
                target=(user_id, str(chat_id)),
            )
        except Exception:
            logger.error("התראת ה-handoff לבעלים נכשלה", exc_info=True)
