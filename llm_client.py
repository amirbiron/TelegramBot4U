"""
LLM Client — שכבת dispatch אחת לכל קריאות הצ'אט אל ה-LLM.

הועתק מ-`ai-business-bot/llm_client.py` (‏ROADMAP T0.7). שני אתרי הקריאה
ב-`llm.py` (תשובה ללקוח, סיכום שיחה) עוברים דרך `chat_complete()`, שבוחרת
ספק לפי הגדרת ה-tenant הנוכחי:

  ''        → client בסגנון OpenAI (‏OPENAI_BASE_URL — ‏OpenAI או Gemini).
  'claude'  → client נפרד של Anthropic (‏Messages API).

ההיקף מכוון: רק צינור הצ'אט עובר ספק. סיווג הכוונות (‏intent.py) ממשיך
על ה-client בסגנון OpenAI.

בחירת הספק פר-tenant: סוד `anthropic_api_key` ב-control plane מדליק את
Claude ל-tenant הזה; אחרת ה-env של הפלטפורמה (‏LLM_PROVIDER) קובע.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

try:
    import anthropic
except ImportError:
    # החבילה לא מותקנת — נכשלים רק אם בפועל בוחרים Claude (לא מפיל boot)
    anthropic = None

from openai_client import get_openai_client

logger = logging.getLogger(__name__)

# מודל fallback כש-provider='claude' אבל לא נבחר מודל. ניתן לעקוף עם
# משתנה הסביבה ANTHROPIC_MODEL.
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5"

# מיפוי stop_reason של Anthropic לאוצר המילים של OpenAI שהקוראים ב-llm.py
# בודקים ("length" ⇒ קציצה ב-max_tokens, "stop" ⇒ סיום תקין).
_STOP_REASON_MAP = {"end_turn": "stop", "max_tokens": "length"}

_anthropic_client = None
_anthropic_lock = threading.Lock()


@dataclass(frozen=True)
class ChatResult:
    """תוצאת קריאת צ'אט מנורמלת בין ספקים."""

    text: str | None
    finish_reason: str
    model: str
    prompt_tokens: int = -1
    completion_tokens: int = -1


def is_anthropic_configured() -> bool:
    """האם מפתח Anthropic מוגדר בסביבה (ברמת הפלטפורמה)."""
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def get_anthropic_client():
    """Client של Anthropic — singleton עצל עם double-checked locking.

    נכשל בשימוש בלבד: חבילה חסרה / מפתח חסר ⇒ RuntimeError ברור, לא
    קריסת boot.
    """
    global _anthropic_client
    if _anthropic_client is None:
        with _anthropic_lock:
            if _anthropic_client is None:
                if anthropic is None:
                    raise RuntimeError(
                        "חבילת anthropic אינה מותקנת — pip install anthropic"
                    )
                if not is_anthropic_configured():
                    raise RuntimeError(
                        "ANTHROPIC_API_KEY לא מוגדר — לא ניתן להשתמש בספק Claude"
                    )
                _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def get_llm_provider_config() -> tuple[str, str]:
    """(ספק, מודל) ל-tenant הנוכחי.

    ‏tenant שיש לו סוד `anthropic_api_key` משלו — עובר ל-Claude. אחרת
    ברירת המחדל של הפלטפורמה מ-env (‏LLM_PROVIDER / ‏LLM_MODEL).
    כשל בקריאת ה-control plane לא מפיל את השיחה — נופלים לברירת המחדל.
    """
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    model = os.getenv("LLM_MODEL", "").strip()
    try:
        from tenancy import DEFAULT_TENANT, get_current_tenant

        tenant = get_current_tenant()
        if tenant != DEFAULT_TENANT:
            import control_plane as _cp

            if _cp.get_tenant_secret(tenant, "anthropic_api_key"):
                provider = "claude"
    except Exception:
        logger.error("get_llm_provider_config: כשל בקריאת הגדרת הספק", exc_info=True)
    return provider, model


def chat_complete(messages: list[dict], *, temperature: float, max_tokens: int) -> ChatResult:
    """קריאת צ'אט יחידה — הספק נבחר לפי הגדרת ה-tenant הנוכחי.

    חריגות לא נתפסות כאן (מלבד לוג בענף Claude) — הקוראים ב-llm.py
    עוטפים ב-try/except עם fallback מסודר.
    """
    provider, model = get_llm_provider_config()
    if provider == "claude":
        return _chat_complete_claude(messages, model=model, max_tokens=max_tokens)
    return _chat_complete_openai(messages, temperature=temperature, max_tokens=max_tokens)


def _get_openai_model() -> str:
    """המודל של ענף ה-OpenAI — נקרא דינמית מהמודול (מכבד patches בטסטים)."""
    import config as _cfg

    return getattr(_cfg, "OPENAI_MODEL", "gpt-4.1-mini")


def _chat_complete_openai(
    messages: list[dict], *, temperature: float, max_tokens: int
) -> ChatResult:
    """ענף ברירת המחדל — OpenAI או כל ספק תואם (Gemini דרך OPENAI_BASE_URL).

    ‏Gemini 2.5 הם "thinking models": הם צורכים thinking tokens פנימיים
    שנספרים אל max_tokens אבל לא נחשפים ב-completion_tokens, והתוצאה היא
    קציצה באמצע מילה. לבוט שירות לקוחות לא נדרש reasoning, ולכן מכבים
    (‏reasoning_effort="none" — הפרמטר הסטנדרטי שה-compat layer תומך בו).
    """
    client = get_openai_client()
    _model = _get_openai_model()
    extra_kwargs = {}
    if _model.startswith("gemini-2.5") or "thinking" in _model.lower():
        extra_kwargs["reasoning_effort"] = "none"
    response = client.chat.completions.create(
        model=_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **extra_kwargs,
    )
    choice = response.choices[0]
    try:
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else -1
        completion_tokens = usage.completion_tokens if usage else -1
    except Exception:
        prompt_tokens = -1
        completion_tokens = -1
    return ChatResult(
        text=choice.message.content,
        finish_reason=choice.finish_reason,
        model=_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _split_system_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """המרת רשימת הודעות בסגנון OpenAI למבנה של Anthropic Messages API.

    - כל הודעות ה-system מאוחדות למחרוזת אחת (הפרמטר system= נפרד).
    - הודעות עוקבות מאותו role ממוזגות — סינון הודעות fallback ב-
      `_build_messages` יכול להשאיר שתי הודעות user רצופות.
    - אם ההיסטוריה נפתחת ב-assistant (חלון שנחתך באמצע זוג) — מקדימים
      הודעת user קצרה, כי Anthropic דורש שהרשימה תתחיל ב-user.
    """
    system_parts: list[str] = []
    turns: list[dict] = []
    for msg in messages:
        role = msg.get("role", "")
        raw = msg.get("content")
        content = raw.strip() if isinstance(raw, str) else raw
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        mapped_role = "assistant" if role == "assistant" else "user"
        if turns and turns[-1]["role"] == mapped_role:
            turns[-1]["content"] += "\n\n" + content
        else:
            turns.append({"role": mapped_role, "content": content})

    if not turns:
        raise ValueError("_split_system_messages: אין הודעות user/assistant לשליחה")
    if turns[0]["role"] == "assistant":
        turns.insert(0, {"role": "user", "content": "(המשך שיחה קודמת)"})
    return "\n\n".join(system_parts), turns


def _chat_complete_claude(messages: list[dict], *, model: str, max_tokens: int) -> ChatResult:
    """ענף Claude — Anthropic Messages API."""
    client = get_anthropic_client()
    _model = model or os.getenv("ANTHROPIC_MODEL", "").strip() or ANTHROPIC_DEFAULT_MODEL
    system_text, claude_messages = _split_system_messages(messages)

    kwargs = {}
    if system_text:
        kwargs["system"] = system_text
    # בכוונה בלי temperature: חלק ממודלי Claude דוחים פרמטרי sampling
    # (או temperature לא-דיפולטי כש-thinking פעיל). כלל אחיד = בלי.
    if _model.startswith("claude-sonnet-5"):
        # thinking צורך tokens מתוך max_tokens ומאט תשובות — אותו סוג באג
        # כמו Gemini 2.5. לבוט שירות לקוחות מכבים.
        kwargs["thinking"] = {"type": "disabled"}

    try:
        resp = client.messages.create(
            model=_model, max_tokens=max_tokens, messages=claude_messages, **kwargs,
        )
    except anthropic.APIError as e:
        # base class של ה-SDK — לוג עם הקשר ואז re-raise אל ה-fallback
        # של הקורא ב-llm.py
        logger.error("Anthropic API error (model=%s): %s", _model, e)
        raise

    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    if not text:
        # refusal / תשובה ריקה — מרימים כדי שהקורא ייפול ל-fallback המסודר
        # שלו במקום לשלוח הודעה ריקה ללקוח
        raise RuntimeError(f"Claude החזיר טקסט ריק (stop_reason={resp.stop_reason!r})")

    usage = getattr(resp, "usage", None)
    return ChatResult(
        text=text,
        finish_reason=_STOP_REASON_MAP.get(resp.stop_reason, resp.stop_reason or ""),
        model=_model,
        prompt_tokens=getattr(usage, "input_tokens", -1) if usage else -1,
        completion_tokens=getattr(usage, "output_tokens", -1) if usage else -1,
    )
