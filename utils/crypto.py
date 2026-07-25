"""הצפנה סימטרית לסודות במנוחה (refresh tokens, OAuth credentials).

עיקרון: שדות ספציפיים שאם דולפים מעניקים גישה חיצונית — מוצפנים ברמת
היישום עם Fernet (AES-128-CBC + HMAC). המפתח חי ב-env var נפרד מה-DB
(`SECRETS_ENCRYPTION_KEY`); כך שגם אם DB דולף, התוקף לא יכול לפענח
בלי המפתח.

פורמט: `v1:<base64_ciphertext>` — ה-prefix מאפשר key rotation עתידי
בלי לשבור את ה-DB. שדות ריקים ('') לא מוצפנים, נשמרים כמו שהם.

Migration plan: encrypt_at_write בקוד, read_both_formats לתקופת מעבר.
לפענוח, decrypt_field מזהה אם הערך כבר מוצפן (מתחיל ב-'v1:') ופועל
בהתאם — כך ערכים legacy בטקסט גלוי עוד עובדים עד שיכתבו מחדש.
"""

from __future__ import annotations

import base64
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

CURRENT_KEY_VERSION = "v1"
_KEY_PREFIX_SEPARATOR = ":"

# cache של מופעי Fernet לפי גרסת מפתח — Fernet thread-safe
_fernet_cache: dict[str, Fernet] = {}


class EncryptionConfigError(RuntimeError):
    """מפתח ההצפנה לא הוגדר או לא תקין."""


def _normalize_key(raw: str) -> bytes:
    """מקבל מפתח מ-env (base64 או טקסט) ומחזיר bytes באורך תקין ל-Fernet.

    Fernet דורש 32 בייט base64-encoded. אם המשתמש סיפק מחרוזת base64
    תקינה — נשתמש בה ישירות. אחרת — נגזור 32 בייט מ-SHA256 של הטקסט
    (גישה פשוטה למשתמשים שלא רוצים להתעסק עם base64 ידנית, אבל פחות
    מומלצת — כי איכות המפתח תלויה באורך הקלט).
    """
    raw = raw.strip()
    if not raw:
        raise EncryptionConfigError(
            "SECRETS_ENCRYPTION_KEY ריק. הגדר משתנה סביבה עם מפתח Fernet "
            "תקין (Fernet.generate_key().decode())."
        )

    # ניסיון ראשון: base64 תקין באורך 44 תווים (הפלט הסטנדרטי של Fernet.generate_key)
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        if len(decoded) == 32:
            return raw.encode("ascii")
    except Exception:
        pass

    # נפילה רכה: גזירה מ-SHA256 כדי לא לחסום את האפליקציה אם המשתמש סיפק
    # סיסמה רגילה. עדיין עובד, אבל המפתח באיכות נמוכה יותר.
    import hashlib
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    logger.warning(
        "SECRETS_ENCRYPTION_KEY לא בפורמט Fernet סטנדרטי — נגזר מ-SHA256. "
        "מומלץ להחליף ל-Fernet.generate_key() תקין."
    )
    return base64.urlsafe_b64encode(digest)


def _get_fernet(version: str = CURRENT_KEY_VERSION) -> Fernet:
    """מחזיר Fernet לגרסה מבוקשת. כעת קיימת רק v1; key rotation בעתיד
    יוסיף v2 וכו' עם משתני סביבה ייעודיים (SECRETS_ENCRYPTION_KEY_V2)."""
    if version in _fernet_cache:
        return _fernet_cache[version]

    env_var = "SECRETS_ENCRYPTION_KEY" if version == "v1" else f"SECRETS_ENCRYPTION_KEY_{version.upper()}"
    raw = os.getenv(env_var, "")
    key = _normalize_key(raw)
    fernet = Fernet(key)
    _fernet_cache[version] = fernet
    return fernet


def is_encryption_configured() -> bool:
    """בדיקה רכה — האם המפתח קיים. לא מעלה חריגה (משמש ב-startup checks)."""
    return bool(os.getenv("SECRETS_ENCRYPTION_KEY", "").strip())


# דגל שמתעד אם כבר היה log אזהרה ל-legacy mode (כתיבה בלי הצפנה).
# פעם אחת בריצה — לא להציף Render logs.
_legacy_warning_logged = False


def encrypt_field(plaintext: str) -> str:
    """מצפין שדה. מחזיר 'v1:<ciphertext>'.

    Legacy mode: אם SECRETS_ENCRYPTION_KEY לא מוגדר, מחזיר את הטקסט
    כמו שהוא (בלי prefix). decrypt_field יודע לטפל בזה — ערכים בלי
    prefix נחשבים legacy plaintext. זה מה ש-.env.example מבטיח —
    בלי המפתח, ההצפנה היא no-op רכה במקום שבירת deployments קיימים.

    שדה ריק ('') — נשמר כמו שהוא (אין טעם להצפין כלום, וזה גם מאפשר
    לזהות 'אין טוקן' בלי לפענח).
    """
    if not plaintext:
        return ""

    if not is_encryption_configured():
        global _legacy_warning_logged
        if not _legacy_warning_logged:
            logger.warning(
                "SECRETS_ENCRYPTION_KEY לא מוגדר — שדות סודיים נשמרים "
                "בטקסט גלוי (legacy mode). להפעלת הצפנה, הגדר משתנה "
                "סביבה עם Fernet.generate_key().decode()."
            )
            _legacy_warning_logged = True
        return plaintext

    fernet = _get_fernet(CURRENT_KEY_VERSION)
    ciphertext = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{CURRENT_KEY_VERSION}{_KEY_PREFIX_SEPARATOR}{ciphertext}"


def encrypt_field_strict(plaintext: str) -> str:
    """כמו encrypt_field אבל **fail-closed**: בלי מפתח תקין — חריגה.

    לשימוש שכבת הפלטפורמה (control_plane.tenant_secrets): סודות של
    tenants לעולם לא נשמרים בטקסט גלוי. ה-fallback הרך של encrypt_field
    קיים רק לתאימות פריסות legacy של שדות ה-tenant הוותיקים.
    """
    if not plaintext:
        return ""
    if not is_encryption_configured():
        raise EncryptionConfigError(
            "SECRETS_ENCRYPTION_KEY לא מוגדר — כתיבת סודות פלטפורמה חסומה "
            "(fail-closed). ייצור מפתח: "
            "python -c 'from utils.crypto import generate_new_key; print(generate_new_key())'"
        )
    fernet = _get_fernet(CURRENT_KEY_VERSION)
    ciphertext = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{CURRENT_KEY_VERSION}{_KEY_PREFIX_SEPARATOR}{ciphertext}"


def decrypt_field(value: str) -> str:
    """מפענח שדה. תומך גם בערכים legacy בטקסט גלוי (בלי prefix).

    ערך ריק → ''.
    ערך עם prefix 'vN:' → פענוח לפי הגרסה.
    ערך בלי prefix → מחזיר כמו שהוא (legacy plaintext, יוחלף בכתיבה הבאה).
    """
    if not value:
        return ""

    # זיהוי prefix של גרסת מפתח: 'v1:', 'v2:' וכו'
    if _KEY_PREFIX_SEPARATOR in value and value.split(_KEY_PREFIX_SEPARATOR, 1)[0].startswith("v"):
        version, ciphertext = value.split(_KEY_PREFIX_SEPARATOR, 1)
        version = version.strip()
        if version[1:].isdigit():
            try:
                fernet = _get_fernet(version)
                return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
            except InvalidToken:
                logger.error(
                    "decrypt_field: InvalidToken — המפתח שונה או הערך פגום (version=%s)",
                    version,
                )
                raise
            except EncryptionConfigError:
                logger.error(
                    "decrypt_field: מפתח לגרסה %s לא הוגדר ב-env", version,
                )
                raise

    # legacy plaintext — מחזירים כמו שהוא
    return value


def is_encrypted(value: str) -> bool:
    """בדיקה אם ערך כבר מוצפן (מתחיל ב-prefix של גרסת מפתח)."""
    if not value or _KEY_PREFIX_SEPARATOR not in value:
        return False
    prefix = value.split(_KEY_PREFIX_SEPARATOR, 1)[0]
    return prefix.startswith("v") and prefix[1:].isdigit()


def generate_new_key() -> str:
    """עזר ל-CLI/admin: מייצר מפתח Fernet תקין להצבה ב-env var.

    שימוש: python -c 'from utils.crypto import generate_new_key; print(generate_new_key())'
    """
    return Fernet.generate_key().decode("ascii")
