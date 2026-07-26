"""
Message Processor — הליבה הערוץ-אגנוסטית של עיבוד הודעה נכנסת.

הועתק מ-`ai-business-bot/core/message_processor.py` (‏ROADMAP T0.8) עם
הניקויים שהערוץ מחייב:

- **אין ענפי booking ואין הפניה לכפתורים** — הם לא קיימים בערוץ הזה.
- **אין תשובות סטטיות לברכה/פרידה** — "ברוכים הבאים! 👋" מסגיר בוט;
  גם ברכה עוברת דרך ה-LLM עם הפרסונה (‏PLAN §3.3).
- **רישום פער הידע עבר מ-`chunks_used == 0` לזיהוי `[HANDOFF]`** — אין
  chunks יותר, וה-handoff הוא הסימן שלא הייתה תשובה בבסיס הידע.

הפונקציה מחזירה `MessageResult` ושכבת הערוץ מבצעת — היא לא שולחת כלום
בעצמה ולא מכירה טלגרם.
"""

import logging
from dataclasses import dataclass, field

import database as db
from config import CONTEXT_WINDOW_SIZE, FALLBACK_RESPONSE, HANDOFF_MARKER
from intent import Intent, detect_intent_with_llm
from llm import generate_answer, strip_html_tags, strip_source_citation
from rate_limiter import check_rate_limit, record_message

logger = logging.getLogger(__name__)

__all__ = [
    "MessageResult",
    "process_incoming_message",
    "should_handoff_to_human",
    "strip_handoff_marker",
    "ESCALATION_THRESHOLD",
]

# אחרי כמה handoffs רצופים הבוט מפסיק לנסות ומשתיק את עצמו עד שהבעלים
# ייכנס (‏PLAN §3.3 — "ההסלמה השלישית = handoff שקט לבעלים"). ההתראה
# לבעלים נשלחת כבר בפעם הראשונה (‏PLAN §4.5) — מה שמסלים הוא ההשתקה.
ESCALATION_THRESHOLD = 3


@dataclass
class MessageResult:
    """תוצאת עיבוד — מוחזרת לשכבת הערוץ לביצוע.

    text: הטקסט לשליחה ללקוח. מחרוזת ריקה = לא שולחים כלום.
    intent: הכוונה שזוהתה (לתיוג ולרישום פערי ידע).
    action: ‏'reply' | ‏'handoff' | ‏'rate_limited' | ‏'silent'.
    consecutive_fallbacks: המונה המעודכן — הקורא שומר אותו ב-DB.
    escalate_takeover: הגיעו לסף ההסלמה — להשתיק את הצ'אט עד שהבעלים יענה.
    handoff_reason: תקציר לבעלים (מה הלקוח שאל) כש-action='handoff'.
    needs_summarization: האם לתזמן סיכום שיחה ברקע.
    rate_limit_window: איזה חלון נחרג (למידע לבעלים) כש-action='rate_limited'.
    """

    text: str
    intent: Intent
    action: str = "reply"
    consecutive_fallbacks: int = 0
    escalate_takeover: bool = False
    handoff_reason: str = ""
    needs_summarization: bool = False
    rate_limit_window: str = ""
    sources: list[str] = field(default_factory=list)


# ─── זיהוי handoff ───────────────────────────────────────────────────────


def should_handoff_to_human(text: str) -> bool:
    """זיהוי תשובת LLM שמבקשת להעביר לבעל העסק.

    מנגנון: הפרומפט מורה ל-LLM לפתוח את התשובה ב-HANDOFF_MARKER. כאן
    בודקים `startswith` בלבד — **אסור fuzzy matching** (חיפוש "אעביר את
    הפנייה" וכו'), הוא מייצר false positives שחוסמים תשובות תמימות.

    רשת ביטחון אחת: התאמה מדויקת ל-FALLBACK_RESPONSE — המקרה שבו הטוקן
    הוסר במקום אחר בצינור אבל הטקסט הקבוע נשאר.
    """
    if not text:
        return False
    t = text.strip()
    if t.startswith(HANDOFF_MARKER):
        return True
    return t == FALLBACK_RESPONSE.strip()


def strip_handoff_marker(text: str) -> str:
    """הסרת HANDOFF_MARKER מתחילת התשובה.

    הטוקן הוא סיגנל פנימי בין ה-LLM לפרסר; **אסור** שיגיע ללקוח.
    מוסר תמיד — גם כשלא זוהה handoff — למקרה שהמודל הוסיף אותו בטעות
    באמצע זרימה אחרת.
    """
    if not text:
        return text
    stripped = text.lstrip()
    if stripped.startswith(HANDOFF_MARKER):
        return stripped[len(HANDOFF_MARKER):].lstrip()
    return text


def sanitize_outgoing(text: str) -> str:
    """ניקוי אחרון לפני שליחה ללקוח: טוקן handoff, תגי HTML, ציון מקור.

    כל שליחה ללקוח **חייבת** לעבור דרך כאן (‏CLAUDE.md — הטוקן לעולם לא
    דולף, והערוץ הוא טקסט נקי).
    """
    return strip_source_citation(strip_html_tags(strip_handoff_marker(text or ""))).strip()


# ─── עיבוד הודעה ─────────────────────────────────────────────────────────


def process_incoming_message(
    user_id: str,
    text: str,
    user_info: dict,
    consecutive_fallbacks: int = 0,
    rate_limit_already_checked: bool = False,
    channel: str = "telegram_business",
    conversation_history: list[dict] | None = None,
) -> MessageResult:
    """עיבוד הודעת טקסט מלקוח: כוונה ← ‏LLM ← ‏handoff/תשובה.

    Args:
        user_id: מזהה המשתמש (מחרוזת).
        text: טקסט ההודעה.
        user_info: מידע על המשתמש — ‏display_name (חובה).
        consecutive_fallbacks: מונה ה-handoffs הרצופים (מה-DB).
        rate_limit_already_checked: ‏True אם הקורא כבר בדק (ה-handler
            בערוץ הזה בודק בעצמו, כדי להתריע לבעלים בעצמו).
        channel: הערוץ (נשמר עם כל הודעה).
        conversation_history: ההיסטוריה **לפני** ההודעה הנוכחית. הקורא
            שולף אותה לפני שהוא שומר את ההודעה הנכנסת (ה-handler שומר
            תמיד, עוד לפני ה-guards), כדי שהשאלה לא תופיע פעמיים בפרומפט.

    Returns:
        MessageResult עם הטקסט, הכוונה והפעולה הנדרשת מהערוץ.
    """
    display_name = user_info.get("display_name", "")
    user_id = str(user_id)

    # ── מגבלת קצב ──
    if not rate_limit_already_checked:
        window = check_rate_limit(user_id)
        if window is not None:
            # שתיקה ללקוח — הטקסט ריק בכוונה (PLAN §4.3)
            return MessageResult(
                text="", intent=Intent.GENERAL,
                action="rate_limited", rate_limit_window=window,
                consecutive_fallbacks=consecutive_fallbacks,
            )
        record_message(user_id)

    # ── זיהוי כוונה (תיוג בלבד) ──
    intent = detect_intent_with_llm(text)

    # ── היסטוריה ──
    if conversation_history is None:
        conversation_history = db.get_conversation_history(user_id, limit=CONTEXT_WINDOW_SIZE)

    # ── קריאת ה-LLM ──
    result = generate_answer(
        user_query=text,
        conversation_history=conversation_history,
        user_id=user_id,
        username=display_name,
        channel=channel,
    )
    raw_answer = result["answer"]

    # זיהוי handoff חייב לקרות **לפני** הסרת הטוקן.
    is_handoff = should_handoff_to_human(raw_answer)
    # בקשה מפורשת לדבר עם אדם היא handoff גם אם המודל לא סימן — הלקוח
    # אמר את זה במפורש, ואין טעם לבקש ממנו לנסח מחדש.
    if intent == Intent.HUMAN_AGENT:
        is_handoff = True
    # בקשת מחיקה כופה handoff — ולא מחיקה. שתי סיבות: (א) ה-regex הוא
    # היוריסטיקה ומחיקה בלתי הפיכה, ולכן ההחלטה של בעל העסק; (ב) משפט
    # הגישור ("אבדוק ואחזור אליך") אינו מבטיח כלום, בעוד תשובת LLM
    # חופשית עלולה לומר "מחקתי" כשלא נמחק דבר.
    if intent == Intent.DELETE_REQUEST:
        is_handoff = True
    answer = sanitize_outgoing(raw_answer)

    if is_handoff:
        fallback_count = consecutive_fallbacks + 1

        # רישום פער ידע — הטריגר בערוץ הזה הוא ה-handoff (אין chunks).
        try:
            db.save_unanswered_question(
                user_id, display_name, text, intent=intent.value, channel=channel,
            )
        except Exception:
            logger.error("כשל ברישום פער ידע", exc_info=True)

        # אם המודל נכשל לגמרי (או השאיר טקסט ריק) — משפט גישור מוגדר-tenant
        if not answer:
            answer = _bridge_message()

        return MessageResult(
            text=answer,
            intent=intent,
            action="handoff",
            consecutive_fallbacks=fallback_count,
            escalate_takeover=fallback_count >= ESCALATION_THRESHOLD,
            handoff_reason=text,
            needs_summarization=True,
        )

    # ── תשובה רגילה ──
    if not answer:
        # תשובה ריקה מהמודל — מתייחסים אליה ככשל ולא שולחים הודעה ריקה
        logger.warning("ה-LLM החזיר תשובה ריקה — נשלח משפט גישור")
        answer = _bridge_message()

    return MessageResult(
        text=answer,
        intent=intent,
        action="reply",
        consecutive_fallbacks=0,
        needs_summarization=True,
    )


def _bridge_message() -> str:
    """משפט הגישור המוגדר ב-tenant ("בודק ואחזור אליך בהקדם")."""
    try:
        settings = db.get_bot_settings() or {}
        msg = (settings.get("handoff_bridge_message") or "").strip()
        if msg:
            return msg
    except Exception:
        logger.error("כשל בקריאת משפט הגישור מההגדרות", exc_info=True)
    return FALLBACK_RESPONSE
