"""
הבוט המנהל — צימוד לקוחות ויצירת בוטים-בנים (מודל B).

הבעיה שהצימוד פותר (‏PLAN §4.6): כשמגיע `managed_bot` update, טלגרם
אומרת **מי המשתמש היוצר** — לא לאיזה עסק הוא שייך אצלנו. לכן קושרים
`owner_user_id ↔ tenant` **לפני** יצירת הבוט:

    1. אשף בפאנל: יצירת tenant ⇒ קוד צימוד חד-פעמי (תפוגה שעה)
    2. הלקוח פותח את הבוט המנהל עם `/start PAIR-xxxx` ⇒ הקוד נצרך,
       ומעכשיו ידוע לאיזה tenant הוא שייך
    3. המנהל שולח דיפ-לינק ליצירת הבוט
    4. הלקוח מאשר ⇒ `managed_bot` update ⇒ התאמה לפי `user.id`
       (**ראשי**; ה-username משני, כי המשתמש יכול לשנות אותו במסך היצירה)

**‏Secretary Mode אינו ניתן להדלקה דרך ה-API** — נבדק ותועד ב-V1. לכן
שלב 4 בזרימה הוא הודעת הדרכה מפורשת ללקוח, ולא קריאת API.

הבוט המנהל נשאר לענייני פלטפורמה בלבד (צימוד, ‏billing עתידי). התקשורת
השוטפת עם בעל העסק עוברת בצ'אט שלו עם **הבוט שלו** (‏PLAN §4.5).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ManagedBotUpdatedHandler,
)

import control_plane as cp

logger = logging.getLogger(__name__)

PAIRING_PREFIX = "PAIR-"

# ‏username של בוט בטלגרם: 5–32 תווים, אותיות/ספרות/קו תחתון, מתחיל
# באות, **חייב להסתיים ב-bot**. אין מקפים — ולכן slug של tenant
# (שמותר בו מקף) עובר המרה.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[Bb][Oo][Tt]$")
_USERNAME_MAX = 32


def suggest_username(tenant_id: str, taken: set[str] | None = None) -> str:
    """‏username מוצע לבוט-הבן, נגזר מה-slug של ה-tenant.

    ההצעה בלבד — המשתמש יכול לערוך אותה במסך היצירה אם היא תפוסה
    בטלגרם, ולכן ההתאמה ל-tenant **לעולם לא נעשית לפי השם** (סיכון 6
    ב-PLAN §8).
    """
    taken = {t.lower() for t in (taken or set())}
    base = tenant_id.replace("-", "_").strip("_") or "biz"
    if not base[0].isalpha():
        base = "b_" + base
    # מקום ל-"_bot" ולסיומת ייחודיות
    base = base[: _USERNAME_MAX - 6]

    candidate = f"{base}_bot"
    suffix = 1
    while candidate.lower() in taken:
        suffix += 1
        candidate = f"{base}{suffix}_bot"

    # אכיפת התבנית בפועל ולא רק תיעודה. ‏slug קצר ("ac") נותן
    # "ac_bot" — 6 תווים, מתחת למינימום של טלגרם — וההצעה הייתה נדחית
    # במסך היצירה בלי שנדע למה. הריפוד מתרחש כאן, לא אצל הלקוח.
    if not _USERNAME_RE.match(candidate):
        padded = (base + "_service")[: _USERNAME_MAX - 4]
        candidate = f"{padded}_bot"
        suffix = 1
        while candidate.lower() in taken:
            suffix += 1
            candidate = f"{padded}{suffix}_bot"
    if not _USERNAME_RE.match(candidate):
        logger.warning(
            "לא הצלחתי לגזור username תקין מ-%s — נופלים להצעה גנרית", tenant_id,
        )
        candidate = "my_business_bot"
    return candidate


def build_creation_deep_link(manager_username: str, suggested: str, display_name: str) -> str:
    """הדיפ-לינק שפותח ללקוח מסך יצירת בוט ממולא מראש.

    הפורמט מתועד ב-`bots/features`:
    ``https://t.me/newbot/{manager}/{new_username}?name={new_name}``
    """
    return (
        f"https://t.me/newbot/{manager_username}/{suggested}"
        f"?name={quote(display_name)}"
    )


def build_pairing_link(manager_username: str, code: str) -> str:
    """הלינק שהאשף מציג ללקוח כדי לצמוד אותו ל-tenant."""
    return f"https://t.me/{manager_username}?start={PAIRING_PREFIX}{code}"


# ─── ‏handlers ───────────────────────────────────────────────────────────


async def on_start(update, context) -> None:
    """‏`/start PAIR-xxxx` — צריכת קוד הצימוד ושליחת הדיפ-לינק.

    ‏`/start` בלי קוד, קוד שגוי, או קוד שפג — כולם מקבלים אותה הודעה
    ניטרלית: אנחנו לא מסגירים אם הקוד קיים אבל נוצל, כי זה מידע על
    לקוחות אחרים.
    """
    msg = update.effective_message
    if msg is None or update.effective_user is None:
        return

    args = context.args if getattr(context, "args", None) else []
    raw = (args[0] if args else "").strip()
    if not raw.startswith(PAIRING_PREFIX):
        await msg.reply_text(
            "היי! כדי לחבר את הבוט לעסק שלך צריך קישור הצטרפות אישי. "
            "פנה למי שהקים לך את החשבון."
        )
        return

    code = raw[len(PAIRING_PREFIX):]
    tenant_id = cp.consume_pairing_code(code, update.effective_user.id)
    if not tenant_id:
        logger.warning("צימוד: קוד לא תקף")
        await msg.reply_text(
            "הקישור הזה כבר לא בתוקף. בקש קישור חדש ממי שהקים לך את החשבון."
        )
        return

    tenant = cp.get_tenant(tenant_id)
    display_name = (tenant or {}).get("display_name", tenant_id)
    taken = {b["bot_username"] for b in cp.list_managed_bots()}
    suggested = suggest_username(tenant_id, taken)
    manager_username = _manager_username()

    if not manager_username:
        logger.error("MANAGER_BOT_USERNAME לא מוגדר — אי אפשר לבנות דיפ-לינק")
        await msg.reply_text(
            "משהו לא מוגדר אצלנו נכון ואני לא יכול להמשיך כרגע. "
            "פנה למי שהקים לך את החשבון."
        )
        return

    link = build_creation_deep_link(manager_username, suggested, display_name)
    await msg.reply_text(
        f"מעולה, זיהיתי אותך — {display_name}.\n\n"
        "עכשיו ניצור לך בוט משלך. לחץ על הקישור, ואשר את המסך שייפתח "
        "(אפשר לשנות את השם וה-username אם תרצה):\n"
        f"{link}\n\n"
        "אחרי האישור אשלח לך את שני הצעדים האחרונים."
    )
    logger.info("צימוד הושלם ונשלח דיפ-לינק ליצירת בוט (tenant=%s)", tenant_id)


async def on_managed_bot(update, context) -> None:
    """קליטת בוט-בן שנוצר: טוקן, ‏webhook, והוראות ללקוח.

    סדר הפעולות נגזר מ-fail-closed: קודם מוודאים שיש התאמה ל-tenant,
    ורק אז מושכים טוקן. ‏`managed_bot` בלי צימוד תואם ⇒ לוג + הודעת
    "לא מזוהה" ליוצר, **בלי ליצור state**.
    """
    from tenancy import tenant_context

    managed = update.managed_bot
    if managed is None or managed.user is None or managed.bot is None:
        return

    creator_id = managed.user.id
    bot_id = managed.bot.id
    bot_username = managed.bot.username or ""

    tenant_id = cp.get_tenant_by_paired_user(creator_id)
    if not tenant_id:
        logger.warning("managed_bot: אין צימוד תואם ליוצר — נדחה")
        await _reply_to_creator(
            context, creator_id,
            "יצרת בוט, אבל אני לא מזהה אותך כלקוח שלנו. "
            "אם התכוונת לחבר אותו לעסק שלך — פנה למי שהקים לך את החשבון.",
        )
        return

    # 1. טוקן — הצעד היחיד שדורש את ה-API של טלגרם
    try:
        token = await context.bot.get_managed_bot_token(user_id=bot_id)
    except Exception:
        logger.error("managed_bot: משיכת הטוקן נכשלה", exc_info=True)
        await _reply_to_creator(
            context, creator_id,
            "הבוט נוצר, אבל לא הצלחתי לקבל אליו גישה. ננסה שוב בקרוב.",
        )
        return

    # 2. שמירה מוצפנת + רישום. **לפני** ה-setWebhook: אם השמירה נכשלה,
    #    ה-webhook היה מצביע לבוט שאין לנו טוקן אליו.
    try:
        cp.set_tenant_secret(tenant_id, "telegram_bot_token", token)
        cp.set_tenant_secret(tenant_id, "telegram_bot_username", bot_username)
        cp.register_managed_bot(bot_id, tenant_id, bot_username, creator_id)
    except Exception:
        logger.error("managed_bot: שמירת פרטי הבוט נכשלה", exc_info=True)
        await _reply_to_creator(
            context, creator_id, "הבוט נוצר אבל לא הצלחתי לשמור אותו. ננסה שוב.",
        )
        return

    # 3. ‏webhook. האפליקציה של ה-tenant מאופסת כדי שתיבנה מהטוקן החדש.
    from bot.business_bot import setup_tenant_webhook
    from bot.registry import reset_tenant

    reset_tenant(tenant_id)
    try:
        with tenant_context(tenant_id):
            await setup_tenant_webhook(tenant_id)
    except Exception:
        logger.error("managed_bot: רישום ה-webhook נכשל", exc_info=True)
        await _reply_to_creator(
            context, creator_id,
            "הבוט נוצר, אבל החיבור שלו אלינו לא הושלם. אנחנו על זה.",
        )
        return

    # 4. הגבלת גישה: רק הבעלים יוכל לדבר עם הבוט-הבן ישירות. סוגר וקטור
    #    שבו זר מוצא אותו ב-t.me ומתחיל איתו שיחה שאינה חלק מהערוץ.
    #    best-effort — כשל כאן לא מצדיק לעצור את ה-onboarding.
    try:
        await context.bot.set_managed_bot_access_settings(
            user_id=bot_id, is_access_restricted=True,
        )
    except Exception:
        logger.error("managed_bot: הגבלת הגישה לבוט-הבן נכשלה", exc_info=True)

    # 5. ההוראות ללקוח. שני הצעדים האלה **חייבים** להיעשות ידנית:
    #    אין API להדלקת Secretary Mode (V1), ואת החיבור עצמו רק בעל
    #    החשבון יכול לאשר.
    await _reply_to_creator(context, creator_id, _onboarding_instructions(bot_username))
    logger.info("managed_bot: בוט-בן נקלט ל-tenant=%s", tenant_id)


def _onboarding_instructions(bot_username: str) -> str:
    """שני הצעדים הידניים שנותרו ללקוח."""
    handle = f"@{bot_username}" if bot_username else "הבוט שלך"
    return (
        f"הבוט {handle} מוכן. נשארו שני צעדים קצרים, ושניהם אצלך באפליקציה:\n\n"
        "1️⃣ פתח את @BotFather ← /mybots ← בחר את הבוט ← Bot Settings ← "
        "הדלק את Secretary Mode.\n"
        "(בלי זה טלגרם לא תיתן לחבר אותו לחשבון — זו דרישה שלה, לא שלנו.)\n\n"
        "2️⃣ הגדרות טלגרם ← Chatbots ← בחר את הבוט ← אשר לו לענות להודעות, "
        "ובחר אילו צ'אטים הוא רואה.\n\n"
        "ברגע שתסיים אשלח לך אישור, ומשם אני מתחיל לענות ללקוחות שכותבים לך."
    )


async def _reply_to_creator(context, creator_id: int, text: str) -> None:
    """הודעה ליוצר בצ'אט שלו עם הבוט המנהל."""
    try:
        await context.bot.send_message(chat_id=creator_id, text=text)
    except Exception:
        logger.error("שליחת הודעה ליוצר הבוט נכשלה", exc_info=True)


def _manager_username() -> str:
    import config as _cfg

    return (getattr(_cfg, "MANAGER_BOT_USERNAME", "") or "").lstrip("@")


# ─── האפליקציה ───────────────────────────────────────────────────────────


def create_manager_application(token: str) -> Application:
    """אפליקציית הבוט המנהל.

    היא **קבועה** ולא עצלה: היא לא שייכת לאף tenant, ועולה פעם אחת
    בעליית התהליך (בניגוד לבוטים-הבנים, שנבנים לפי הצורך).
    """
    app = ApplicationBuilder().token(token).job_queue(None).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(ManagedBotUpdatedHandler(on_managed_bot))
    return app
