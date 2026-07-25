"""עזרי תאריך משותפים — פורמט תצוגה ישראלי.

תאריך בעברית מוצג תמיד יום/חודש/שנה (DD/MM/YYYY). המרה זו נדרשת בכל
מקום שמציג ללקוח תאריך שנשמר בפורמט ISO (YYYY-MM-DD) — למשל תאריך
חזרה ממצב חופשה.
"""

from __future__ import annotations

from datetime import datetime


def format_il_date(value: str) -> str:
    """המרת תאריך מפורמט ISO (``YYYY-MM-DD``) לפורמט ישראלי (``DD/MM/YYYY``).

    fail-safe: אם הערך ריק או אינו בפורמט הצפוי — מוחזר כמו שהוא (לא
    זורק חריגה). כך טקסט שכבר בפורמט אחר, או ערך לא-תקין, לא מפיל את
    ההודעה שבה הוא משובץ.
    """
    if not value:
        return ""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return value
