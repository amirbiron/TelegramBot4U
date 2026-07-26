"""פנקס הסכמות פסאודונימי (consent_ledger) — תיקון 13 + תיקון 40.

תכלית: לשמור ראיות הסכמה/ביטול/מחיקה/עיון אחרי שהמשתמש מבקש /forget,
מבלי להשאיר את המידע האישי עצמו. ה-ledger דטרמיניסטי (אותו user_id →
אותו subject_hash) — אופציה א' לפי המלצת היועץ: מקושר במאמץ סביר ולכן
פסאודונימי, לא אנונימי. שקיפות במדיניות הפרטיות מטפלת בזה.

ארכיטקטורה: שתי קטגוריות באותה טבלה עם retention שונה:
  - 'consent' — הוכחות הסכמה (consent_given/revoked/superseded,
    opt_in/out_marketing). retention: 5 שנים מהאירוע (או כל עוד
    החשבון פעיל ל-consent_given שלא בוטל).
  - 'audit' — תיעוד מימוש זכויות (deletion/access requested/completed).
    retention: 24 חודשים.

אבטחה: HMAC-SHA256(user_id || channel, pepper) — pepper ב-env var
נפרד (LEDGER_PEPPER_V1) שלא חי ב-DB ולא חי ב-SECRETS_ENCRYPTION_KEY.
תמיכה ב-key rotation: pepper_version נשמר בכל רשומה. דליפת pepper →
מסמנים compromised=1 על רשומות עם הגרסה הדלופה ועוברים ל-V2 קדימה.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# קטגוריות — קובעות retention. ראה purge_old_data ב-database.py.
CATEGORY_CONSENT = "consent"
CATEGORY_AUDIT = "audit"

# event_type enum — הקטגוריה נגזרת מהסוג. אם מוסיפים event_type חדש,
# חובה לעדכן את EVENT_TYPE_CATEGORIES וגם את המטריצה.
EVENT_CONSENT_GIVEN = "consent_given"
EVENT_CONSENT_REVOKED = "consent_revoked"
EVENT_CONSENT_SUPERSEDED = "consent_superseded"
EVENT_OPT_IN_MARKETING = "opt_in_marketing"
EVENT_OPT_OUT_MARKETING = "opt_out_marketing"
EVENT_DELETION_REQUESTED = "deletion_requested"
EVENT_DELETION_COMPLETED = "deletion_completed"
EVENT_DELETION_FAILED = "deletion_failed"
EVENT_ACCESS_REQUESTED = "access_requested"
EVENT_ACCESS_DELIVERED = "access_delivered"
# החלטת בעל העסק על שורת הגילוי (‏T4.3). קטגוריה consent — זו‏
# החלטה על הבסיס המשפטי מול לקוחות הקצה, לא פעולה תפעולית.
EVENT_DISCLOSURE_ENABLED = "disclosure_enabled"
EVENT_DISCLOSURE_DISABLED = "disclosure_disabled"

EVENT_TYPE_CATEGORIES: dict[str, str] = {
    EVENT_CONSENT_GIVEN: CATEGORY_CONSENT,
    EVENT_CONSENT_REVOKED: CATEGORY_CONSENT,
    EVENT_CONSENT_SUPERSEDED: CATEGORY_CONSENT,
    EVENT_OPT_IN_MARKETING: CATEGORY_CONSENT,
    EVENT_OPT_OUT_MARKETING: CATEGORY_CONSENT,
    EVENT_DELETION_REQUESTED: CATEGORY_AUDIT,
    EVENT_DELETION_COMPLETED: CATEGORY_AUDIT,
    EVENT_DELETION_FAILED: CATEGORY_AUDIT,
    EVENT_ACCESS_REQUESTED: CATEGORY_AUDIT,
    EVENT_ACCESS_DELIVERED: CATEGORY_AUDIT,
    EVENT_DISCLOSURE_ENABLED: CATEGORY_CONSENT,
    EVENT_DISCLOSURE_DISABLED: CATEGORY_CONSENT,
}

CURRENT_PEPPER_VERSION = "v1"
_PEPPER_ENV_PREFIX = "LEDGER_PEPPER_"


class LedgerConfigError(RuntimeError):
    """ה-pepper לא הוגדר. לא חוסם startup — הקריאות ל-record_consent_event
    ייכשלו רכות (logger.error) כדי שלא ליפול בייצור על rollout חלקי."""


def _get_pepper(version: str = CURRENT_PEPPER_VERSION) -> bytes:
    """מחזיר את ה-pepper לגרסה. מעלה חריגה אם לא מוגדר.

    שימוש: pepper = _get_pepper("v1") → b"..."
    משתנה הסביבה: LEDGER_PEPPER_V1.
    """
    env_var = f"{_PEPPER_ENV_PREFIX}{version.upper()}"
    raw = os.getenv(env_var, "").strip()
    if not raw:
        raise LedgerConfigError(
            f"{env_var} לא מוגדר — ledger לא יכול לכתוב רשומה. "
            f"לייצור: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return raw.encode("utf-8")


def is_pepper_configured(version: str = CURRENT_PEPPER_VERSION) -> bool:
    """בדיקה רכה — ה-pepper לגרסה קיים? משמש ב-startup checks."""
    env_var = f"{_PEPPER_ENV_PREFIX}{version.upper()}"
    return bool(os.getenv(env_var, "").strip())


def _subject_hash(user_id: str, channel: str, version: str = CURRENT_PEPPER_VERSION) -> str:
    """HMAC-SHA256(user_id || ':' || channel, pepper).

    משלב גם user_id וגם channel כדי שאותו מספר טלפון בערוצים שונים
    (אם יקרה תרחיש קצה) ייתן hashes שונים. דטרמיניסטי בכוונה — לפי
    המלצת היועץ (אופציה א').
    """
    pepper = _get_pepper(version)
    msg = f"{user_id}:{channel}".encode("utf-8")
    return hmac.new(pepper, msg, hashlib.sha256).hexdigest()


def _enqueue_retry(
    user_id: str,
    channel: str,
    event_type: str,
    consent_version: int | None,
    metadata: dict | None,
    event_at: str,
    last_error: str,
) -> None:
    """מכניס payload לטבלת ledger_write_retry לטיפול ב-job יומי.

    payload_json שומר user_id + channel גלויים (לא hash) — אם הכשל
    היה בגלל pepper חסר, ה-job יחשב hash בעת הניסיון הבא. זו טבלה
    זמנית; ה-PII בה צריך להתרוקן ברגע שהבעיה נפתרת.
    """
    try:
        from database import get_connection
        payload = {
            "user_id": user_id,
            "channel": channel,
            "event_type": event_type,
            "consent_version": consent_version,
            "metadata": metadata or {},
            "event_at": event_at,
        }
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO ledger_write_retry
                       (payload_json, attempts, last_error, last_attempt_at)
                   VALUES (?, 0, ?, datetime('now'))""",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), last_error),
            )
        logger.warning(
            "ledger_write_retry: enqueued %s for retry (reason=%s)",
            event_type, last_error,
        )
    except Exception:
        # אם גם ה-retry נכשל — אין יותר מה לעשות. log בולט.
        logger.error(
            "[LEDGER_RETRY_ENQUEUE_FAILED] event_type=%s — לא ניתן לשמור ל-retry",
            event_type, exc_info=True,
        )


def record_consent_event(
    user_id: str,
    channel: str,
    event_type: str,
    consent_version: int | None = None,
    metadata: dict | None = None,
) -> bool:
    """כותב רשומת אירוע ל-consent_ledger. מחזיר True אם הצליח.

    הקריאה לא חוסמת — אם ה-pepper לא מוגדר או יש כשל DB, מתעד שגיאה,
    מכניס ל-ledger_write_retry, ומחזיר False. הסיבה: הקורא לא צריך
    ליפול אם ה-ledger נכשל; הזכות עצמה יותר חשובה מהראיה. ה-retry
    queue מבטיח שלא נאבד את ההוכחה.

    user_id: ה-user_id של המשתמש (לפני hash).
    channel: תמיד 'telegram_business' בפרויקט הזה (ערוץ יחיד).
    event_type: אחד מ-EVENT_* קבועים.
    consent_version: אם רלוונטי (consent_given/superseded).
    metadata: dict אופציונלי — counts של מחיקה, סיבת ביטול, וכו'.
    """
    if event_type not in EVENT_TYPE_CATEGORIES:
        logger.error(
            "record_consent_event: event_type לא מוכר: %s", event_type,
        )
        return False

    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    event_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    category = EVENT_TYPE_CATEGORIES[event_type]

    # שלב 1: חישוב hash. אם pepper חסר → enqueue ל-retry, לא נאבד.
    try:
        subj_hash = _subject_hash(user_id, channel, CURRENT_PEPPER_VERSION)
    except LedgerConfigError as exc:
        logger.error(
            "record_consent_event: pepper חסר ל-%s, מועבר ל-retry queue: %s",
            event_type, exc,
        )
        _enqueue_retry(
            user_id, channel, event_type, consent_version, metadata,
            event_at, f"pepper_missing: {exc}",
        )
        return False

    # שלב 2: כתיבה ל-ledger. כשל DB → enqueue ל-retry.
    try:
        from database import get_connection
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO consent_ledger
                       (subject_hash, pepper_version, channel, category,
                        event_type, consent_version, event_at, metadata_json,
                        compromised)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    subj_hash, CURRENT_PEPPER_VERSION, channel, category,
                    event_type, consent_version, event_at, metadata_json,
                ),
            )
        logger.info(
            "consent_ledger: %s recorded (channel=%s, version=%s)",
            event_type, channel, consent_version,
        )
        return True
    except Exception as exc:
        logger.error(
            "record_consent_event: כשל DB ב-%s, מועבר ל-retry", event_type,
            exc_info=True,
        )
        _enqueue_retry(
            user_id, channel, event_type, consent_version, metadata,
            event_at, f"db_error: {type(exc).__name__}",
        )
        return False


# מקסימום ניסיונות לפני שמרימים [LEDGER_RETRY_EXHAUSTED] ל-Render logs
# ומשאירים את הרשומה ל-investigation ידני. 5 הוא מספר סביר — הספיק כדי
# לשרוד downtime זמני, מועט מספיק כדי לא להציף.
LEDGER_RETRY_MAX_ATTEMPTS = 5


def process_ledger_retry_queue(max_records: int = 100) -> dict:
    """job יומי — מנסה לכתוב מחדש רשומות ledger שנכשלו.

    מופעל מתוך purge_old_data כדי להשתמש ב-scheduler הקיים. מחזיר
    dict עם counts: succeeded, failed, exhausted, total_processed.
    אחרי LEDGER_RETRY_MAX_ATTEMPTS ניסיונות — log עם prefix מובחן
    [LEDGER_RETRY_EXHAUSTED] שאפשר לחפש ב-Render logs.
    """
    counts = {"succeeded": 0, "failed": 0, "exhausted": 0, "total_processed": 0}
    try:
        from database import get_connection
    except Exception:
        logger.error("process_ledger_retry_queue: כשל import של database", exc_info=True)
        return counts

    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, payload_json, attempts FROM ledger_write_retry "
                "WHERE attempts < ? ORDER BY id ASC LIMIT ?",
                (LEDGER_RETRY_MAX_ATTEMPTS, max_records),
            ).fetchall()
    except Exception:
        logger.error("process_ledger_retry_queue: כשל בשליפה", exc_info=True)
        return counts

    for row in rows:
        rd = dict(row)
        counts["total_processed"] += 1
        try:
            payload = json.loads(rd["payload_json"])
        except Exception:
            logger.error(
                "[LEDGER_RETRY_EXHAUSTED] retry_id=%s — payload_json פגום, "
                "מסמן כ-exhausted", rd["id"],
            )
            _mark_retry_exhausted(rd["id"], "invalid_payload_json")
            counts["exhausted"] += 1
            continue

        # ניסיון כתיבה ישירות ל-ledger (לא דרך record_consent_event,
        # כדי שכשל לא יכניס שוב לתור ויצור loop)
        try:
            subj_hash = _subject_hash(
                payload["user_id"], payload["channel"], CURRENT_PEPPER_VERSION,
            )
            metadata_json = json.dumps(
                payload.get("metadata") or {}, ensure_ascii=False, sort_keys=True,
            )
            category = EVENT_TYPE_CATEGORIES[payload["event_type"]]
            with get_connection() as conn:
                conn.execute(
                    """INSERT INTO consent_ledger
                           (subject_hash, pepper_version, channel, category,
                            event_type, consent_version, event_at, metadata_json,
                            compromised)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (
                        subj_hash, CURRENT_PEPPER_VERSION, payload["channel"],
                        category, payload["event_type"],
                        payload.get("consent_version"),
                        payload.get("event_at") or datetime.now(timezone.utc).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        metadata_json,
                    ),
                )
                # הצלחה → DELETE מהתור
                conn.execute("DELETE FROM ledger_write_retry WHERE id = ?", (rd["id"],))
            counts["succeeded"] += 1
            logger.info(
                "ledger_retry: success id=%s event_type=%s",
                rd["id"], payload["event_type"],
            )
        except LedgerConfigError as exc:
            # pepper עדיין חסר — לא מגדילים attempts, מחכים שיוגדר
            logger.warning(
                "ledger_retry: id=%s עדיין ממתין ל-pepper", rd["id"],
            )
            counts["failed"] += 1
            _bump_retry_attempt(rd["id"], rd["attempts"], f"pepper_still_missing: {exc}")
        except Exception as exc:
            new_attempts = rd["attempts"] + 1
            counts["failed"] += 1
            if new_attempts >= LEDGER_RETRY_MAX_ATTEMPTS:
                logger.error(
                    "[LEDGER_RETRY_EXHAUSTED] retry_id=%s event_type=%s "
                    "attempts=%d last_error=%s — נדרש טיפול ידני",
                    rd["id"], payload.get("event_type", "?"),
                    new_attempts, type(exc).__name__,
                )
                counts["exhausted"] += 1
            _bump_retry_attempt(rd["id"], new_attempts, f"{type(exc).__name__}: {exc}")

    if counts["total_processed"]:
        logger.info("process_ledger_retry_queue: %s", counts)
    return counts


def _bump_retry_attempt(retry_id: int, new_attempts: int, error: str) -> None:
    """עדכון attempts + last_error + last_attempt_at לרשומה ב-retry."""
    try:
        from database import get_connection
        with get_connection() as conn:
            conn.execute(
                "UPDATE ledger_write_retry SET attempts = ?, last_error = ?, "
                "last_attempt_at = datetime('now') WHERE id = ?",
                (new_attempts, error[:500], retry_id),
            )
    except Exception:
        logger.error("_bump_retry_attempt: כשל בעדכון retry_id=%s", retry_id, exc_info=True)


def _mark_retry_exhausted(retry_id: int, reason: str) -> None:
    """סימון רשומה ככזו שמיצתה ניסיונות (גם אם עוד לא הגיעה למקס')."""
    _bump_retry_attempt(retry_id, LEDGER_RETRY_MAX_ATTEMPTS, reason)


def get_events_for_subject(
    user_id: str,
    channel: str,
    event_type: str | None = None,
    pepper_version: str = CURRENT_PEPPER_VERSION,
) -> list[dict]:
    """שליפת אירועים עבור subject מסוים — בעיקר לטסטים ול-debugging.

    אסור להשתמש לזכות עיון של משתמש (ה-ledger הוא פסאודונימי, לא
    מיועד לחשיפה ישירה). זה כלי מנהלים בלבד.
    """
    try:
        subj_hash = _subject_hash(user_id, channel, pepper_version)
    except LedgerConfigError:
        return []

    from database import get_connection
    sql = (
        "SELECT * FROM consent_ledger "
        "WHERE subject_hash = ? AND pepper_version = ?"
    )
    params: list = [subj_hash, pepper_version]
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    sql += " ORDER BY event_at ASC, id ASC"

    try:
        with get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        logger.error("get_events_for_subject: כשל DB", exc_info=True)
        return []


def mark_pepper_compromised(version: str) -> int:
    """לאחר דליפה משוערת של pepper — מסמן את כל הרשומות בגרסה הדלופה
    כ-compromised. הרשומות לא נמחקות (הן עדיין הוכחה משפטית), אבל
    לא ניתן עוד להישען עליהן ל-anonymity claim.

    אחרי הקריאה, יש להגדיר LEDGER_PEPPER_V<N+1> ב-env, ולעדכן את
    CURRENT_PEPPER_VERSION בקוד. רשומות חדשות יכתבו עם הגרסה החדשה.
    """
    from database import get_connection
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "UPDATE consent_ledger SET compromised = 1 "
                "WHERE pepper_version = ? AND compromised = 0",
                (version,),
            )
            count = cur.rowcount or 0
            logger.warning(
                "mark_pepper_compromised: סומנו %d רשומות עם pepper_version=%s",
                count, version,
            )
            return count
    except Exception:
        logger.error("mark_pepper_compromised: כשל DB", exc_info=True)
        return 0
