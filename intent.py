"""
Intent Detection — סיווג הודעות לקוח.

הועתק מ-`ai-business-bot/intent.py` (‏ROADMAP T0.8) וצומצם למרחב הכוונות
של הערוץ: אין תורים (‏booking/cancel/reschedule) — העסק לא מתאם אונליין
בערוץ הזה, ופנייה כזאת מטופלת כבקשה לדבר עם בעל העסק.

**מה הכוונה עושה כאן, ומה לא.** בערוץ הזה *כל* הודעה עוברת דרך אותו
צינור LLM — אין תשובות סטטיות ואין ניתוב לפי כפתורים ("ברוכים הבאים! 👋"
מסגיר בוט, ‏PLAN §3.3). לכן הכוונה משמשת ל**תיוג** בלבד: רישום פערי ידע
עם קטגוריה, סטטיסטיקה בפאנל, וזיהוי בקשות מפורשות לדבר עם אדם.

מכיוון שזה השימוש היחיד, ‏`LLM_INTENT_ENABLED` כבוי כברירת מחדל בפרויקט
הזה: קריאת LLM שנייה בכל הודעה מכפילה עלות ולטנסי עבור תווית בלבד.
ה-regex המלא הוא ברירת המחדל; מי שרוצה דיוק גבוה יותר מדליק את הדגל.
"""

import json
import logging
import re
from enum import Enum

logger = logging.getLogger(__name__)


class Intent(Enum):
    GREETING = "greeting"
    FAREWELL = "farewell"
    BUSINESS_HOURS = "business_hours"
    PRICING = "pricing"
    LOCATION = "location"
    HUMAN_AGENT = "human_agent"
    COMPLAINT = "complaint"
    # בקשת מחיקה בשפה חופשית ("תמחקו את המידע שלי") — זכות לפי תיקון 13.
    # **לעולם לא מתבצעת אוטומטית:** ‏regex הוא היוריסטיקה, ומחיקה היא
    # בלתי הפיכה. הכוונה מייצרת התראה לבעלים, והוא מאשר (‏T4.2).
    DELETE_REQUEST = "delete_request"
    GENERAL = "general"


_INTENT_BY_VALUE: dict[str, Intent] = {i.value: i for i in Intent}


# ─── Regex fast path — ברכות ופרידות (anchored, מדויק) ─────────────────

_GREETING_PATTERN = re.compile(
    r"^("
    r"hi|hello|hey|hiya|good morning|good evening|good afternoon"
    r"|שלום|היי|הי|בוקר טוב|ערב טוב|צהריים טובים|מה נשמע|מה קורה|אהלן|הלו"
    r")[.!?\s]*$",
    re.IGNORECASE,
)

_FAREWELL_PATTERN = re.compile(
    r"^("
    r"thanks|thank you|bye|goodbye|see you|have a good day|good night"
    r"|תודה|תודה רבה|ביי|ביביי|להתראות|יום טוב|לילה טוב|שבוע טוב|יאללה ביי"
    r")[.!?\s]*$",
    re.IGNORECASE,
)

_FAST_PATTERNS: list[tuple[Intent, re.Pattern]] = [
    (Intent.GREETING, _GREETING_PATTERN),
    (Intent.FAREWELL, _FAREWELL_PATTERN),
]

# בקשה מפורשת לדבר עם אדם — **הכוונה היחידה עם השלכה פונקציונלית**
# (`message_processor` כופה עליה handoff מיידי). שאר הכוונות הן תיוג בלבד.
_HUMAN_AGENT_PATTERN = re.compile(
    r"("
    r"talk\s*to\s*(an?\s*)?(human|person|agent|representative|someone)"
    r"|i\s*need\s*(an?\s*)?(human|person|agent)"
    r"|can\s*i\s*(speak|talk)\s*(to|with)\s*(an?\s*)?(human|person|agent)"
    r"|אדם\s*אמיתי"
    r"|לדבר\s*עם\s*(מישהו|בנאדם|נציג|אדם|בעל\s*העסק|בעלים|הבעלים)"
    r"|אני\s*רוצה\s*(לדבר\s*עם\s*)?(נציג|בנאדם|אדם|בעל\s*העסק|בעלים)"
    r"|אפשר\s*לדבר\s*עם\s*(נציג|מישהו|בעל\s*העסק|בעלים)"
    r"|מבקש\s*ש?(יחזרו|יחזור|בעל\s*העסק|מישהו)"
    r"|ש(יחזרו|יחזור)\s*אלי"
    r"|בעל\s*העסק\s*ש?י(חזור|תקשר)"
    r")",
    re.IGNORECASE,
)


_DELETE_REQUEST_PATTERN = re.compile(
    r"("
    r"תמחק(?:ו|י)?\s*(?:לי\s*)?(?:את\s*)?(?:כל\s*)?(?:ה?מידע|ה?נתונים|ה?פרטים|ה?היסטוריה)"
    r"|(?:מחק|למחוק)\s*(?:לי\s*)?(?:את\s*)?(?:כל\s*)?(?:ה?מידע|ה?נתונים|ה?פרטים)\s*שלי"
    r"|אני\s*(?:רוצה|מבקש(?:ת)?)\s*(?:ש?תמחקו|למחוק)"
    r"|(?:תסירו|להסיר)\s*(?:אותי|את\s*הפרטים\s*שלי)"
    # ‏"את" לבדו תפס כל מושא שהוא — "אל תשמרו את הקבלה" סווג כבקשת
    # מחיקה. נדרש מושא שקשור לנתונים אישיים.
    r"|אל\s*תשמרו\s*(?:עלי|את\s*(?:כל\s*)?(?:ה?מידע|ה?נתונים|ה?פרטים|ה?היסטוריה))"
    r"|delete\s*(?:all\s*)?my\s*(?:data|info|information|details)"
    r"|(?:remove|erase)\s*(?:all\s*)?my\s*(?:data|info|details)"
    r"|forget\s*(?:everything\s*)?about\s*me"
    r")",
    re.IGNORECASE,
)


# ─── Regex מלא — כל הכוונות ────────────────────────────────────────────
_FALLBACK_PATTERNS: list[tuple[Intent, re.Pattern]] = [
    (Intent.GREETING, _GREETING_PATTERN),
    (Intent.FAREWELL, _FAREWELL_PATTERN),
    # DELETE_REQUEST ראשון מבין כוונות הפעולה: "תמחקו את המידע שלי,
    # אני רוצה לדבר עם מישהו" הוא קודם כול בקשת מחיקה — זו זכות לפי
    # חוק, והיא גוברת על בקשה לנציג.
    (Intent.DELETE_REQUEST, _DELETE_REQUEST_PATTERN),
    # HUMAN_AGENT לפני כל דפוס התיוג: הוא היחיד שמייצר פעולה, ואסור
    # שדפוס תיוג יגנוב אותו. "כמה יעלה לדבר עם בעל העסק?" מכיל גם מחיר
    # וגם בקשה לאדם — הבקשה לאדם היא שמשנה מה קורה.
    (Intent.HUMAN_AGENT, _HUMAN_AGENT_PATTERN),
    (
        Intent.BUSINESS_HOURS,
        re.compile(
            r"("
            r"are\s*you\s*open|when\s*(do\s*you|are\s*you)\s*(open|close)"
            r"|what\s*(are\s*)?your\s*hours|opening\s*hours|business\s*hours"
            r"|what\s*time\s*(do\s*you|are\s*you)\s*(open|close)"
            r"|שעות\s*פתיחה|שעות\s*פעילות|שעות\s*עבודה"
            r"|מתי\s*(אתם\s*)?(פותחים|סוגרים|פתוחים)"
            r"|אתם\s*פתוחים|פתוח\s*היום|פתוח\s*עכשיו|פתוחים\s*היום|פתוחים\s*עכשיו"
            r"|האם\s*(אתם\s*)?פתוחים|סגור\s*היום|סגורים\s*היום"
            r"|עד\s*מתי\s*(אתם\s*)?(פתוחים|פתוח)"
            r"|מה\s*שעות\s*(הפתיחה|הפעילות)"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.PRICING,
        re.compile(
            r"("
            r"how\s*much|what.*price\b|what.*cost\b|pricing|price\s*list"
            r"|כמה\s*עולה|כמה\s*זה\s*עולה|מה\s*המחיר|מה\s*העלות|מחיר|מחירון|מחירים"
            r"|כמה\s*יעלה|כמה\s*כסף|עלות|תעריף|תעריפים"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.COMPLAINT,
        re.compile(
            r"("
            r"i\s*(want\s*to\s*)?complain|complaint|not\s*happy|not\s*satisfied"
            r"|terrible\s*service|bad\s*service|worst\s*service|unacceptable"
            r"|i\s*want\s*a\s*refund|give\s*me\s*my\s*money\s*back"
            r"|אני\s*לא\s*מרוצה|לא\s*מרוצה|יש\s*לי\s*בעיה|רוצה\s*להתלונן|תלונה"
            r"|שירות\s*גרוע|שירות\s*נוראי|מאוכזב|מאוכזבת|אני\s*כועס|אני\s*כועסת"
            r"|לא\s*בסדר|חוויה\s*רעה|חוויה\s*גרועה"
            r"|בושה|איזה\s*זלזול"
            r"|עושים\s*צחוק|עושה\s*צחוק"
            r"|רוצה\s*זיכוי|תחזירו\s*לי\s*את\s*הכסף"
            r"|אף\s*אחד\s*לא\s*עונה|לא\s*מגיבים"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.LOCATION,
        re.compile(
            r"("
            r"where\s*are\s*you|what.*address|how\s*(do\s*i\s*)?get\s*there"
            r"|your\s*location|directions"
            r"|איפה\s*אתם|מה\s*הכתובת|כתובת|איך\s*מגיעים|איך\s*אפשר\s*להגיע"
            r"|מיקום|היכן\s*אתם|איפה\s*(ה)?(חנות|סלון|עסק|מקום)|הגעה"
            r")",
            re.IGNORECASE,
        ),
    ),
]


# ─── LLM Function Calling — הגדרת הכלי לסיווג ──────────────────────────

_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "classify_intent",
        "description": "סיווג כוונת הודעת הלקוח לקטגוריה המתאימה",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [i.value for i in Intent],
                    "description": (
                        "הכוונה המזוהה:\n"
                        "- greeting: ברכה בלבד (שלום, היי, בוקר טוב)\n"
                        "- farewell: פרידה/תודה בלבד (ביי, תודה, להתראות)\n"
                        "- business_hours: שאלה על שעות פתיחה/סגירה/זמינות\n"
                        "- pricing: שאלה על מחיר, עלות, תעריף\n"
                        "- location: שאלה על כתובת, מיקום, הגעה\n"
                        "- human_agent: בקשה לדבר עם בעל העסק/אדם אמיתי\n"
                        "- delete_request: בקשה למחוק את המידע/הנתונים "
                        "שנשמרו על הלקוח\n"
                        "- complaint: תלונה, תסכול, חוויה רעה\n"
                        "- general: כל שאלה אחרת"
                    ),
                },
            },
            "required": ["intent"],
        },
    },
}

_LLM_SYSTEM_PROMPT = (
    "אתה מסווג כוונות של הודעות שלקוחות שולחים לעסק.\n"
    "קרא את ההודעה וסווג אותה לקטגוריה המתאימה ביותר דרך הפונקציה "
    "classify_intent.\n"
    "דוגמאות:\n"
    '- "זה יקר לי" → pricing\n'
    '- "פתוחים בשבת?" → business_hours\n'
    '- "איפה זה?" → location\n'
    '- "אני מתוסכל" → complaint\n'
    '- "אפשר לדבר עם דנה?" → human_agent\n'
    '- "תמחקו את כל המידע שלי" → delete_request\n'
    "אם לא ברור — סווג כ-general."
)


def detect_intent(message: str) -> Intent:
    """‏fast path — ברכות ופרידות בלבד (regex מעוגן)."""
    text = (message or "").strip()
    if not text:
        return Intent.GENERAL
    for intent, pattern in _FAST_PATTERNS:
        if pattern.search(text):
            logger.info("intent (fast): %s", intent.value)
            return intent
    return Intent.GENERAL


def _detect_intent_regex_full(message: str) -> Intent:
    """‏regex מלא — כל הכוונות."""
    text = (message or "").strip()
    if not text:
        return Intent.GENERAL
    for intent, pattern in _FALLBACK_PATTERNS:
        if pattern.search(text):
            logger.info("intent (regex): %s", intent.value)
            return intent
    return Intent.GENERAL


def _detect_intent_llm(message: str) -> Intent:
    """סיווג באמצעות LLM function calling (מודל קל, tool_choice כפוי).

    כל כשל — נופלים ל-regex המלא.
    """
    from config import INTENT_MODEL
    from llm_client import INTENT_TIMEOUT_SECONDS
    from openai_client import get_openai_client

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=INTENT_MODEL,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            tools=[_INTENT_TOOL],
            tool_choice={"type": "function", "function": {"name": "classify_intent"}},
            temperature=0,
            max_tokens=50,
            # timeout קצר: זו תווית בלבד, ואין סיבה שהלקוח יחכה בגללה
            timeout=INTENT_TIMEOUT_SECONDS,
        )
        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            logger.warning("intent: ה-LLM לא החזיר tool_call — נופלים ל-regex")
            return _detect_intent_regex_full(message)
        args = json.loads(tool_calls[0].function.arguments)
        intent = _INTENT_BY_VALUE.get(args.get("intent", "general"), Intent.GENERAL)
        logger.info("intent (LLM): %s", intent.value)
        return intent
    except Exception as e:
        logger.error("סיווג כוונה ב-LLM נכשל, נופלים ל-regex: %s", e)
        return _detect_intent_regex_full(message)


def detect_intent_with_llm(message: str) -> Intent:
    """סיווג היברידי: ‏regex מהיר לברכות, ואז LLM (אם מודלק) או regex מלא."""
    from config import LLM_INTENT_ENABLED

    text = (message or "").strip()
    if not text:
        return Intent.GENERAL

    fast_intent = detect_intent(text)
    if fast_intent != Intent.GENERAL:
        return fast_intent

    if LLM_INTENT_ENABLED:
        return _detect_intent_llm(text)
    return _detect_intent_regex_full(text)
