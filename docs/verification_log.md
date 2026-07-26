# יומן אימות — V1–V5

> משימות האימות שהוגדרו ב-`Plan.md` §8 וב-`ROADMAP.md` שלב 1. הכלל:
> קוד שנשען על הנחה שטרם אומתה מפנה אליה בהערה, ולא בונים עליה שכבות
> נוספות עד שתאומת ותתועד כאן (‏CLAUDE.md → תהליך עבודה).
>
> **מקורות שנבדקו (25/07/2026):** ‏`core.telegram.org/bots/api`
> (‏Bot API 10.2 — הדף שנמשך בפועל ונסרק), ‏`core.telegram.org/bots/features`
> (הפרקים "Secretary Bots" ו-"Managed Bots"), ו-‏`python-telegram-bot` 22.8
> כפי שהותקן (‏`telegram.constants.BOT_API_VERSION == "10.0"`).
>
> **הבחנה חשובה בין שני סוגי ממצא:**
> - **מתועד** — נקבע חד-משמעית מהתיעוד הרשמי או מבדיקת הספרייה. אלה
>   ממצאים סגורים.
> - **ממתין לאימות אמפירי** — דורש חשבון טלגרם אמיתי עם בוט מחובר. לא
>   ניתן לביצוע בסביבת הפיתוח הזאת; מסומן במפורש ומה שנגזר ממנו בקוד
>   נכתב fail-safe.

---

## V1 — האם `setManagedBotAccessSettings` מדליק Secretary Mode לבוט-בן?

**סטטוס: מתועד — התשובה שלילית. ההנחה של `Plan.md` §1.6 נפלה.**

מה שהמתודה באמת עושה, מהתיעוד:

> **setManagedBotAccessSettings** — Use this method to change the access
> settings of a managed bot. Returns True on success.
> `user_id` (Integer, Yes) — User identifier of the managed bot whose access settings will be changed.
> `is_access_restricted` (Boolean, Yes) — Pass True if only selected users can access the bot. The bot's owner can always access it.
> `added_user_ids` (Array of Integer, Optional) — A JSON-serialized list of up to 10 identifiers of users who will have access to the bot in addition to its owner.

כלומר: **הגבלת גישה — מי מורשה להשתמש בבוט**. אין לזה שום קשר להדלקת
Secretary Mode. גם `BotAccessSettings` מכיל רק `is_access_restricted`
ו-`added_users`.

חיפוש שיטתי בכל 175 המתודות שה-SDK חושף (‏PTB 22.8, שממפה 1:1 ל-Bot API)
לא העלה שום מתודה להדלקת Secretary/Business Mode. התיעוד הרשמי אומר זאת
מפורשות בשלב הראשון של המדריך:

> Here is a quick start guide for allowing users to connect your bot to
> their accounts: **Enable Secretary Mode for your bot in @BotFather.**

**מסקנה מבצעית — ה-fallback של `Plan.md` §1.6 הופך למסלול היחיד.**
המשתמש היוצר הוא הבעלים של הבוט-הבן, ולכן הוא יכול להדליק Secretary Mode
ב-BotFather בעצמו. ה-onboarding בשלב 2 חייב לכלול שלב הדרכה מפורש:

1. הלקוח מאשר את יצירת הבוט בדיפ-לינק (בלחיצה).
2. אנחנו מושכים טוקן, רושמים webhook, ושולחים לו הוראה: לפתוח את
   ‏@BotFather ← ‏`/mybots` ← לבחור את הבוט ← ‏Bot Settings ← להדליק
   Secretary Mode.
3. רק אחר כך: הגדרות טלגרם ← ‏Chatbots ← בחירת הבוט.

זה מוסיף חיכוך אמיתי ל-onboarding שנמכר כ"לחיצה אחת", אבל אינו חוסם:
בלי זה הבוט פשוט לא ניתן לחיבור. **יש לעדכן את `Plan.md` §4.6 בהתאם**
ולסמן את השלב הזה כדורש-משתמש באשף.

מה שכן שימושי מ-`setManagedBotAccessSettings`: אפשר להגביל את הבוט-הבן
כך שרק בעל העסק יוכל לדבר איתו ישירות (`is_access_restricted=True`, בלי
`added_user_ids`). זה סוגר וקטור שבו זר מוצא את הבוט-הבן ב-t.me ומתחיל
איתו שיחה שאינה חלק מהערוץ. מומלץ להפעיל בשלב 2.

**אין מתודה למחיקת בוט מנוהל** (נבדק באותה סריקה — זו הייתה שאלה פתוחה
ב-ROADMAP T2.5). ה-offboarding נשאר: ניתוק webhook ⇒ `replaceManagedBotToken`
לנטרול הטוקן הפעיל ⇒ מחיקת הסוד ⇒ השעיית ה-tenant.

---

## V2 — נקודת החיבור למשתמש ללא Premium

**סטטוס: הדרישה בוטלה — מתועד. מיקום מסך החיבור — ממתין לאימות אמפירי.**

מ-changelog של Bot API 10.0 (8/5/2026):

> **Allowed Secretary Bots to manage accounts of users without a Telegram
> Premium subscription.**

זה סוגר את החלק המהותי של השאלה: קהל היעד הוא כל בעל עסק עם טלגרם, לא
רק מנויי Premium. מ-`bots/features`:

> Bots can enable Secretary Mode, allowing users to connect them to their
> account and allow it to process incoming messages and even respond on
> their behalf. The account owner can specify which chats your bot can
> access.

מה שנשאר פתוח: **המיקום המדויק של המסך באפליקציה** עבור משתמש חינמי.
בגרסאות הישנות המסך ישב תחת הגדרות ← ‏Telegram Business, שהיה נעול
ל-Premium. התיעוד לא מציין נתיב UI, וההנחה הסבירה היא הגדרות ← ‏Chatbots.
זה דורש מכשיר עם חשבון חינמי — **פעולה ידנית של בעל הפרויקט**.

עד לאימות: מדריך הלקוח (‏`docs/client_guide.md`, ‏T4.6) יכתב עם הנתיב
המשוער ובלי צילומי מסך, ויסומן כטעון אימות. שום קוד לא נשען על הממצא הזה.

---

## V3 — האם PTB 22.x חושף handler לעדכון `managed_bot`?

**סטטוס: מתועד — התשובה חיובית. אין צורך ב-TypeHandler.**

נבדק על הספרייה המותקנת (‏PTB 22.8):

```python
>>> from telegram.ext import ManagedBotUpdatedHandler   # קיים
>>> from telegram import ManagedBotUpdated, ManagedBotCreated, BotAccessSettings
>>> "managed_bot" in Update.__slots__                    # True
>>> ManagedBotUpdated.__slots__                          # ('bot', 'user')
```

`ManagedBotUpdated` מכיל בדיוק את מה שהתכנון הניח: `user` (המשתמש היוצר)
ו-`bot` (אובייקט הבוט החדש). ההתאמה ל-tenant נעשית לפי `user.id` —
בדיוק כפי ש-`Plan.md` §4.6 קבע (ולא לפי ה-username, שהמשתמש יכול לשנות).

המתודות הנלוות קיימות ב-`telegram.Bot`: ‏`get_managed_bot_token`,
`replace_managed_bot_token`, ‏`get_managed_bot_access_settings`,
`set_managed_bot_access_settings`.

פורמט הדיפ-לינק אושר מהתיעוד:

> `https://t.me/newbot/{manager_bot_username}/{new_username}?name={new_name}`

והזרימה: המשתמש מאשר ⇒ הבוט המנהל מקבל `managed_bot` update ⇒
`getManagedBotToken`.

---

## V4 — `sendMessageDraft` מעל business connection

**סטטוס: מתועד — לא נתמך מעל business connection. הדגל נשאר כבוי.**

חתימת המתודה מהתיעוד:

> **sendMessageDraft** — Use this method to stream a partial message to a
> user while the message is being generated. Note that the streamed draft
> is ephemeral and acts as a temporary 30-second preview — once the output
> is finalized, you must call `sendMessage` with the complete message to
> persist it in the user's chat.
> פרמטרים: `chat_id`, `message_thread_id`, `draft_id`, `text`,
> `parse_mode`, `entities`.

**אין פרמטר `business_connection_id`.** בלעדיו ההודעה יוצאת מהבוט ולא
מהחשבון של בעל העסק, וזה בדיוק מה שאסור בערוץ הזה. אותו דבר נכון
ל-`sendRichMessageDraft` (‏10.1).

לכן ה"הזרמה האנושית" מ-`Plan.md` §1.8 אינה ניתנת למימוש כפי שתוארה.
`HUMANIZED_DELIVERY` נשאר `false` ומתועד ככזה. אם בעתיד יתווסף הפרמטר —
זו תהיה בדיקה של שורה אחת.

**מה כן זמין לתחושת "אנושיות"** ושכן מקבל `business_connection_id`:
`sendChatAction` עם `typing`, ועיכוב פרופורציוני לאורך התשובה לפני
השליחה. זה מה שממומש בפועל ב-`dispatch_result` (‏T1.3).

---

## V5 — חלון 24 השעות: האם מושפע מהגדרות החיבור?

**סטטוס: מתועד — התשובה חיובית, והחלון מוגדר לפי הודעות נכנסות.**

ההגדרה המדויקת, מתוך `BusinessBotRights`:

> `can_reply` — True, if the bot can send and edit messages in the private
> chats that **had incoming messages in the last 24 hours**.

ומ-`bots/features`:

> **Depending on the connection settings**, your bot may also be able to
> send messages and do other actions on behalf of the account owner in
> chats that were **active in the last 24h**.

שלוש מסקנות מבצעיות:

1. **החלון נמדד מהודעה נכנסת** — לא מכל פעילות ולא מהודעה יוצאת. לכן
   `users.last_inbound_at` (שמתעדכן אך ורק ב-`upsert_user(inbound=True)`)
   הוא בדיוק המדד הנכון, וההימנעות מעדכונו בשליחה יוצאת נכונה.
2. **"בכפוף להגדרות החיבור" = ‏`rights.can_reply`.** אין שני מנגנונים
   נפרדים: אם הבעלים לא נתן את ההרשאה, אין שליחה בכלל; אם נתן, השליחה
   מוגבלת לחלון. שני ה-guards ב-`on_business_message` מכסים בדיוק את זה.
3. אותה מגבלה חלה על מתודות נוספות עם ניסוח מפורש —
   `readBusinessMessage`: "The chat must have been active in the last 24
   hours".

**מה שנשאר לאימות אמפירי:** נוסח השגיאה המדויק שחוזר מטלגרם כששולחים
לצ'אט שהחלון שלו נסגר. הסיווג ב-`dispatch_result` נכתב לכן על **סט
דפוסים** ולא על מחרוזת אחת, ועם ברירת מחדל בטוחה: כשל שאינו מזוהה
מסווג כ-`other`, נרשם ללוג המלא, ומתריע לבעלים — לא retry עיוור ולא
הודעה ללקוח. כשהשגיאה האמיתית תיראה בפרודקשן, מוסיפים דפוס אחד.

---

## סיכום למקבל ההחלטות

| # | ממצא | השפעה |
|---|---|---|
| V1 | **שלילי** — אין API להדלקת Secretary Mode | ‏onboarding מוסיף שלב ידני ב-BotFather. חיכוך, לא חסם |
| V2 | ‏Premium **אינו** נדרש (מתועד); מיקום המסך — לאימות ידני | קהל היעד רחב כמתוכנן |
| V3 | **חיובי** — ‏`ManagedBotUpdatedHandler` קיים ב-PTB 22.8 | שלב 2 נבנה עליו ישירות |
| V4 | **שלילי** — ‏`sendMessageDraft` בלי `business_connection_id` | הדגל נשאר כבוי; typing + עיכוב במקום |
| V5 | **חיובי** — החלון נמדד מהודעה נכנסת, וכפוף ל-`can_reply` | ‏`last_inbound_at` הוא המדד הנכון |

**שתי פעולות פתוחות שדורשות אדם עם חשבון טלגרם:** אימות מיקום מסך
החיבור למשתמש חינמי (‏V2), ולכידת נוסח השגיאה של חלון סגור (‏V5).
