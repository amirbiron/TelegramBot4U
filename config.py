"""
Config — טעינת הגדרות ממשתני סביבה, וזהות העסק פר-tenant.

מקוצץ מ-`ai-business-bot/config.py` לערוץ telegram_business (‏ROADMAP
T0.7). מה שיצא: ‏WhatsApp/Twilio/Meta, ‏widget, ‏RAG, תורים, שידורים.
מה שנוסף: ענף הערוץ ב-`build_system_prompt` (‏PLAN §4.4).

כלל מחייב (multi-tenant): **אסור** `from config import BUSINESS_NAME` —
הערך קופא ב-import ולא ניתן להחלפה פר-tenant. הזהות העסקית נקראת בזמן
ריצה דרך `get_business_config()`.
"""

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# ─── נתיבים ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
_DATA_DIR_DEFAULT = str(BASE_DIR / "data")

# קודם .env בשורש (הגדרות בסיסיות כמו DATA_DIR), ואז DATA_DIR/.env —
# קובץ אופציונלי על הדיסק הקבוע (Render), להגדרות שלא רוצים ב-repo.
#
# **בלי override**: משתני הסביבה של הפריסה גוברים על הקובץ. הריפו המקור
# השתמש ב-override=True כי הפאנל שלו כתב לקובץ הזה בזמן ריצה; כאן אין
# כותב כזה, ולכן ה-override היה נותן רק את הסיכון — סוד ישן שנשאר על
# הדיסק היה גובר על סוד מסובב שהוגדר ב-env, בלי שום סימן.
load_dotenv()
_persistent_env = Path(os.getenv("DATA_DIR", _DATA_DIR_DEFAULT)).resolve() / ".env"
if _persistent_env.exists():
    load_dotenv(_persistent_env)

DATA_DIR = Path(os.getenv("DATA_DIR", _DATA_DIR_DEFAULT)).resolve()
# ה-DB של ה-tenant של ברירת המחדל. ‏tenants אחרים: DATA_DIR/tenants/<slug>/
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "chatbot.db"))).resolve()

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ─── טלגרם ───────────────────────────────────────────────────────────────
# הבוט המנהל (Bot Management Mode) — שלב 2.
MANAGER_BOT_TOKEN = os.getenv("MANAGER_BOT_TOKEN", "")
MANAGER_BOT_USERNAME = os.getenv("MANAGER_BOT_USERNAME", "")
MANAGER_WEBHOOK_SECRET = os.getenv("MANAGER_WEBHOOK_SECRET", "")
# הבוט הידני של שלב 1 (‏tenant יחיד, ‏Secretary Mode הודלק ב-BotFather).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# בסיס ה-URL הציבורי לרישום webhooks (בלי / בסוף)
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")

# ארבעת סוגי עדכוני ה-Business + message (פקודות הבעלים בצ'אט הבן).
# שכחת אחד מהם = דממה שקטה בלי שגיאה (CLAUDE.md — כללי הערוץ).
BUSINESS_ALLOWED_UPDATES = [
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "message",
]
# הבוט המנהל מאזין ליצירת בוטים-בנים
MANAGER_ALLOWED_UPDATES = ["message", "managed_bot"]

# ─── LLM ─────────────────────────────────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
# כבוי כברירת מחדל: בערוץ הזה הכוונה משמשת לתיוג בלבד (רישום פערי ידע,
# סטטיסטיקה) — כל הודעה עוברת דרך אותו צינור LLM ממילא. קריאת LLM שנייה
# בכל הודעה מכפילה עלות ולטנסי עבור תווית. ראה intent.py.
LLM_INTENT_ENABLED = os.getenv("LLM_INTENT_ENABLED", "false").lower() in ("true", "1", "yes")
INTENT_MODEL = os.getenv("INTENT_MODEL", "gpt-4.1-nano")

# ─── שיחה ────────────────────────────────────────────────────────────────
CONTEXT_WINDOW_SIZE = int(os.getenv("CONTEXT_WINDOW_SIZE", "10"))
SUMMARY_THRESHOLD = int(os.getenv("SUMMARY_THRESHOLD", "10"))
# סף אזהרה לגודל בסיס הידע (טוקנים) — מעליו הפאנל מציג "שקול פיצול"
KB_TOKEN_WARN_THRESHOLD = int(os.getenv("KB_TOKEN_WARN_THRESHOLD", "50000"))
# ‏timeout ההשתקה: אחרי כמה דקות בלי הודעת בעלים הבוט חוזר לענות
TAKEOVER_TIMEOUT_MINUTES = int(os.getenv("TAKEOVER_TIMEOUT_MINUTES", "120"))

# ─── זיכרון לקוחות ───────────────────────────────────────────────────────
BUSINESS_ID = os.getenv("BUSINESS_ID", "default")
MEMORY_INJECTION_ENABLED = os.getenv("MEMORY_INJECTION_ENABLED", "true").lower() in (
    "true", "1", "yes",
)
MEMORY_STALENESS_DAYS = int(os.getenv("MEMORY_STALENESS_DAYS", "90"))

# ─── מגבלות קצב ──────────────────────────────────────────────────────────
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "50"))
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "100"))

# ─── פאנל אדמין ──────────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "")
ADMIN_HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
ADMIN_PORT = int(os.getenv("ADMIN_PORT") or os.getenv("PORT") or "5000")
# עוגיית הסשן נשלחת רק על HTTPS. לכבות רק לפיתוח מקומי על http://.
ADMIN_COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "true").lower() in (
    "true", "1", "yes",
)

# ─── דגלי פיצ'רים ────────────────────────────────────────────────────────
# הזרמה "אנושית" של התשובה (sendMessageDraft). כבוי כברירת מחדל — ראה
# משימת האימות V4 ב-docs/verification_log.md.
HUMANIZED_DELIVERY = os.getenv("HUMANIZED_DELIVERY", "false").lower() in ("true", "1", "yes")

# ─── זהות עסקית (ברירת מחדל ל-tenant ה-legacy) ───────────────────────────
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "העסק")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "")
BUSINESS_ADDRESS = os.getenv("BUSINESS_ADDRESS", "")
BUSINESS_WEBSITE = os.getenv("BUSINESS_WEBSITE", "")


@dataclass(frozen=True)
class BusinessConfig:
    """הזהות העסקית — שם, טלפון, כתובת, אתר."""

    name: str
    phone: str
    address: str
    website: str


# tenants שכבר קיבלו אזהרת fallback — כדי לא להציף את הלוג בקריאה שחוזרת
# בכל הודעה (state מודולרי ממופתח לפי tenant, כנדרש).
_identity_fallback_warned: set = set()


def _warn_identity_fallback_once(exc: Exception) -> None:
    """לוג אזהרה פעם אחת פר-tenant כשקריאת הזהות מה-DB נכשלה (בלי PII)."""
    try:
        from tenancy import get_current_tenant

        key = get_current_tenant()
    except Exception:
        key = "(no-context)"
    if key in _identity_fallback_warned:
        return
    _identity_fallback_warned.add(key)
    logging.getLogger(__name__).warning(
        "get_business_config: קריאת הזהות מה-DB נכשלה (tenant=%s) — fallback ל-env: %s",
        key, exc.__class__.__name__,
    )


def get_business_config() -> BusinessConfig:
    """הזהות העסקית — **לקרוא בזמן-ריצה, לא לייבא כקבועים**.

    שם העסק: מקור אמת יחיד הוא `display_name` שנקבע בהקמת ה-tenant
    (control plane) — אין שדה שם נפרד לעריכה, כדי שלא ייווצר עותק שקופא.
    כרטיס הביקור (טלפון/כתובת/אתר): מ-`bot_settings` של ה-tenant.
    שדה ריק, או כשל בקריאת ה-DB (‏startup מוקדם, טסטים בלי סכימה) —
    נופל ל-env, כך שה-tenant של ברירת המחדל ממשיך לעבוד.
    """
    _mod = sys.modules[__name__]

    phone = address = website = ""
    try:
        # ייבוא עצל — config נטען לפני database, אסור מעגל ייבוא
        import database as _db

        row = _db.get_bot_settings() or {}
        phone = (row.get("business_phone") or "").strip()
        address = (row.get("business_address") or "").strip()
        website = (row.get("business_website") or "").strip()
    except Exception as exc:
        _warn_identity_fallback_once(exc)

    name = ""
    try:
        from tenancy import DEFAULT_TENANT, get_current_tenant

        tenant = get_current_tenant()
        if tenant != DEFAULT_TENANT:
            import control_plane as _cp

            trow = _cp.get_tenant(tenant)
            if trow:
                name = (trow.get("display_name") or "").strip()
    except Exception as exc:
        _warn_identity_fallback_once(exc)

    return BusinessConfig(
        name=name or _mod.BUSINESS_NAME,
        phone=phone or _mod.BUSINESS_PHONE,
        address=address or _mod.BUSINESS_ADDRESS,
        website=website or _mod.BUSINESS_WEBSITE,
    )


# ─── פרומפט המערכת (שכבה A) ──────────────────────────────────────────────

TONE_PROFILES: dict[str, dict[str, str]] = {
    "none": {
        "label": "ללא בחירה",
        "definition": "",
        "guidelines": (
            "- כתוב כמו אדם שמנהל התכתבות אישית: משפטים קצרים, בלי ז'רגון.\n"
            "- כשיש כמה פריטים — שורה לכל פריט, בלי כותרות מעוצבות."
        ),
    },
    "friendly": {
        "label": "ידידותי",
        "definition": (
            "כתוב בטון חם, חברי וקליל — כמו בעל עסק קטן שעונה ללקוח שהוא מכיר. "
            "אימוג'י בודד מותר כשהוא טבעי, לא בכל הודעה."
        ),
        "guidelines": (
            "- שפה טבעית ושיחתית. הימנע מניסוחים רובוטיים כמו \"במה אוכל לסייע לך היום?\".\n"
            "- אמפתיה תחילה: לקוח מתוסכל — קודם מכירים בתסכול, אחר כך עונים.\n"
            "- כשיש כמה פריטים — שורה לכל פריט, בלי כותרות מעוצבות."
        ),
    },
    "formal": {
        "label": "רשמי",
        "definition": (
            "כתוב בטון מנומס ועסקי. בלי סלנג ובלי אימוג'ים, אבל גם בלי נוקשות מיותרת."
        ),
        "guidelines": (
            "- עברית תקינה ומכבדת, משפטים קצרים.\n"
            "- אמפתיה מקצועית: מכירים בפנייה, ואז עונים לגופה.\n"
            "- כשיש כמה פריטים — שורה לכל פריט, בלי כותרות מעוצבות."
        ),
    },
    "sales": {
        "label": "מכירתי",
        "definition": (
            "כתוב בטון חיובי ומזמין, וכשזה טבעי — הצע את הצעד הבא (לתאם, להתנסות, לבוא)."
        ),
        "guidelines": (
            "- שפה מזמינה, בלי לחץ ובלי סיסמאות שיווקיות.\n"
            "- אמפתיה תחילה, ורק אז ההצעה.\n"
            "- כשיש כמה פריטים — שורה לכל פריט, בלי כותרות מעוצבות."
        ),
    },
    "luxury": {
        "label": "יוקרתי",
        "definition": (
            "כתוב בטון מעודן ומדוד. בלי סימני קריאה מרובים ובלי אימוג'ים; "
            "ביטויים כמו \"בוודאי\", \"בשמחה\"."
        ),
        "guidelines": (
            "- עברית מלוטשת, קצרה ומדויקת.\n"
            "- אמפתיה עדינה ומכבדת.\n"
            "- כשיש כמה פריטים — שורה לכל פריט, בלי כותרות מעוצבות."
        ),
    },
}

TONE_DEFINITIONS: dict[str, str] = {k: v["definition"] for k, v in TONE_PROFILES.items()}
TONE_LABELS: dict[str, str] = {k: v["label"] for k, v in TONE_PROFILES.items()}

# תווים מותרים בביטויים מותאמים אישית — אותיות (כל שפה), ספרות, רווחים,
# פיסוק בסיסי ותווים עסקיים. חוסם תווים שעלולים לשמש ל-prompt injection
# (מפרידי סקשנים ──, ‏en/em-dash שמודלים מפרשים כמפריד).
_CUSTOM_PHRASES_PATTERN = re.compile(
    r"[^\w\s֐-׿؀-ۿ.,!?;:'\"\-()•·\n%₪$€/+#&@]",
    re.UNICODE,
)
_CUSTOM_PHRASES_MAX_LENGTH = 500


def _sanitize_custom_phrases(text: str) -> str:
    """סניטציה של ביטויים מותאמים אישית — מסיר תווים חשודים ומגביל אורך."""
    cleaned = _CUSTOM_PHRASES_PATTERN.sub("", text).strip()
    if len(cleaned) > _CUSTOM_PHRASES_MAX_LENGTH:
        cleaned = cleaned[:_CUSTOM_PHRASES_MAX_LENGTH].rsplit(" ", 1)[0]
    return cleaned


# מחרוזות עוגן לבדיקת סדר הבלוקים בפרומפט (‏prompt caching, ‏ROADMAP כלל 8).
# ‏llm.py מרכיב את ההודעה לפי הסדר הזה; טסט אוכף אותו.
ANCHOR_PERSONA = "── מי אתה ──"
ANCHOR_KB = "── בסיס הידע של העסק ──"
ANCHOR_TENANT_SETTINGS = "── הנחיות העסק ──"
ANCHOR_MEMORY = "── מה שאתה יודע על הלקוח ──"
ANCHOR_SUMMARY = "── סיכום שיחה קודמת ──"


def _build_formatting_rules(channel: str) -> str:
    """הנחיות עיצוב טקסט לפי ערוץ.

    בערוץ telegram_business הלקוח חושב שהוא מדבר עם אדם — ולכן כל עיצוב
    "של בוט" מסגיר: כותרות, טבלאות, אימוג'י-כותרות, ובוודאי הפניות
    לכפתורים (שאינם קיימים בערוץ הזה בכלל).
    """
    if channel == "telegram_business":
        return (
            "חוק ברזל: טקסט רגיל בלבד, כמו הודעה שאדם מקליד.\n"
            "- אסור תגי HTML, אסור Markdown (כוכביות, קווים תחתונים, סולמיות, backticks).\n"
            "- אסור כותרות מעוצבות, אסור טבלאות, אסור אימוג'י בתחילת שורה ככותרת.\n"
            "- תשובות קצרות — 1-4 משפטים ברוב המקרים. זו התכתבות אישית, לא דף שירות.\n"
            "- כשיש כמה פריטים (מחירים, שירותים) — שורה לכל פריט, בלי מספור מעוצב.\n"
            "דוגמה נכונה: תספורת 99 ש\"ח, צבע מ-250. מתאים לך מחר בבוקר?\n"
            "דוגמה שגויה: **מחירון:**\\n1. תספורת — <b>99 ש\"ח</b>"
        )
    # ברירת מחדל — טקסט נקי (אין ערוצים אחרים בפרויקט הזה)
    return "חוק ברזל: כתוב בטקסט רגיל בלבד, בלי HTML ובלי Markdown."


def _build_channel_rules(channel: str) -> str:
    """כללי הערוץ — handoff ושקיפות.

    מנגנון "טוקן סמן": ה-LLM פותח את התשובה ב-HANDOFF_MARKER. הפרסר
    ב-`core/message_processor.py` בודק `startswith` בלבד — דטרמיניסטי,
    בלי fuzzy matching שיוצר false positives.
    """
    handoff_rule = (
        f"5. כשאין לך מספיק מידע לענות, או כשהלקוח מבקש במפורש לדבר עם בעל/ת "
        f"העסק — התחל את התשובה בדיוק במחרוזת {HANDOFF_MARKER} (כולל הסוגריים "
        f"המרובעים, ללא רווח לפניה, בשורה משלה), ואז שורה ריקה, ואז משפט גישור "
        f"קצר אחד בלבד.\n"
        f"   חשוב: אל תכתוב {HANDOFF_MARKER} בשום מצב אחר, ואל תסביר ללקוח שאתה "
        f"מעביר אותו לגורם אחר — פשוט אמור שתבדוק ותחזור אליו."
    )
    if channel == "telegram_business":
        return (
            "1. ענה רק על סמך המידע שסופק לך למעלה. לעולם אל תמציא מחיר, שעה, "
            "תאריך או מדיניות.\n"
            "2. אל תפנה את הלקוח לכפתורים, לתפריטים או לפקודות — אין דבר כזה "
            "בצ'אט הזה, והוא לא רואה שום ממשק.\n"
            "3. אל תציין מקורות, קטגוריות או שמות רשומות מבסיס הידע. הלקוח לא "
            "אמור לדעת שיש דבר כזה.\n"
            "4. ענה באותה שפה שבה הלקוח פנה.\n"
            f"{handoff_rule}\n"
            "6. שקיפות (חובה): אם הלקוח שואל ישירות אם הוא מדבר עם אדם, עם בוט, "
            "או אם ההודעה אוטומטית — אשר שזו מענה אוטומטי מטעם העסק, בקצרה "
            "ובלי התנצלות, והמשך לעזור. **אסור להכחיש**. אל תעלה את הנושא "
            "מיוזמתך."
        )
    return handoff_rule


def build_system_prompt(
    channel: str = "telegram_business",
    business_name: str = "",
) -> str:
    """שכבה A — הפרסונה וכללי הערוץ. **הבלוק הכי יציב בפרומפט.**

    business_name — כשריק נשלף דרך `get_business_config()` (הזהות של
    ה-tenant הנוכחי), כדי שהפרומפט לא ייתלה בקבוע גלובלי.

    הבלוק הזה הוא ה-prefix של ההודעה ל-prompt caching, ולכן הוא מכיל רק
    מה שכמעט לא משתנה. הטון, הביטויים וההנחיות של בעל העסק יושבים בבלוק
    נפרד **אחרי** בסיס הידע — ראה `build_tenant_settings_block` ואת סדר
    הבלוקים ב-`llm._build_messages` (‏PLAN §3.3).
    """
    business_name = business_name or get_business_config().name

    memory_usage_section = ""
    if MEMORY_INJECTION_ENABLED:
        memory_usage_section = (
            "\n\n── שימוש במידע על הלקוח ──\n"
            "אם תקבל בלוק עם מה שידוע על הלקוח — השתמש בו בטבעיות, בלי להכריז "
            "עליו (אל תכתוב \"ראיתי שרשום אצלי ש...\", פשוט התחשב).\n"
            "פריט המסומן \"מידע רגיש\" — היה דיסקרטי. פריט המסומן "
            "\"ייתכן שלא רלוונטי\" — אל תניח שהוא עדיין נכון; אם זה חשוב לשיחה, "
            "שאל בעדינות."
        )

    return f"""{ANCHOR_PERSONA}
אתה העוזר האישי שמנהל את התכתובות של {business_name}.
אתה עונה ללקוחות מטעם בעל/ת העסק, בצ'אט אישי בטלגרם, בגוף ראשון.
הלקוח כותב הודעה פרטית — לא פונה ל"שירות לקוחות". תכתוב כמו אדם: קצר, ענייני וחם.

── עיצוב טקסט (חובה!) ──
{_build_formatting_rules(channel)}

── כללים — יש לעקוב אחריהם בקפידה ──
{_build_channel_rules(channel)}

── מה לא לעשות ──
- אל תכתוב הודעות מערכת ("אנא המתן", "פנייתך התקבלה במערכת", "מספר פנייה").
- אל תבטיח מה שלא כתוב בבסיס הידע.
- אל תשאל את הלקוח שאלות שכבר נענו בשיחה.{memory_usage_section}"""


def build_tenant_settings_block(
    tone: str = "friendly",
    custom_phrases: str = "",
    custom_prompt: str = "",
) -> str:
    """הגדרות ה-tenant — טון, ביטויים אופייניים והנחיות בעל העסק.

    מוזרק **אחרי** בסיס הידע (‏PLAN §3.3): אלה הגדרות שבעל העסק עורך
    בפאנל, ולכן הן פחות יציבות מהפרסונה. מחזיר '' כשאין מה להזריק — לא
    מזריקים כותרת ריקה.
    """
    effective_tone = tone if tone in TONE_PROFILES else "friendly"
    profile = TONE_PROFILES[effective_tone]

    parts: list[str] = []
    if profile["definition"].strip():
        parts.append(f"טון כתיבה:\n{profile['definition']}")
    if profile["guidelines"].strip():
        parts.append(f"איך לכתוב:\n{profile['guidelines']}")
    if custom_phrases and custom_phrases.strip():
        safe_phrases = _sanitize_custom_phrases(custom_phrases)
        if safe_phrases:
            parts.append(
                "ביטויים אופייניים לעסק (השתמש בהם באופן טבעי):\n" + safe_phrases
            )
    if custom_prompt and custom_prompt.strip():
        # ללא סניטציה — המקור הוא בעל העסק דרך פאנל מאומת, לא משתמש קצה
        parts.append("הנחיות מבעל העסק:\n" + custom_prompt.strip())

    if not parts:
        return ""
    return f"{ANCHOR_TENANT_SETTINGS}\n" + "\n\n".join(parts)


# ─── טוקנים וטקסטים קבועים ───────────────────────────────────────────────

# טוקן סמן שה-LLM שם בתחילת תשובה כדי לסמן "אני מבקש להעביר לבעלים".
# שימוש בטוקן (במקום fuzzy text matching) הופך את הזיהוי לדטרמיניסטי.
HANDOFF_MARKER = "[HANDOFF]"

# ה-fallback שנשלח ללקוח כשקריאת ה-LLM עצמה נכשלה. משפט גישור אנושי —
# בלי "המערכת", בלי "שגיאה", בלי מספר פנייה.
FALLBACK_RESPONSE = "אני בודק ואחזור אליך בהקדם"

# ציטוט מקור — לא מבקשים אותו בערוץ הזה, אבל משאירים strip הגנתי ב-llm.py
# למקרה שהמודל מוסיף אותו מיוזמתו (הוא מסגיר אוטומציה).
SOURCE_CITATION_PATTERN = r"([Ss]ource|מקור):\s*.+"


def validate_config(*, require_bot: bool = False, require_admin: bool = False) -> list[str]:
    """בדיקת תקינות משתני סביבה קריטיים. מחזיר רשימת שגיאות (ריקה = תקין)."""
    errors: list[str] = []
    if require_bot:
        if not TELEGRAM_BOT_TOKEN and not MANAGER_BOT_TOKEN:
            errors.append(
                "לא הוגדר אף טוקן בוט (TELEGRAM_BOT_TOKEN לשלב 1 / "
                "MANAGER_BOT_TOKEN לשלב 2) — אין ממה לקבל עדכונים"
            )
        if not WEBHOOK_BASE_URL:
            errors.append("WEBHOOK_BASE_URL לא מוגדר — לא ניתן לרשום webhook")
    if require_admin:
        if not ADMIN_PASSWORD and not ADMIN_PASSWORD_HASH:
            errors.append(
                "ADMIN_PASSWORD / ADMIN_PASSWORD_HASH לא מוגדרים — "
                "לא ניתן להתחבר לפאנל"
            )
        if not ADMIN_SECRET_KEY:
            errors.append("ADMIN_SECRET_KEY לא מוגדר — sessions ו-CSRF לא מאובטחים")
    from utils.crypto import is_encryption_configured

    if not is_encryption_configured():
        errors.append(
            "SECRETS_ENCRYPTION_KEY לא מוגדר — כתיבת סודות פלטפורמה חסומה "
            "(fail-closed)"
        )
    return errors
