"""סניטיזציה של PII בטקסט חופשי לפני שמירה / שליחה (תיקון 13).

משמש בעיקר ב-developer_reports — בעל העסק כותב תיאור באג בטקסט חופשי
שעלול לכלול PII של לקוחותיו (טלפון, אימייל). השכבה הזו היא
fail-safe — מחליפה דפוסים מזוהים ב-redaction tags לפני שהם מגיעים
ל-DB או ל-מייל למפתח.

כיסוי:
    - טלפון ישראלי: `+972...`, `05X-XXXXXXX`, `05X XXXXXXX`, `05XXXXXXXX`,
      ‏VoIP‏ `07X-XXXXXXX`, וקווי `0X-XXXXXXX`
    - מייל: דפוס סטנדרטי ב-RFC 5322 פשוט
    - לא מנסים שמות פרטיים — regex על שמות בעברית הוא false-positive farm.
      ל-UI hint מבקשים מהמשתמש לא לכתוב שמות.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

logger = logging.getLogger(__name__)

# טלפון ישראלי — 4 וריאנטים נפוצים. הסדר חשוב: תופס +972 לפני 05X
# כדי שלא נחתוך באמצע מספר בינלאומי.
_PHONE_PATTERNS = [
    # +972 / 00972 בינלאומי, עם אופציונלי מקף/רווח
    re.compile(r"\+972[\s-]?\d{1,2}[\s-]?\d{3}[\s-]?\d{4}"),
    re.compile(r"00972[\s-]?\d{1,2}[\s-]?\d{3}[\s-]?\d{4}"),
    # מקומי 05X-XXXXXXX או 05X XXXXXXX
    re.compile(r"\b05\d[\s-]?\d{3}[\s-]?\d{4}\b"),
    # 05XXXXXXXX רצוף (10 ספרות)
    re.compile(r"\b05\d{8}\b"),
    # VoIP / ספקים וירטואליים 07X-XXXXXXX (‏072/073/074/076/077/079).
    # הקידומת כאן היא בת **שלוש** ספרות והמספר בן 10 — בדיוק כמו סלולר,
    # ולא כמו קווי. לכן הרחבת מחלקת התווים של הקווי ל-[2-46-9] לא תופסת
    # אותם: שם התבנית מניחה קידומת בת שתי ספרות ומספר בן 9, ו-"073-1234567"
    # פשוט לא מתאים לאורך. נדרשת תבנית נפרדת.
    re.compile(r"\b07\d[\s-]?\d{3}[\s-]?\d{4}\b"),
    # קווי 0X-XXXXXXX — קידומת בת שתי ספרות, 9 ספרות בסך הכול.
    # ‏5 ו-7 מוחרגים (טופלו למעלה), ו-06 אינו קידומת ישראלית קיימת
    # (מוזגה ל-04 בשנות ה-90).
    re.compile(r"\b0[2-489][\s-]?\d{3}[\s-]?\d{4}\b"),
]

_EMAIL_PATTERN = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
)

# סודות — לצורך לוגים בלבד (`sanitize_for_log`). לא נכנס ל-`sanitize_pii`
# כי שם היעד הוא DB ומייל, ושם ההקשר שונה.
#   - `sk-...` / `sk-proj-...` — מפתחות OpenAI ותואמים
#   - טוקן בוט טלגרם — `<digits>:<35 תווים>`
#   - `Bearer <token>` בכותרות שמישהו הדפיס
_SECRET_PATTERN = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}"
    r"|\b\d{6,12}:[A-Za-z0-9_-]{30,}"
    r"|\bBearer\s+[A-Za-z0-9._-]{16,}",
)

PHONE_REDACTION = "[REDACTED_PHONE]"
EMAIL_REDACTION = "[REDACTED_EMAIL]"
SECRET_REDACTION = "[REDACTED_SECRET]"


class SanitationResult(NamedTuple):
    """תוצאת סניטיזציה: הטקסט הנקי + מספר ההחלפות לכל סוג."""
    text: str
    phones_redacted: int
    emails_redacted: int

    @property
    def changed(self) -> bool:
        return bool(self.phones_redacted or self.emails_redacted)


def sanitize_pii(text: str) -> SanitationResult:
    """מחליף דפוסי PII זוהים ב-redaction tags. מחזיר טקסט + מונים.

    שמרני: עדיף false-positive (להחליף משהו שדומה לטלפון אבל לא) על
    פני false-negative (לפספס מספר טלפון אמיתי שיגיע למפתח). אם משתמש
    כותב "יש לי 0501234567 בעיות" — זה ייחתך, וזה מקובל.
    """
    if not text:
        return SanitationResult(text="", phones_redacted=0, emails_redacted=0)

    phones_count = 0
    sanitized = text
    for pattern in _PHONE_PATTERNS:
        new_text, n = pattern.subn(PHONE_REDACTION, sanitized)
        if n:
            phones_count += n
            sanitized = new_text

    sanitized, emails_count = _EMAIL_PATTERN.subn(EMAIL_REDACTION, sanitized)

    return SanitationResult(
        text=sanitized,
        phones_redacted=phones_count,
        emails_redacted=emails_count,
    )


def sanitize_for_log(text: str) -> str:
    """‏PII **וסודות** מוסרים מטקסט שעומד להיכנס ללוג (‏T4.5).

    ההבדל מ-`sanitize_pii`: שם המטרה היא DB / מייל למפתח, ולכן די
    בטלפון ומייל. כאן היעד הוא קובץ לוג — שנשלח ל-Sentry, נאסף
    לשירות ריכוז, ולרוב שמור לאורך זמן בלי הצפנה — ולכן מוסרים גם
    מפתחות API וטוקנים. `sk-...` בלוג הוא סוד שדלף, גם אם הודפס
    "רק לדיבוג".

    ‏**זו לא הגנה שמותר להסתמך עליה**: הכלל הוא לא להעביר תוכן חופשי
    ללוג מלכתחילה (הטסט ב-`tests/test_observability.py` אוכף אותו).
    הפונקציה הזאת היא רשת ביטחון לנתיבים שבהם באמת חייבים להדפיס טקסט
    שמקורו חיצוני.
    """
    if not text:
        return ""
    sanitized = sanitize_pii(text).text
    return _SECRET_PATTERN.sub(SECRET_REDACTION, sanitized)


def has_pii_indicators(text: str) -> bool:
    """בדיקה מהירה: האם הטקסט מכיל דפוסים שדומים ל-PII?

    משמש ב-client-side warning (JS) כתחליף — זה ה-backend equivalent.
    מחזיר True אם יש שום דפוס; לא סופר.
    """
    if not text:
        return False
    for pattern in _PHONE_PATTERNS:
        if pattern.search(text):
            return True
    if _EMAIL_PATTERN.search(text):
        return True
    return False
