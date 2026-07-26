"""לוגים תפעוליים בלי PII (‏T4.5).

שני חצאים לאותה דרישה:

1. **מה שחייב להיות בלוג** — כל קריאת LLM, כל handoff, כל rate-limit
   hit, וכל כשל שליחה עם סיווג. בלעדיהם אין מה לחקור אחרי תקלה
   בפרודקשן, ואי אפשר לדעת אם הבוט שותק כי אין תנועה או כי הוא נחסם.
2. **מה שאסור להיות בלוג** — תוכן הודעות של לקוחות. הסריקה הסטטית
   כאן היא זו שמונעת מהמידע לזלוג פנימה בהיסח הדעת: לוג הוא היעד
   הקל ביותר להוסיף אליו משתנה, והוא היחיד שאין עליו בקרת הרשאות.
"""

import ast
import logging
import pathlib

import pytest

import database as db
from tenancy import tenant_context
from tests.doubles import FakeBot

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ביטויים שאסור להעביר ל-`logger.info` / `logger.warning`. אלה שמות
# שבקוד הזה מחזיקים תוכן חופשי שהלקוח כתב, או זהות שלו.
FORBIDDEN_ARGS = {
    "text", "raw_text", "message_text", "answer", "raw_answer",
    "question", "handoff_reason", "display_name", "first_name",
    "last_name", "username", "phone", "api_key", "token", "secret",
}

# מודולים שאינם רצים בפרודקשן ולכן לא מעניינים לצורך הסריקה.
SKIP_DIRS = {"tests", ".git", "__pycache__", "venv", ".venv", "backups"}

# פטורים מנומקים. ‏(קובץ, שם) — לא מספר שורה, כדי שהם לא יתיישנו בכל
# עריכה. כל שורה כאן היא החלטה, לא השתקה.
ALLOWED = {
    # ב-control_plane ה-`display_name` הוא **שם העסק** של הלקוח שלנו
    # (‏tenant), לא שמו של לקוח קצה. הוא ממילא מופיע בשם התיקייה,
    # בפאנל ובחשבונית.
    ("control_plane.py", "display_name"),
}


def _production_files() -> list[pathlib.Path]:
    return [
        p for p in ROOT.rglob("*.py")
        if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)
    ]


def _log_call_args(tree: ast.AST) -> list[tuple[int, ast.expr]]:
    """כל הארגומנטים של קריאות `logger.info` / `logger.warning`.

    ‏`error` ו-`exception` אינם נסרקים: הם רצים במסלול תקלה, לרוב עם
    `exc_info=True`, והם מה שמאפשר לדבג — הכלל ב-CLAUDE.md מדבר על
    רמת INFO.
    """
    found: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("info", "warning"):
            continue
        target = func.value
        if not (isinstance(target, ast.Name) and target.id == "logger"):
            continue
        # הארגומנט הראשון הוא תבנית הפורמט — הערכים הם מהשני והלאה.
        found.extend((node.lineno, arg) for arg in node.args[1:])
    return found


def _mentions_forbidden(arg: ast.expr) -> str | None:
    """שם אסור שמופיע בתוך הביטוי, או None.

    ‏`len(text)` מותר ואינו נספר: אורך אינו תוכן, והוא בדיוק מה שצריך
    כדי לדבג קציצה של הודעה בלי לראות מה נכתב בה. לכן לא יורדים אל
    תוך `len(...)`.
    """
    if isinstance(arg, ast.Call):
        func = arg.func
        if isinstance(func, ast.Name) and func.id == "len":
            return None
    if isinstance(arg, ast.Name):
        return arg.id if arg.id in FORBIDDEN_ARGS else None
    if isinstance(arg, ast.Attribute):
        if arg.attr in FORBIDDEN_ARGS:
            return arg.attr
        return _mentions_forbidden(arg.value)
    for child in ast.iter_child_nodes(arg):
        if isinstance(child, ast.expr):
            found = _mentions_forbidden(child)
            if found:
                return found
    return None


class TestNoPiiInLogs:
    def test_no_message_content_in_info_logs(self):
        """הסריקה שה-ROADMAP דורש: אין `msg.text` ודומיו בלוגי INFO."""
        offenders: list[str] = []
        for path in _production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, arg in _log_call_args(tree):
                bad = _mentions_forbidden(arg)
                rel = path.relative_to(ROOT)
                if bad and (str(rel), bad) not in ALLOWED:
                    offenders.append(f"{rel}:{lineno} — '{bad}'")
        assert not offenders, (
            "ערכים אסורים בלוגי INFO/WARNING:\n" + "\n".join(offenders)
        )

    def test_the_scan_actually_catches_something(self):
        """הסריקה עצמה נבדקת — טסט שלא יכול להיכשל אינו טסט."""
        tree = ast.parse("logger.info('שאלה: %s', msg.text)")
        args = _log_call_args(tree)
        assert args
        assert _mentions_forbidden(args[0][1]) == "text"

    def test_length_is_not_content(self):
        """`len(text)` מותר — אורך אינו תוכן, והוא מה שמדבג קציצה."""
        tree = ast.parse("logger.info('%d תווים', len(result.text))")
        assert _mentions_forbidden(_log_call_args(tree)[0][1]) is None

    def test_nested_content_is_still_caught(self):
        """עטיפה אינה מסתירה — `text[:50]` הוא עדיין תוכן."""
        tree = ast.parse("logger.info('%s', msg.text[:50])")
        assert _mentions_forbidden(_log_call_args(tree)[0][1]) == "text"
        tree = ast.parse("logger.info('%s', f'{answer}')")
        assert _mentions_forbidden(_log_call_args(tree)[0][1]) == "answer"


class TestRequiredEvents:
    """ארבעת האירועים שה-ROADMAP מחייב שיופיעו בלוג."""

    def test_llm_call_logs_model_duration_and_tokens(self, caplog, monkeypatch):
        import llm_client

        class _Choice:
            message = type("M", (), {"content": "שלום"})()
            finish_reason = "stop"

        class _Resp:
            choices = [_Choice()]
            usage = type("U", (), {"prompt_tokens": 120, "completion_tokens": 8})()

        monkeypatch.setattr(
            llm_client, "get_llm_provider_config", lambda: ("openai", "gpt-x", ""),
        )
        monkeypatch.setattr(
            llm_client, "get_openai_client",
            lambda: type("C", (), {
                "chat": type("Ch", (), {
                    "completions": type("Co", (), {
                        "create": staticmethod(lambda **kw: _Resp()),
                    })(),
                })(),
            })(),
        )
        with caplog.at_level(logging.INFO, logger="llm_client"):
            llm_client.chat_complete(
                [{"role": "user", "content": "היי"}], temperature=0.5, max_tokens=100,
            )

        line = next(r.getMessage() for r in caplog.records if "llm_call" in r.getMessage())
        assert "model=gpt-x" in line
        assert "prompt_tokens=120" in line
        assert "completion_tokens=8" in line
        assert "ms=" in line
        # התוכן עצמו לא נכנס
        assert "היי" not in line
        assert "שלום" not in line

    def test_llm_failure_is_logged_with_latency(self, caplog, monkeypatch):
        """‏latency של כשל הוא בדיוק מה שמעניין כשחושדים ב-timeout."""
        import llm_client

        monkeypatch.setattr(
            llm_client, "get_llm_provider_config", lambda: ("openai", "gpt-x", ""),
        )

        def _boom():
            raise RuntimeError("אין רשת")

        monkeypatch.setattr(llm_client, "get_openai_client", _boom)
        with caplog.at_level(logging.WARNING, logger="llm_client"):
            with pytest.raises(RuntimeError):
                llm_client.chat_complete([], temperature=0.5, max_tokens=10)

        line = next(r.getMessage() for r in caplog.records if "llm_call" in r.getMessage())
        assert "status=error" in line
        assert "error=RuntimeError" in line
        assert "ms=" in line
        # הודעת החריגה עצמה לא נכנסת — היא עלולה להחזיר את הבקשה
        assert "אין רשת" not in line

    def test_handoff_is_logged_without_the_question(
        self, default_tenant_db, caplog, monkeypatch,
    ):
        from core import message_processor as mp

        monkeypatch.setattr(
            mp, "generate_answer",
            lambda **kw: {"answer": "[HANDOFF] אבדוק", "kb_empty": False,
                          "kb_tokens": 1, "llm_failed": False},
        )
        with caplog.at_level(logging.INFO, logger="core.message_processor"):
            mp.process_incoming_message(
                "1", "כמה עולה ניתוח לייזר?", {"display_name": "דנה"},
            )

        line = next(r.getMessage() for r in caplog.records if "handoff " in r.getMessage())
        assert "streak=1" in line
        assert "escalate=False" in line
        assert "לייזר" not in line
        assert "דנה" not in line

    def test_rate_limit_hit_is_logged(self, caplog):
        import rate_limiter

        rate_limiter.reset_all()
        with caplog.at_level(logging.INFO, logger="rate_limiter"):
            for _ in range(60):
                if rate_limiter.check_rate_limit("1") is None:
                    rate_limiter.record_message("1")
        rate_limiter.reset_all()

        assert any("rate limit hit" in r.getMessage() for r in caplog.records)

    async def test_send_failure_is_logged_with_a_classification(
        self, tenant, caplog,
    ):
        """כשל שליחה בלי סיווג הוא כשל שאי אפשר לפעול לפיו."""
        from telegram.error import Forbidden

        from bot import dispatch

        bot = FakeBot(fail_send=Forbidden("bot lacks rights"))
        conn = {"connection_id": "conn-demo-0001", "user_chat_id": 900001}
        with caplog.at_level(logging.WARNING, logger="bot.dispatch"):
            with tenant_context("acme"):
                await dispatch.send_to_customer(
                    bot, 500042, "conn-demo-0001", "תשובה", "500042", "דנה", conn,
                )

        line = next(
            r.getMessage() for r in caplog.records if "כשל שליחה" in r.getMessage()
        )
        assert "reason=" in line
        # הסיווג הוא ערך מוכר ולא "אחר" גנרי
        assert "reason=no_permission" in line or "reason=window_closed" in line
        assert "תשובה" not in line


class TestSentry:
    def test_sentry_is_initialized_when_the_dsn_is_set(self, monkeypatch):
        """בלי DSN אין init — ובלי init אין דיווח חריגות בפרודקשן."""
        import main

        calls: list[dict] = []
        fake = type("S", (), {"init": staticmethod(lambda **kw: calls.append(kw))})
        monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", fake)

        import config as _cfg

        monkeypatch.setattr(_cfg, "SENTRY_DSN", "https://x@example.invalid/1", raising=False)
        main.init_sentry()
        assert calls, "‏Sentry לא אותחל למרות שה-DSN מוגדר"
        assert calls[0].get("send_default_pii") is False, (
            "‏send_default_pii חייב להיות כבוי — אחרת Sentry מצרף IP וזהות"
        )

    def test_no_sentry_without_a_dsn(self, monkeypatch):
        import main

        calls: list[dict] = []
        fake = type("S", (), {"init": staticmethod(lambda **kw: calls.append(kw))})
        monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", fake)

        import config as _cfg

        monkeypatch.setattr(_cfg, "SENTRY_DSN", "", raising=False)
        main.init_sentry()
        assert calls == []


class TestPhoneMasking:
    """טלפון בלוג — ממוסך. הסניטייזר קיים; כאן נבדק שהוא באמת מכסה."""

    @pytest.mark.parametrize(
        "raw",
        [
            "0501234567", "050-123-4567", "+972501234567",
            "03-1234567", "0731234567", "077-123-4567",
        ],
    )
    def test_phone_is_masked(self, raw):
        from utils.pii_sanitizer import sanitize_for_log

        masked = sanitize_for_log(f"הלקוח השאיר {raw}")
        assert raw not in masked

    def test_api_key_is_masked(self):
        from utils.pii_sanitizer import sanitize_for_log

        masked = sanitize_for_log("sk-proj-AbCdEf1234567890AbCdEf1234567890")
        assert "AbCdEf1234567890" not in masked


def test_ledger_and_db_writes_are_not_logged_with_user_ids(default_tenant_db):
    """`user_id` של לקוח קצה אינו נכנס ללוג — גם לא בעקיפין.

    ‏`delete_user_data` מדווח מונים, לא זהות. אם מישהו יוסיף את המזהה
    לשורת הלוג הזאת, מחיקה — הפעולה שכל תכליתה להפסיק להחזיק מידע —
    תשאיר את המזהה בקובץ הלוג.
    """
    import logging as _logging

    db.upsert_user("500042", "דנה", inbound=True)
    records: list[str] = []
    handler = type(
        "H", (_logging.Handler,),
        {"emit": lambda self, r: records.append(r.getMessage())},
    )()
    root = _logging.getLogger()
    root.addHandler(handler)
    try:
        db.delete_user_data("500042")
    finally:
        root.removeHandler(handler)

    assert not any("500042" in line for line in records), records
