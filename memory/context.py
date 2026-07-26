"""
Customer Memory — שלב 8: הזרקת facts ל-context של הבוט.

שתי פונקציות ציבוריות:
- get_relevant_facts_for_context: שולף active facts של משתמש (filtered
  לפי vocabulary substring match), ממיין, cap 10, ומעלה access_count.
- format_facts_block: בונה את הבלוק הטקסטואלי שמוזרק ל-system message.

נקרא מ-`llm.py:_build_messages`. ה-toggle MEMORY_INJECTION_ENABLED
ב-config.py מאפשר כיבוי ההזרקה בלי להפסיק את החילוץ.

הועתק מ-`ai-business-bot/memory/context.py` עם נרמול imports.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import database as db
from config import MEMORY_STALENESS_DAYS

logger = logging.getLogger(__name__)

# שעון ישראל — אותו tz כמו business_hours.py ו-admin/app.py:_format_il_datetime.
# DB מאחסן UTC (SQLite datetime('now')); כל ההצגה ללקוח/לבוט בשעון ישראל.
_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def now_israel() -> datetime:
    """תאריך/שעה נוכחיים בשעון ישראל (aware datetime). public כדי שטסטים
    ו-llm.py יוכלו להשתמש באותה פונקציה במקום datetime.now() נאיבי."""
    return datetime.now(_ISRAEL_TZ)


def format_current_date_il() -> str:
    """תאריך נוכחי בפורמט DD/MM/YYYY בשעון ישראל — לראש בלוק ה-facts."""
    return now_israel().strftime("%d/%m/%Y")

# המקסימום שמוזרק ל-prompt. גבול חצי-שרירותי שמונע תפיחה של ה-system
# message כשמשתמש צובר עשרות facts.
_FACTS_CAP = 10

# סוגי facts שעוברים תמיד (אם status='active'). vocabulary מסונן בנפרד.
_ALWAYS_INCLUDED_TYPES = {
    "preference", "personal_info", "relationship", "open_issue",
}


def _parse_db_dt(s: Optional[str]) -> Optional[datetime]:
    """ממיר 'YYYY-MM-DD HH:MM:SS' (UTC מה-DB) ל-datetime aware בשעון ישראל.

    SQLite `datetime('now')` מחזיר UTC כ-naive string. ההצגה ללקוח/לבוט
    בשעון ישראל — אותו עיקרון כמו admin/app.py:_format_il_datetime.
    None אם ריק/לא תקין.
    """
    if not s:
        return None
    try:
        naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=timezone.utc).astimezone(_ISRAEL_TZ)


def _format_il_date(dt: Optional[datetime]) -> str:
    """datetime → 'DD/MM/YYYY'. ריק אם dt is None."""
    return dt.strftime("%d/%m/%Y") if dt else ""


def _bump_access_count(fact_ids: list[int]) -> None:
    """UPDATE batch אחד: access_count++ לכל id ברשימה.

    bookkeeping — אם נכשל, רושמים שגיאה אבל לא מקריסים את ההזרקה
    (CLAUDE.md — Exceptions תמיד עם logger.error). ביצוע ע"י db
    בכוונה כדי לעבור דרך get_connection() עם WAL + busy_timeout.
    """
    if not fact_ids:
        return
    try:
        with db.get_connection() as conn:
            placeholders = ",".join("?" * len(fact_ids))
            conn.execute(
                f"UPDATE customer_facts SET access_count = access_count + 1 "
                f"WHERE id IN ({placeholders})",
                tuple(fact_ids),
            )
    except Exception:
        logger.error(
            "context: כשל בעדכון access_count עבור %d facts",
            len(fact_ids), exc_info=True,
        )


def get_relevant_facts_for_context(
    user_id: str,
    business_id: str,
    current_message: Optional[str] = None,
) -> list[dict]:
    """שולף facts active של משתמש להזרקה ל-context.

    כללי סינון:
    - status='active' בלבד (לא resolved/superseded/rejected/pending_approval).
    - preference/personal_info/relationship/open_issue — תמיד נכללים.
    - vocabulary — רק אם current_message מכיל את ה-content (case-insensitive
      substring). חוסך זיהום של ה-prompt בכינויים שלא קשורים להודעה.

    מיון: confidence DESC, last_confirmed_at DESC, id DESC (tiebreaker
    יציב — CLAUDE.md דורש tiebreaker לכל ORDER BY שמוגבל ב-LIMIT).

    Cap: 10 facts. אחרי הסינון — מעלה access_count++ לכל fact שנכלל.
    """
    actives = db.get_customer_facts(user_id, business_id, status="active")

    # tiebreaker יציב — ה-CRUD ממיין לפי confidence/last_confirmed_at,
    # אבל לא לפי id ב-DB. מסדרים פה ידנית למניעת flakiness כש-2 facts
    # זהים בערכי המיון הראשוניים.
    # שלושת המפתחות DESC: confidence (עם -), last_confirmed_at (string
    # YYYY-MM-DD HH:MM:SS — מיון לקסיקלי שווה לכרונולוגי; הופכים עם
    # reverse=True במקום מינוס שלא חוקי על strings), id (עם -).
    # נשים לב: עבור strings אסור להשתמש במינוס; אם נשתמש ב-reverse=True
    # זה ייהפך גם את כיוון ה-confidence/id. הפתרון: מפתח שילובי שמיישם
    # את כל ה-DESC ידנית — confidence/id מנוסחים כשליליים, ו-
    # last_confirmed_at מוחלף ל"שלילי" ע"י הקדמת תו max-unicode (פתרון
    # פשוט: לבצע sort בשני שלבים יציבים).
    # שיטה פשוטה ובטוחה: שני sorts יציבים (Python sort הוא stable).
    actives.sort(key=lambda f: -int(f.get("id") or 0))                      # tertiary
    actives.sort(key=lambda f: f.get("last_confirmed_at") or "",
                 reverse=True)                                              # secondary DESC
    actives.sort(key=lambda f: -float(f.get("confidence") or 0))            # primary

    msg_lower = (current_message or "").lower()
    selected: list[dict] = []
    for f in actives:
        if len(selected) >= _FACTS_CAP:
            break
        fact_type = f.get("fact_type")
        if fact_type in _ALWAYS_INCLUDED_TYPES:
            selected.append(f)
        elif fact_type == "vocabulary":
            content = (f.get("content") or "").lower().strip()
            if content and msg_lower and content in msg_lower:
                selected.append(f)
        # סוגים לא מוכרים — מתעלמים (forward-compat).

    if selected:
        _bump_access_count([int(f["id"]) for f in selected])

    return selected


def format_facts_block(
    facts: list[dict], current_date: str,
) -> Optional[str]:
    """ממיר רשימת facts לבלוק טקסט שמוזרק ל-system message.

    None אם facts ריק — נמנע מהזרקת בלוק ריק/מטעה.

    פורמט:
        תאריך נוכחי: DD/MM/YYYY

        מה שאתה יודע על הלקוח:
        - {content} ({tags})

    tags נבנים מ:
    - "מידע רגיש" (אם requires_consent — תמיד ראשון).
    - "נאמר {created_at}".
    - "אומת שוב {last_confirmed_at}" (רק אם הפרש >= יום מ-created_at).
    """
    if not facts:
        return None

    # נקודת זמן אחת לכל הקריאה — staleness מחושב מולה ל-deterministic.
    now = now_israel()

    # בונים את ה-bullets קודם — אם כולם נדחו (content ריק), מחזירים None
    # ולא בלוק עם header בלבד (CLAUDE.md / spec: אין מה להזריק → לא להזריק).
    bullets: list[str] = []
    for f in facts:
        content = (f.get("content") or "").strip()
        if not content:
            continue
        tags: list[str] = []
        if f.get("requires_consent"):
            tags.append("מידע רגיש")

        created_dt = _parse_db_dt(f.get("created_at"))
        confirmed_dt = _parse_db_dt(f.get("last_confirmed_at"))
        if created_dt:
            tags.append(f"נאמר {_format_il_date(created_dt)}")
        if created_dt and confirmed_dt:
            # "אומת שוב" רק אם ההפרש גדול מיום — אחרת זה רק רעש (created_at
            # ו-last_confirmed_at מתחילים זהים, רק confirm משנה אותו).
            if abs((confirmed_dt - created_dt).days) >= 1:
                tags.append(f"אומת שוב {_format_il_date(confirmed_dt)}")

        # Staleness flag — fact שלא נאמר/אומת מעבר ל-MEMORY_STALENESS_DAYS
        # מסומן כדי שהבוט יידע לטפל בו בזהירות (לפי הוראות
        # build_system_prompt: "אל תניח שעדיין נכון; שאל אם רלוונטי").
        # reference = last_confirmed_at אם קיים, אחרת created_at.
        reference_dt = confirmed_dt or created_dt
        if reference_dt and (now - reference_dt).days > MEMORY_STALENESS_DAYS:
            tags.append("ייתכן שלא רלוונטי")

        if tags:
            bullets.append(f"- {content} ({', '.join(tags)})")
        else:
            bullets.append(f"- {content}")

    if not bullets:
        return None

    return "\n".join([
        f"תאריך נוכחי: {current_date}",
        "",
        "מה שאתה יודע על הלקוח:",
        *bullets,
    ])
