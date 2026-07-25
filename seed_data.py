"""
זריעת נתוני דמו — ‏tenant לדוגמה + בסיס ידע מייצג.

מטרת ה-seed היא שאדם שקיבל את הריפו יוכל להרים סביבה ולראות פאנל עם
תוכן אמיתי בלי להמציא נתונים. הרשומות כתובות כמו שבעל עסק היה כותב
אותן — משפטים מלאים, בלי כותרות מעוצבות — כי הן נשלחות ל-LLM כמו שהן.
"""

import logging

import database as db

logger = logging.getLogger(__name__)

DEMO_TENANT_ID = "demo"
DEMO_TENANT_NAME = "סלון דנה"

# (קטגוריה, כותרת, תוכן)
DEMO_KB_ENTRIES: list[tuple[str, str, str]] = [
    (
        "שעות",
        "שעות פעילות",
        "אנחנו פתוחים ראשון עד חמישי מ-9:00 עד 19:00, ושישי מ-9:00 עד 13:00.\n"
        "בשבת סגור. בערבי חג סוגרים ב-13:00.",
    ),
    (
        "מיקום",
        "כתובת והגעה",
        "הרצל 15, תל אביב, קומה 2 (יש מעלית).\n"
        "יש חניה חינם בחניון הסמוך למשך שעתיים — צריך לקחת כרטיס ולהחתים אצלנו.",
    ),
    (
        "מחירון",
        "מחירון תספורות וצבע",
        "תספורת נשים 120 ש\"ח, תספורת גברים 70 ש\"ח, תספורת ילדים 55 ש\"ח.\n"
        "צבע שורש מ-250 ש\"ח, צבע מלא מ-350 ש\"ח (תלוי באורך).\n"
        "פן 80 ש\"ח, פן מיוחד לאירוע 150 ש\"ח.\n"
        "המחירים כוללים מע\"מ.",
    ),
    (
        "מחירון",
        "טיפולי שיער",
        "החלקה אורגנית מ-700 ש\"ח, מסכת שיקום 120 ש\"ח.\n"
        "ייעוץ ראשוני לפני החלקה — ללא תשלום, כרבע שעה.",
    ),
    (
        "מדיניות",
        "ביטולים ואיחורים",
        "ביטול עד 24 שעות לפני התור — ללא חיוב.\n"
        "ביטול מאוחר יותר או אי-הגעה — חיוב של 50% מהטיפול.\n"
        "איחור של יותר מ-15 דקות עלול לחייב קיצור הטיפול או דחייה, כי התור הבא כבר קבוע.",
    ),
    (
        "שאלות נפוצות",
        "תשלום",
        "מקבלים מזומן, אשראי, ביט ופייבוקס. אין תשלומים.\n"
        "אפשר לקנות שובר מתנה בכל סכום.",
    ),
    (
        "שאלות נפוצות",
        "ילדים ונגישות",
        "אפשר להביא ילדים, יש פינת המתנה קטנה.\n"
        "המקום נגיש — יש מעלית מהכניסה ושירותי נכים בקומה.",
    ),
]


def seed_kb() -> int:
    """זריעת בסיס הידע לדמו. אידמפוטנטי — לא מכפיל רשומות קיימות.

    מחזיר כמה רשומות נוספו בפועל.
    """
    existing = {(e["category"], e["title"]) for e in db.get_all_kb_entries(active_only=False)}
    added = 0
    for category, title, content in DEMO_KB_ENTRIES:
        if (category, title) in existing:
            continue
        try:
            db.add_kb_entry(category, title, content)
            added += 1
        except Exception:
            # לולאת I/O — כשל ברשומה אחת לא עוצר את השאר
            logger.error("seed_kb: כשל בהוספת הרשומה '%s'", title, exc_info=True)
    logger.info("seed_kb: נוספו %d רשומות (מתוך %d)", added, len(DEMO_KB_ENTRIES))
    return added


def seed_demo_tenant() -> str:
    """יצירת tenant דמו (אם אינו קיים) וזריעת בסיס הידע שלו.

    מחזיר את מזהה ה-tenant. אידמפוטנטי — הרצה חוזרת לא נכשלת.
    """
    import control_plane as cp
    from tenancy import tenant_context

    if cp.get_tenant(DEMO_TENANT_ID) is None:
        cp.create_tenant(DEMO_TENANT_ID, DEMO_TENANT_NAME)
        logger.info("נוצר tenant דמו: %s", DEMO_TENANT_ID)

    with tenant_context(DEMO_TENANT_ID):
        db.init_db()
        seed_kb()
        # טון ברירת מחדל + משפט גישור בעברית טבעית
        db.update_bot_settings(
            tone="friendly",
            custom_phrases="נשמח לראותכם",
            handoff_bridge_message="בודק ואחזור אליך בהקדם",
        )
    return DEMO_TENANT_ID
