"""סניטיזציה של PII בטקסט חופשי לפני שמירה / שליחה (תיקון 13).

משמש בעיקר ב-developer_reports — בעל העסק כותב תיאור באג בטקסט חופשי
שעלול לכלול PII של לקוחותיו (טלפון, אימייל). השכבה הזו היא
fail-safe — מחליפה דפוסים מזוהים ב-redaction tags לפני שהם מגיעים
ל-DB או ל-מייל למפתח.

כיסוי:
    - טלפון ישראלי: `+972...`, `05X-XXXXXXX`, `05X XXXXXXX`, `05XXXXXXXX`
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
    # קווי 0X-XXXXXXX (X≠5 כדי לא לתפוס סלולר שכבר טופל)
    re.compile(r"\b0[2-489][\s-]?\d{3}[\s-]?\d{4}\b"),
]

_EMAIL_PATTERN = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
)

PHONE_REDACTION = "[REDACTED_PHONE]"
EMAIL_REDACTION = "[REDACTED_EMAIL]"


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
