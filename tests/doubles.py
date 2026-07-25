"""‏doubles משותפים לטסטים — `telegram.Bot` ו-`Context` בלי רשת.

מודול עזר ולא ‏conftest: הטסטים **יורשים** מ-`FakeBot` (‏FloodBot,
CountingBot), ומחלקה ב-conftest אינה מוזרקת לקובץ הטסט אלא רק
‏fixtures. וגם לא בתוך קובץ טסט: ייבוא בין קובצי טסט גורר את כל
ה-collection של הקובץ השני ומקשר את סדר ההרצה של השניים.
"""

class FakeBot:
    """‏double ל-`telegram.Bot` שמתעד קריאות במקום לבצע אותן."""

    def __init__(self, fail_send: Exception | None = None):
        self.messages: list[dict] = []
        self.actions: list[dict] = []
        self.fail_send = fail_send

    async def send_message(self, chat_id, text, business_connection_id=None, **kwargs):
        if self.fail_send is not None and business_connection_id is not None:
            raise self.fail_send
        self.messages.append({
            "chat_id": chat_id, "text": text,
            "business_connection_id": business_connection_id,
        })
        return None

    async def send_chat_action(self, chat_id, action, business_connection_id=None, **kwargs):
        self.actions.append({
            "chat_id": chat_id, "action": action,
            "business_connection_id": business_connection_id,
        })
        return None

    # ── עזרי בדיקה ──
    @property
    def customer_messages(self) -> list[dict]:
        """הודעות שיצאו ללקוח (עם business_connection_id)."""
        return [m for m in self.messages if m["business_connection_id"]]

    @property
    def owner_messages(self) -> list[dict]:
        """הודעות שיצאו לצ'אט הבעלים (בלי business_connection_id)."""
        return [m for m in self.messages if not m["business_connection_id"]]


class FakeContext:
    def __init__(self, bot):
        self.bot = bot
