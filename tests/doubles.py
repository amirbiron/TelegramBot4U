"""‏doubles משותפים לטסטים — `telegram.Bot` ו-`Context` בלי רשת.

מודול עזר ולא ‏conftest: הטסטים **יורשים** מ-`FakeBot` (‏FloodBot,
CountingBot), ומחלקה ב-conftest אינה מוזרקת לקובץ הטסט אלא רק
‏fixtures. וגם לא בתוך קובץ טסט: ייבוא בין קובצי טסט גורר את כל
ה-collection של הקובץ השני ומקשר את סדר ההרצה של השניים.
"""

class FakeBot:
    """‏double ל-`telegram.Bot` שמתעד קריאות במקום לבצע אותן."""

    def __init__(self, fail_send: Exception | None = None, fail_owner_send: bool = False):
        self.messages: list[dict] = []
        self.actions: list[dict] = []
        self.fail_send = fail_send
        self.fail_owner_send = fail_owner_send
        # ‏message_id עוקב — ‏`Bot.send_message` האמיתי מחזיר `Message`,
        # ו-`owner_channel` נשען על ה-message_id שחזר כדי למפות התראה
        # ללקוח. double שמחזיר None היה מסתיר את הנתיב הזה.
        self._next_message_id = 1000

    async def send_message(self, chat_id, text, business_connection_id=None, **kwargs):
        # ‏`fail_send` חל על שליחה **ללקוח** בלבד (זו שנושאת
        # connection_id). ההתראה לבעלים היא נתיב נפרד וחייבת להמשיך
        # לעבוד — היא בדיוק מה שהטסטים האלה בודקים.
        if self.fail_send is not None and business_connection_id is not None:
            raise self.fail_send
        if self.fail_owner_send and business_connection_id is None:
            raise RuntimeError("שליחה לצ'אט הבעלים נכשלה")
        self._next_message_id += 1
        self.messages.append({
            "chat_id": chat_id, "text": text,
            "business_connection_id": business_connection_id,
            "message_id": self._next_message_id,
        })
        # ‏`Message` אמיתי נושא גם `chat` — ו-`owner_channel` נשען עליו
        # כדי לרשום את חצי המפתח השני של מיפוי ההתראות.
        return type(
            "SentMessage", (),
            {
                "message_id": self._next_message_id,
                "chat": type("Chat", (), {"id": chat_id})(),
            },
        )()

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
