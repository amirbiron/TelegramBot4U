"""
kb_service — התפר היחיד בין בסיס הידע לבין הפרומפט.

מחליף את צינור ה-RAG של הריפו המקור (‏PLAN §3.2). ההחלטה: בסיס הידע של
עסק קטן הוא 20–100 רשומות, ריאלית עד ~30K טוקנים — נכנס בשלמותו לחלון
ההקשר. ‏retrieval היה מנגנון דחיסה לעידן של חלונות 8K, והוא נכשל בדיוק
בשאלות שדורשות שתי רשומות ("יש חניה? ואתם פתוחים בשישי?").

**החוזה קבוע:** כל קורא מקבל את ההקשר דרך `get_kb_context()` בלבד. אם
יום אחד tenant יחצה את הסף, מימוש retrieval ייכנס **מאחורי הפונקציה
הזאת** בלי לגעת באף קורא — לכן היא כבר מקבלת `top_hint` (שאילתת הלקוח)
שכרגע אינו בשימוש. אסור לממש retrieval בלי דיון מפורש (ROADMAP כלל 6).

‏cache: פר-tenant, ומפתחו הוא חתימת הגרסה של בסיס הידע
(`database.get_kb_version()` — ‏COUNT + ‏MAX(updated_at)). שאילתה אחת זולה
בכל הודעה, ועריכה בפאנל נכנסת לתוקף בהודעה הבאה בלי rebuild.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ‏cache פר-tenant: tenant → (kb_version, KBContext).
# ‏state ברמת מודול חייב מפתח tenant מהיום הראשון (CLAUDE.md).
_cache: dict[str, tuple[str, "KBContext"]] = {}
_cache_lock = threading.Lock()

# אומדן טוקנים כשאין tiktoken: עברית ב-UTF-8 יוצאת סביב 3 תווים לטוקן
# ברוב ה-tokenizers המודרניים. אומדן גס בכוונה — הוא משמש לאזהרה בפאנל,
# לא לחישוב עלות.
_CHARS_PER_TOKEN_FALLBACK = 3

_encoder = None
_encoder_lock = threading.Lock()
_encoder_unavailable = False


@dataclass(frozen=True)
class KBContext:
    """ההקשר המלא של בסיס הידע + מטא לתצוגה בפאנל.

    text — הטקסט שמוזרק לפרומפט (ריק כשאין רשומות פעילות).
    entry_count — כמה רשומות פעילות נכללו.
    token_estimate — אומדן טוקנים (tiktoken אם מותקן, אחרת len//3).
    is_over_threshold — האם חצינו את KB_TOKEN_WARN_THRESHOLD.
    version — חתימת הגרסה ששימשה כמפתח ה-cache (לדיבוג ולטסטים).
    """

    text: str
    entry_count: int
    token_estimate: int
    is_over_threshold: bool
    version: str

    @property
    def is_empty(self) -> bool:
        return self.entry_count == 0


def _estimate_tokens(text: str) -> int:
    """אומדן טוקנים. ‏tiktoken כשזמין, אחרת חלוקת תווים.

    ה-encoder נטען פעם אחת ונשמר; כישלון טעינה (חבילה חסרה, אין רשת
    להורדת ה-vocab) מוריד את הפיצ'ר ל-fallback ולא מפיל את הזרימה
    (דפוס אוניברסלי #3 — אתחול SDK לא מקריס boot).
    """
    if not text:
        return 0
    global _encoder, _encoder_unavailable
    if _encoder is None and not _encoder_unavailable:
        with _encoder_lock:
            if _encoder is None and not _encoder_unavailable:
                try:
                    import tiktoken

                    _encoder = tiktoken.get_encoding("cl100k_base")
                except Exception as exc:
                    _encoder_unavailable = True
                    logger.warning(
                        "kb_service: tiktoken לא זמין (%s) — אומדן טוקנים לפי אורך",
                        type(exc).__name__,
                    )
    if _encoder is not None:
        try:
            return len(_encoder.encode(text))
        except Exception:
            logger.error("kb_service: כשל בקידוד tiktoken — נופלים לאומדן", exc_info=True)
    return max(1, len(text) // _CHARS_PER_TOKEN_FALLBACK)


def _format_entries(entries: list[dict]) -> str:
    """הרכבת הטקסט: מקובץ לפי קטגוריה, רשומה-רשומה.

    הפורמט זהה במבנהו ל-`format_context` של הריפו המקור, כדי שהתנהגות
    המודל לא תשתנה בבת אחת:
        --- {קטגוריה} — {כותרת} ---
        {תוכן}
    """
    parts: list[str] = []
    current_category: Optional[str] = None
    for entry in entries:
        category = (entry.get("category") or "").strip()
        title = (entry.get("title") or "").strip()
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        if category != current_category:
            current_category = category
            parts.append(f"\n## {category}" if category else "\n## כללי")
        parts.append(f"--- {category} — {title} ---\n{content}")
    return "\n\n".join(p.strip("\n") for p in parts).strip()


def get_kb_context(top_hint: str | None = None) -> KBContext:
    """ההקשר המלא של בסיס הידע של ה-tenant הנוכחי.

    top_hint — שאילתת הלקוח. **אינו בשימוש כרגע** (אין retrieval); הוא
    חלק מהחוזה כדי שמימוש retrieval עתידי ייכנס כאן בלי לשנות קוראים.
    """
    import config as _config
    import database as db
    from tenancy import get_current_tenant

    tenant = get_current_tenant()
    try:
        version = db.get_kb_version()
    except Exception:
        # אין סכימה / DB לא זמין — לא מפילים את הזרימה, מחזירים הקשר ריק
        logger.error("kb_service: כשל בקריאת גרסת בסיס הידע", exc_info=True)
        return KBContext(text="", entry_count=0, token_estimate=0,
                         is_over_threshold=False, version="")

    with _cache_lock:
        hit = _cache.get(tenant)
        if hit and hit[0] == version:
            return hit[1]

    entries = db.get_all_kb_entries(active_only=True)
    text = _format_entries(entries)
    tokens = _estimate_tokens(text)
    threshold = getattr(_config, "KB_TOKEN_WARN_THRESHOLD", 50000)
    ctx = KBContext(
        text=text,
        entry_count=len(entries),
        token_estimate=tokens,
        is_over_threshold=tokens > threshold,
        version=version,
    )
    if ctx.is_over_threshold:
        logger.warning(
            "kb_service: בסיס הידע של tenant=%s גדול (%d טוקנים, סף %d) — "
            "שקול פיצול או retrieval",
            tenant, tokens, threshold,
        )
    with _cache_lock:
        _cache[tenant] = (version, ctx)
    return ctx


def invalidate_cache(tenant_id: str | None = None) -> None:
    """אינבלידציה ידנית של ה-cache (טסטים, ‏offboarding).

    בזרימה הרגילה אין בה צורך: מפתח ה-cache הוא גרסת בסיס הידע, ולכן
    עריכה בפאנל מתבטאת מיד.
    """
    with _cache_lock:
        if tenant_id is None:
            _cache.clear()
        else:
            _cache.pop(tenant_id, None)
