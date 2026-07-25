"""
‏handlers של ערוץ ה-Secretary — **נקודת הכניסה היחידה ללקוח**.

הכלל המחייב (‏CLAUDE.md → הערוץ): אסור נתיב חדש שפונה ללקוח מחוץ לצינור
הזה. ה-guards ליניאריים ובסדר קבוע, בלי דקורטורים — יש נקודת כניסה אחת,
ולכן קוד ליניארי קריא יותר מדקורטורים שמסתירים את הסדר.

סדר ה-guards ב-`on_business_message` (‏PLAN §4.2), וכל אחד מהם קיים
מסיבה:

1. **חיבור לא מוכר** ⇒ עצירה. הגנת cross-wiring: ‏connection של tenant
   אחד שהגיע ל-route של אחר לא ייענה בזהות הלא נכונה.
2. **זיהוי בעלים אנושי** ⇒ ‏takeover ושמירה. התנאי משולש:
   `from.id == owner_user_id` **וגם** `not is_from_offline` **וגם**
   `sender_business_bot is None`. הודעות אוטומטיות של טלגרם עצמה
   (greeting/away/מתוזמנות) יוצאות "מהבעלים" ומסומנות `is_from_offline` —
   סינון לפי `from.id` בלבד היה מפרש אותן כהתערבות אנושית ומשתיק את
   הבוט בטעות.
3. **שמירת ההודעה הנכנסת — תמיד**, גם בהשתקה. זו ההיסטוריה בפאנל.
4. **חסימה / השתקה** ⇒ שקט.
5. **חריגת קצב** ⇒ שקט ללקוח + התראה לבעלים.
6. **‏`can_reply` חסר** ⇒ שקט + התראה.

ללקוח הסופי לעולם אין מקלדות, פקודות, הודעות מערכת, או טוקן `[HANDOFF]`
שדלף. שתיקה עדיפה על הסגרת אוטומציה.
"""

from __future__ import annotations

import asyncio
import logging

import control_plane as cp
import database as db
from core.message_processor import process_incoming_message
from services import owner_channel, takeover_service

logger = logging.getLogger(__name__)


def _display_name(user) -> str:
    """שם לתצוגה מאובייקט משתמש של טלגרם."""
    if user is None:
        return ""
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (user.username or str(user.id))


def _resolve_connection(connection_id: str) -> dict | None:
    """רשומת החיבור — **בתנאי שהיא שייכת ל-tenant הנוכחי**.

    ה-tenant נקבע מה-route של הבוט-הבן, עוד לפני שנגענו בתוכן. חיבור
    ששייך ל-tenant אחר מוחזר כ-None: זו הגנת ה-cross-wiring של PLAN §4.2,
    והיא המקום היחיד שבו היא נאכפת.
    """
    from tenancy import get_current_tenant

    if not connection_id:
        return None
    row = cp.get_business_connection(connection_id)
    if row is None:
        return None
    tenant = get_current_tenant()
    if row.get("tenant_id") != tenant:
        logger.error(
            "cross-wiring: חיבור של tenant אחר הגיע ל-route של %s — נדחה", tenant,
        )
        return None
    return row


# ─── עדכון חיבור ─────────────────────────────────────────────────────────


async def on_business_connection(update, context) -> None:
    """חיבור, ניתוק, או עריכת הרשאות.

    אימות הבעלים: אם ל-tenant כבר רשום בעלים אחר, העדכון נדחה. ‏fail
    closed — עדיף בוט שלא עונה על חיבור לגיטימי מאשר בוט שעונה בשם חשבון
    שלא אישר.
    """
    from tenancy import get_current_tenant

    conn_obj = update.business_connection
    if conn_obj is None:
        return

    tenant = get_current_tenant()
    owner_user_id = conn_obj.user.id if conn_obj.user else None
    if owner_user_id is None:
        logger.error("business_connection בלי משתמש — נדחה")
        return

    rights = conn_obj.rights
    can_reply = bool(getattr(rights, "can_reply", False)) if rights else False
    rights_json = "{}"
    if rights is not None:
        try:
            import json

            rights_json = json.dumps(rights.to_dict(), ensure_ascii=False, sort_keys=True)
        except Exception:
            logger.error("סריאליזציה של rights נכשלה", exc_info=True)

    existing = cp.get_business_connection(conn_obj.id)
    if existing and existing.get("owner_user_id") != owner_user_id:
        logger.error("business_connection: הבעלים השתנה עבור אותו חיבור — נדחה")
        return
    known = cp.get_business_connection_for_tenant(tenant)
    if known and known["connection_id"] != conn_obj.id \
            and known.get("owner_user_id") != owner_user_id:
        logger.error(
            "business_connection: משתמש שאינו הבעלים הרשום ניסה להתחבר ל-%s — נדחה",
            tenant,
        )
        return

    cp.upsert_business_connection(
        connection_id=conn_obj.id,
        tenant_id=tenant,
        owner_user_id=owner_user_id,
        user_chat_id=conn_obj.user_chat_id,
        is_enabled=bool(conn_obj.is_enabled),
        can_reply=can_reply,
        rights_json=rights_json,
    )
    # עדכון סטטוס הבוט-הבן, אם הוא רשום (שלב 2)
    try:
        bot_row = cp.get_managed_bot_for_tenant(tenant)
        if bot_row and conn_obj.is_enabled:
            cp.set_managed_bot_status(bot_row["bot_id"], "connected")
    except Exception:
        logger.error("עדכון סטטוס הבוט-הבן נכשל", exc_info=True)

    stored = cp.get_business_connection(conn_obj.id)
    if stored:
        await owner_channel.notify_connection_changed(
            context.bot, stored, bool(conn_obj.is_enabled), can_reply,
        )
    logger.info(
        "business_connection: tenant=%s enabled=%s can_reply=%s",
        tenant, conn_obj.is_enabled, can_reply,
    )


# ─── הודעה נכנסת ─────────────────────────────────────────────────────────


async def on_business_message(update, context) -> None:
    """הצינור היחיד להודעת לקוח."""
    msg = update.business_message
    if msg is None:
        return

    # 1 — חיבור מוכר ופעיל
    conn = _resolve_connection(msg.business_connection_id)
    if conn is None or not conn.get("is_enabled"):
        logger.warning("הודעה מחיבור לא מוכר או מנותק — נזרקה")
        return

    display_name = _display_name(msg.from_user)
    chat_id = msg.chat.id
    user_id = str(msg.from_user.id) if msg.from_user else ""

    # 2 — הבעלים ענה בעצמו: takeover ושמירה, בלי תשובה
    is_owner_human = (
        msg.from_user is not None
        and msg.from_user.id == conn.get("owner_user_id")
        and not msg.is_from_offline          # greeting/away/מתוזמן ≠ התערבות
        and msg.sender_business_bot is None  # הגנת עומק: הודעות-עצמי לא מגיעות
    )
    if is_owner_human:
        takeover_service.on_owner_message(chat_id)
        _save_owner_message(msg, chat_id)
        return

    if not user_id:
        logger.warning("הודעה עסקית בלי שולח — נזרקה")
        return

    # 3 — שמירת ההודעה הנכנסת: תמיד, גם אם לא נענה
    history = db.get_conversation_history(user_id, limit=_context_window())
    is_media = not (msg.text or "").strip()
    incoming_text = (msg.text or "").strip() or "[מדיה]"
    _save_incoming(msg, user_id, display_name, incoming_text, chat_id)

    # 4 — חסימה / השתקה / autopilot כבוי ⇒ שקט מוחלט
    if _is_silenced(user_id, chat_id):
        return

    # 5 — חריגת קצב ⇒ שקט ללקוח, התראה לבעלים
    from rate_limiter import check_rate_limit, record_message

    window = check_rate_limit(user_id)
    if window is not None:
        await owner_channel.notify_rate_limited(context.bot, conn, display_name, window)
        return

    # 6 — אין הרשאת מענה ⇒ שקט + התראה
    if not conn.get("can_reply"):
        await owner_channel.notify_missing_permission(context.bot, conn)
        return

    record_message(user_id)

    # 7 — מדיה: מענה גישור קצר + התראה. לא שומרים את המדיה, לא מנסים
    #     להבין כיתוב (הוא מתייחס לתמונה שאיננו רואים).
    if is_media:
        await _handle_media(context.bot, msg, conn, user_id, display_name, chat_id)
        return

    # 8 — הצינור: הכוונה, ה-LLM, וה-handoff. ‏to_thread כי הצינור
    #     סינכרוני (DB + LLM) ואסור לחסום את לולאת הבוט.
    #     ‏contextvars מועתקים ל-thread ע"י to_thread, ולכן ה-tenant נשמר.
    result = await asyncio.to_thread(
        process_incoming_message,
        user_id=user_id,
        text=msg.text,
        user_info={"display_name": display_name},
        consecutive_fallbacks=db.get_consecutive_fallbacks(user_id),
        rate_limit_already_checked=True,
        conversation_history=history,
    )

    from bot.dispatch import dispatch_result

    await dispatch_result(context.bot, result, msg, conn, display_name)

    # 9 — מצב אחרי התשובה: מונה ה-handoffs, והסלמה להשתקה
    try:
        db.set_consecutive_fallbacks(user_id, result.consecutive_fallbacks)
        if result.escalate_takeover:
            # ההסלמה השלישית: הבוט מפסיק לנסות ומחכה לבעלים (PLAN §3.3)
            db.start_live_chat(str(chat_id), user_id, display_name, started_by="handoff")
            logger.info("takeover: הצ'אט הושתק אחרי handoffs רצופים")
    except Exception:
        logger.error("עדכון מצב השיחה אחרי התשובה נכשל", exc_info=True)

    if result.needs_summarization:
        # סיכום ברקע — לא מעכב את התשובה שכבר נשלחה
        _schedule_summary(user_id)


def _context_window() -> int:
    import config as _cfg

    return getattr(_cfg, "CONTEXT_WINDOW_SIZE", 10)


def _save_incoming(msg, user_id: str, display_name: str, text: str, chat_id: int) -> None:
    """שמירת ההודעה הנכנסת + עדכון חלון 24 השעות."""
    try:
        db.upsert_user(user_id, display_name, chat_id=str(chat_id), inbound=True)
        db.save_message(
            user_id, display_name, "user", text,
            authored_by="customer", tg_chat_id=chat_id, tg_message_id=msg.message_id,
        )
    except Exception:
        logger.error("שמירת ההודעה הנכנסת נכשלה", exc_info=True)


def _save_owner_message(msg, chat_id: int) -> None:
    """שמירת תשובה שהבעלים כתב בעצמו — היא חלק מההיסטוריה.

    נשמרת תחת ה-user_id של הלקוח (הצ'אט שלו), עם `authored_by='owner'`
    כדי שהפאנל וההיסטוריה שנשלחת ל-LLM יידעו מי ענה.
    """
    try:
        customer_id = str(msg.chat.id)
        db.save_message(
            customer_id, "", "assistant", (msg.text or "").strip() or "[מדיה]",
            authored_by="owner", tg_chat_id=chat_id, tg_message_id=msg.message_id,
        )
    except Exception:
        logger.error("שמירת הודעת הבעלים נכשלה", exc_info=True)


def _is_silenced(user_id: str, chat_id: int) -> bool:
    """חסימה, השתקה פעילה, או autopilot כבוי — בכל אלה שותקים."""
    try:
        if db.is_user_blocked(user_id):
            logger.info("הודעה ממשתמש חסום — שקט")
            return True
        if takeover_service.is_paused(str(chat_id)):
            logger.info("הצ'אט מושתק (הבעלים בשיחה) — שקט")
            return True
        if not db.is_autopilot_enabled():
            logger.info("autopilot כבוי — שקט")
            return True
    except Exception:
        # כשל בבדיקת ה-guards: שותקים. עדיף לא לענות מאשר לענות למי
        # שהבעלים חסם או בזמן שהוא עצמו בשיחה (fail closed).
        logger.error("בדיקת ה-guards נכשלה — שותקים ליתר ביטחון", exc_info=True)
        return True
    return False


async def _handle_media(bot, msg, conn: dict, user_id: str, display_name: str,
                        chat_id: int) -> None:
    """הודעת מדיה: משפט גישור ללקוח + התראה לבעלים.

    לא שומרים את המדיה עצמה (מזעור — ‏PLAN §6), ולא מנסים לענות לפי
    כיתוב: הכיתוב מתייחס לתמונה שאנחנו לא רואים, ותשובה כזאת תהיה ניחוש.
    """
    from bot.dispatch import send_to_customer

    settings = db.get_bot_settings() or {}
    bridge = (settings.get("media_bridge_message") or "").strip() \
        or "קיבלתי, אעבור על זה ואחזור אליך"

    sent = await send_to_customer(
        bot, chat_id, msg.business_connection_id, bridge, user_id, display_name, conn,
    )
    if sent:
        try:
            db.save_message(
                user_id, display_name, "assistant", sent,
                authored_by="bot", tg_chat_id=chat_id,
            )
        except Exception:
            logger.error("שמירת מענה המדיה נכשלה", exc_info=True)
    await owner_channel.notify_media(bot, conn, display_name)


def _schedule_summary(user_id: str) -> None:
    """תזמון סיכום שיחה ברקע, תחת ה-tenant הנוכחי.

    ה-tenant מועבר במפורש: ‏contextvars **לא** עוברים ל-thread חדש, ולכן
    ‏`maybe_summarize` היה נופל ל-tenant של ברירת המחדל וכותב סיכום של
    לקוח אחד ל-DB של עסק אחר (‏CLAUDE.md — multi-tenant).
    """
    import threading

    from tenancy import get_current_tenant, tenant_context

    tenant = get_current_tenant()

    def _run() -> None:
        try:
            with tenant_context(tenant):
                from llm import maybe_summarize

                maybe_summarize(user_id)
        except Exception:
            logger.error("סיכום השיחה ברקע נכשל", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="summarize").start()


# ─── עריכה ומחיקה ────────────────────────────────────────────────────────


async def on_edited_business_message(update, context) -> None:
    """הודעה נערכה — מעדכנים את העותק השמור.

    בלי זה, ההיסטוריה שנשלחת ל-LLM ולפאנל מציגה נוסח שהלקוח כבר תיקן.
    """
    msg = update.edited_business_message
    if msg is None:
        return
    if _resolve_connection(msg.business_connection_id) is None:
        logger.warning("עריכה מחיבור לא מוכר — נזרקה")
        return
    new_text = (msg.text or "").strip() or "[מדיה]"
    try:
        updated = db.update_message_by_tg_id(msg.chat.id, msg.message_id, new_text)
        logger.info("edited_business_message: עודכנו %d עותקים", updated)
    except Exception:
        logger.error("עדכון ההודעה שנערכה נכשל", exc_info=True)


async def on_deleted_business_messages(update, context) -> None:
    """הודעות נמחקו אצל הלקוח — מוחקים את העותקים מיידית.

    זו **חובת פרטיות, לא אופציה** (‏PLAN §6): טלגרם הודיעה שהתוכן נמחק,
    ולכן העותק שלנו חייב להימחק. המחיקה כוללת רישום ב-consent_ledger.
    נגזרות (עובדות זיכרון שנחלצו מההודעות) — ‏T4.1.
    """
    deleted = update.deleted_business_messages
    if deleted is None:
        return
    if _resolve_connection(deleted.business_connection_id) is None:
        logger.warning("הודעת מחיקה מחיבור לא מוכר — נזרקה")
        return

    chat_id = deleted.chat.id if deleted.chat else None
    message_ids = list(deleted.message_ids or [])
    if chat_id is None or not message_ids:
        return

    try:
        removed = db.delete_messages_by_tg_ids(chat_id, message_ids)
        logger.info("deleted_business_messages: נמחקו %d עותקים", removed)
    except Exception:
        logger.error("מחיקת העותקים נכשלה", exc_info=True)
        return

    try:
        from utils.consent_ledger import EVENT_DELETION_COMPLETED, record_consent_event

        record_consent_event(
            user_id=str(chat_id), channel=db.CHANNEL,
            event_type=EVENT_DELETION_COMPLETED,
            metadata={"source": "deleted_business_messages", "count": removed},
        )
    except Exception:
        logger.error("רישום המחיקה ב-consent_ledger נכשל", exc_info=True)
