# ‏fixtures של עדכוני Business

ה-JSONים כאן הם ארבעת סוגי עדכוני ה-Business כפי שטלגרם שולחת אותם.
הם **מנוקים מ-PII**: המזהים מומצאים, השמות גנריים, ותוכן ההודעות נוסח
מחדש.

## מה מבוסס על מה

הקבצים נבנו לפי המבנה שמתועד ב-`core.telegram.org/bots/api` (‏Bot API
10.2) ומאומת מול `telegram.Update.de_json` של ‏python-telegram-bot 22.8 —
טסט (`test_business_handlers.py::TestFixtures`) מוודא שכל fixture נפרס
לאובייקט תקין עם השדות הצפויים. אם טלגרם תשנה מבנה, הטסט ייפול.

**מה שעדיין לא נלכד:** הקלטה של תעבורה אמיתית מבוט מחובר. משימת
ה-fixtures ב-ROADMAP (‏T1.5) מבקשת גם את זה, והיא מסומנת כטעונת השלמה
ב-`docs/verification_log.md` — היא דורשת חשבון טלגרם עם Secretary Mode
פעיל. ההבדל המעשי היחיד שצפוי הוא שדות אופציונליים נוספים שאיננו
קוראים ממילא.

## הקבצים

| קובץ | סוג | מה הוא מדגים |
|---|---|---|
| `business_connection.json` | `business_connection` | חיבור חדש עם `can_reply=true` |
| `business_connection_revoked.json` | `business_connection` | ניתוק (`is_enabled=false`) |
| `business_message_customer.json` | `business_message` | הודעת לקוח רגילה |
| `business_message_owner.json` | `business_message` | הבעלים ענה בעצמו — הטריגר להשתקה |
| `business_message_offline.json` | `business_message` | הודעה אוטומטית של טלגרם (`is_from_offline`) — **אסור** לפרש כהתערבות |
| `business_message_media.json` | `business_message` | תמונה בלי טקסט |
| `edited_business_message.json` | `edited_business_message` | הלקוח תיקן הודעה |
| `deleted_business_messages.json` | `deleted_business_messages` | הלקוח מחק שתי הודעות |
