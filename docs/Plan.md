# PLAN — בוט Secretary לטלגרם: מענה בשם בעל העסק, בוט לכל לקוח

> **סטטוס:** גרסה 2 (25/07/2026), מחליפה את מסמך התכנון הקודם. שלוש הכרעות התקבלו מאז הגרסה הקודמת: (1) **מודל B** — בוט נפרד לכל לקוח, על גבי Managed Bots של Bot API 9.6, במקום בוט פלטפורמה משותף; (2) **ביטול צינור ה-RAG** — בסיס הידע מוזרק לפרומפט במלואו, עם תפר מוכן ל-retrieval עתידי; (3) עדכון עובדתי מול התיעוד הרשמי: הפיצ'ר נקרא היום **Secretary Mode**, אינו דורש Premium מבעל החשבון, וכפוף ל**חלון פעילות של 24 שעות** בשליחה.
>
> **מקורות:** חקירת הריפו `ai-business-bot` (branch main, יולי 2026); core.telegram.org — ‏bots/api, ‏bots/features, ‏api-changelog (נבדקו 25/07/2026, ‏Bot API 10.2); ‏python-telegram-bot 22.8.
>
> **מה בונים:** בוט שבעל עסק מחבר לחשבון הטלגרם האישי שלו. הבוט קורא הודעות DM נכנסות ועונה עליהן בשם בעל החשבון, מתוך בסיס ידע שמנוהל בפאנל אדמין — ב-multi-tenant מלא, כשכל לקוח מקבל בוט משלו שנוצר בלחיצה אחת.

---

## 0. תקציר מנהלים

רוב הפרויקט כבר כתוב בריפו `ai-business-bot`: בידוד multi-tenant ברמת קובץ, control plane עם סודות מוצפנים, פאנל ניהול בסיס ידע, ליבת עיבוד הודעות ערוץ-אגנוסטית, ותשתית ניהול בוטים מרובים (`bot_registry`). כל אלה מועתקים כמעט אחד-לאחד.

מה שחדש מצטמצם לשלושה רכיבים: **מתאם ערוץ Secretary** (handlers לעדכוני Business), **בוט מנהל** שמייצר בוטים-בנים ללקוחות דרך Managed Bots בלי BotFather, ו-**kb_service** שמחליף את צינור ה-RAG בהזרקת הקשר מלא. בנוסף: היפוך שקט של מנגנון ה-live chat (הבעלים עונה — הבוט שותק, בלי הכרזות).

שלוש החלטות מוצר סגורות: בוט לכל לקוח (סעיף 2); הקשר מלא במקום retrieval (סעיף 3.2); שקיפות מחייבת — הבוט לא מכחיש שהוא אוטומציה כשנשאל ישירות (סעיף 4.4).

---

## 1. הפיצ'ר — Secretary Mode, מעודכן ל-Bot API 10.2

### 1.1 שמות ותנאים מוקדמים

מה שהושק ב-Bot API 7.2 (מרץ 2024) כ-"Telegram Business bots" נקרא היום בתיעוד **Secretary Mode**. ההגדרה אצל הבוט: ‏BotFather ← ‏`/mybots` ← ‏Bot Settings ← הפעלת Secretary Mode. בלי זה הבוט לא ניתן לחיבור.

**בעל החשבון לא צריך Premium.** זו הייתה דרישה עד Bot API 10.0 (8/5/2026), שבו הותר ל-Secretary Bots לנהל חשבונות של משתמשים ללא מנוי. המשמעות המסחרית: קהל היעד הוא כל בעל עסק עם טלגרם, לא רק מנויי Premium — וזה משפיע על תמחור (אי אפשר להניח שהלקוח כבר רגיל לשלם לטלגרם). נקודה אחת לאימות בפועל: מסך החיבור הישן ישב תחת הגדרות ← Telegram Business, שהיה נעול ל-Premium; צריך לוודא איפה משתמש חינמי מחבר בוט היום (משימת אימות V2 ב-ROADMAP).

בעת החיבור המשתמש בוחר אילו צ'אטים חשופים לבוט (הכול / קטגוריות / רשימה ידנית) ואילו הרשאות הבוט מקבל.

### 1.2 סוגי העדכונים

| עדכון | מתי מגיע | מה עושים איתו |
|---|---|---|
| `business_connection` | חיבור, ניתוק, או עריכת הרשאות | Upsert של רשומת החיבור; אימות שהמחבר הוא הבעלים הרשום של ה-tenant |
| `business_message` | הודעה נכנסת בצ'אט מחובר — כולל הודעות שבעל החשבון עצמו כותב | ליבת הזרימה: מענה, או זיהוי התערבות בעלים |
| `edited_business_message` | הודעה נערכה | עדכון העותק השמור |
| `deleted_business_messages` | הודעות נמחקו | מחיקת העותקים אצלנו — חובת פרטיות, לא אופציה |

שלוש עובדות שהתיעוד מדייק והגרסה הקודמת של המסמך פספסה:

1. **אין הד-עצמי.** בתוך הצ'אטים המורשים הבוט מקבל את כל העדכונים **פרט להודעות ששלח בעצמו ולהודעות של בוטים אחרים**. כלומר תשובה שהבוט שלח בשם הבעלים לא חוזרת אליו כ-`business_message`, ואין סכנה שהוא "ישתיק את עצמו" אחרי כל מענה.
2. **הודעות הבעלים כן מגיעות** — כשבעל החשבון עונה ללקוח בעצמו, הבוט מקבל `business_message` עם `from.id == owner`. זה הטריגר להשתקה השקטה (סעיף 4.3).
3. **הודעות אוטומטיות של טלגרם עצמה** (greeting / away / מתוזמנות) גם יוצאות "מהבעלים" — ומסומנות ב-`is_from_offline=true`. סינון לפי `from.id` בלבד יפרש אותן כהתערבות אנושית וישתיק את הבוט בטעות. חובה לסנן גם לפי הדגל הזה.

אובייקט `BusinessConnection` מכיל: `id` (נצרף לכל קריאה יוצאת), `user` (הבעלים), `user_chat_id` (צ'אט ישיר בוט↔בעלים — ערוץ הניהול, סעיף 4.5), `is_enabled`, ו-`rights` מסוג `BusinessBotRights` (החליף את `can_reply` הבודד ב-9.0). לפני כל תשובה בודקים `rights.can_reply`.

### 1.3 שליחה בשם המשתמש

כל מתודה יוצאת (`sendMessage`, `editMessageText`, `deleteMessage`, `sendChatAction`...) מקבלת `business_connection_id`. עם הפרמטר — ההודעה יוצאת מהחשבון האישי של הבעלים; בלעדיו — מהבוט (ובצ'אט של לקוח זר זה נכשל). ב-python-telegram-bot ‏`update.business_message.reply_text(...)` מעביר את המזהה אוטומטית.

### 1.4 חלון 24 השעות — המגבלה שמעצבת את המוצר

לפי התיעוד, שליחה ופעולות בשם הבעלים אפשריות רק בצ'אטים **שהיו פעילים ב-24 השעות האחרונות** (בכפוף להגדרות החיבור). זה אותו מודל כמו חלון השירות של WhatsApp Business, ויש לו שלוש השלכות:

1. **אין יוזמות בערוץ הזה.** תזכורת לפני תור, פולו-אפ לליד שלא ענה, ברכת חג — כל אלה חסומים ברמת ה-API אם הצ'אט לא התעורר ביממה האחרונה. מנגנוני ה-followup של הריפו הקיים לא "מחוץ ל-scope" — הם בלתי אפשריים כאן. יוזמות, אם יידרשו, יעברו בערוץ אחר (הבוט-הבן כבוט רגיל מול לקוחות שעשו לו `/start`, או WhatsApp).
2. **downtime הוא אובדן, לא עיכוב.** אם השרת שכב סופ"ש ולקוח כתב ביום שישי, ביום ראשון ייתכן שאי אפשר לענות לו בשם הבעלים. הטיפול: זיהוי כשל השליחה, סימון השיחה כ-"חלון סגור", והתראה לבעלים בערוץ הניהול — לא retry עיוור.
3. **מעקב חלון.** שומרים `last_inbound_at` פר-צ'אט כדי להציג בפאנל אילו שיחות עדיין ניתנות למענה.

הניסוח "בכפוף להגדרות החיבור" בתיעוד מרמז שייתכן שהחלון תלוי בהרשאות שהמשתמש בחר — משימת אימות אמפירית (V5).

### 1.5 מה אין: `getBusinessAccountChats`

אין ב-Bot API מתודה לשליפת רשימת הצ'אטים של המשתמש. קיימת רק `getBusinessConnection` (פרטי חיבור בודד). המשמעות: אין ייבוא היסטוריה ואין רשימת צ'אטים התחלתית — הבוט מכיר צ'אט מהרגע שהגיעה בו הודעה אחרי החיבור. "רשימת השיחות" בפאנל נבנית אצלנו, הודעה-אחרי-הודעה, בטבלת `conversations` הקיימת.

### 1.6 ‏Managed Bots (Bot API 9.6, ‏3/4/2026) — הבסיס למודל B

בוט שהדליק Bot Management Mode (ב-BotFather, ‏`can_manage_bots=true`) יכול ליצור ולשלוט בבוטים-בנים בשם משתמשים, בלי שהמשתמש נוגע ב-BotFather:

1. שולחים למשתמש דיפ-לינק: `https://t.me/newbot/{manager_bot}/{suggested_username}?name={display_name}`.
2. טלגרם מציגה מסך יצירה ממולא מראש; המשתמש מאשר בלחיצה.
3. הבוט המנהל מקבל עדכון `managed_bot` (‏`ManagedBotUpdated` — מכיל את המשתמש היוצר ואת אובייקט הבוט החדש).
4. המנהל קורא `getManagedBotToken` ומקבל טוקן מלא; מכאן הוא מפעיל את ה-Bot API הרגיל בשם הבן (‏`setWebhook`, ‏`setMyCommands`...). ‏`replaceManagedBotToken` קיים לרוטציה.

ב-Bot API 10.0 נוספו `BotAccessSettings` והמתודות `getManagedBotAccessSettings` / `setManagedBotAccessSettings`. **הנחת התכנון:** דרכן מדליקים Secretary Mode לבוט-בן פרוגרמטית. זו ההנחה הקריטית ביותר במסמך ולכן היא משימת האימות הראשונה (V1). ‏fallback אם היא נופלת: המשתמש היוצר הוא הבעלים של הבוט-הבן, ולכן יכול להדליק Secretary Mode ידנית ב-BotFather — חיכוך, אבל לא חסימה.

שתי מגבלות מתועדות: **אין למשתמש UI מובנה לביטול בוט מנוהל** — חובה עלינו לממש פקודת offboarding מפורשת (סעיף 4.6), אחרת יש פער בזכות המחיקה; ו-username של בוט חייב להסתיים ב-`bot` — משפיע על מחולל השמות המוצעים.

### 1.7 תמיכת ספריות

| רכיב | דרישה | הערות |
|---|---|---|
| Bot API | ‏9.6+ (managed), ‏10.0+ (ללא Premium, access settings) | |
| python-telegram-bot | לנעול `>=22.0` (בפועל 22.8, תומך Bot API 10.0) | ‏business handlers קיימים מ-21.1: ‏`BusinessConnectionHandler`, ‏`BusinessMessagesDeletedHandler`, ‏`filters.UpdateType.BUSINESS_MESSAGE` וחברים, העברת `business_connection_id` אוטומטית ב-shortcuts. **לאמת (V3):** האם קיים handler ייעודי לעדכון `managed_bot`; אם לא — ‏`TypeHandler` על ה-update הגולמי |

### 1.8 מגבלות ועקרונות ערוץ

אין `/start` ואין מקלדות מול הלקוח הסופי — הוא מדבר עם "אדם", וכפתורים מסגירים אוטומציה. כל זרימות ה-UX מבוססות-הכפתורים של הריפו הקיים לא עוברות. טקסט הוא הפורמט; ‏`sendChatAction` (typing) עם `business_connection_id` — כן.

**הזרמה אנושית (אופציה, feature flag):** ‏`sendMessageDraft` (תחילת 2026) ו-`sendRichMessageDraft` ‏(10.1) נועדו בדיוק להזרמת תשובות AI בהדרגה. בערוץ שכל מטרתו להרגיש אנושי, הודעה של 400 תווים שנוחתת שלמה אחרי 0.8 שניות היא מה שמסגיר; חשיפה הדרגתית + השהיה פרופורציונלית לאורך נקראת כהקלדה. לא אומת שזה עובד מעל business connection — משימת אימות V4, ומאחורי דגל כבוי כברירת מחדל.

---

## 2. ההכרעה: מודל B — בוט לכל לקוח

הגרסה הקודמת של המסמך המליצה על מודל A (בוט משותף) מנימוק אחד מרכזי: לא לכפות על כל בעל עסק ליצור בוט ב-BotFather ולהעביר טוקן. ‏Managed Bots מחק את הנימוק הזה — ה-onboarding של מודל B הפך ללחיצה אחת. ההכרעה מתהפכת.

| | מודל A — בוט משותף | **מודל B — בוט לכל לקוח (נבחר)** |
|---|---|---|
| Onboarding | הדבקת username בהגדרות | לחיצה על דיפ-לינק ← אישור ← חיבור בהגדרות |
| זיהוי tenant | טבלת מיפוי לפי `business_connection_id` | לפי route של ה-webhook — התשתית קיימת (`tenant_routes` + ‏`bot_registry`) |
| טוקנים וסיכון | טוקן אחד; תקלה/חסימה = כל הלקוחות | בידוד מלא; תקרת ~30 הודעות/שנייה **פר-בוט** |
| ניתוק לקוח | מחיקת שורת מיפוי | ניתוק webhook + נטרול טוקן — ניתוק אמיתי |
| ערוץ ניהול לבעלים | צ'אט עם בוט הפלטפורמה | צ'אט עם הבוט **שלו** — ממותג בשם העסק |

סיכונים ייחודיים למודל B ומעניהם: התנגשות `suggested_username` (המשתמש עשוי לערוך אם תפוס) — ההתאמה ל-tenant נעשית לפי המשתמש היוצר, לא לפי השם (סעיף 4.6); אין revocation UI — פקודת offboarding מפורשת; תלות בהנחת V1 — עם fallback ידני מתועד.

---

## 3. מפת מיחזור מהריפו הקיים

מקרא: ✅ מעתיקים כמו-שהוא · 🔧 מעתיקים עם התאמות · 🆕 חדש · ❌ לא עובר

### 3.1 תשתית multi-tenant — ✅ הנכס הגדול

| מקור | הערות |
|---|---|
| `tenancy.py` | ✅ מילה במילה. ‏contextvar, ולידציית slug, ‏`tenant_db_path()`, ‏`TENANCY_STRICT` |
| `control_plane.py` | 🔧 לקצץ `ROUTE_TYPES` ו-`KNOWN_SECRET_NAMES` לסט טלגרם; להוסיף `managed_bots`, ‏`business_connections`, ‏`pairing_codes` (סעיף 5.1) |
| `database.py:get_connection` | ✅ חיבור-לכל-פעולה לפי `tenant_db_path()`. אין עמודת tenant_id בשום טבלה — הבידוד הוא קובץ-לכל-tenant |
| `migrations.py` | ✅ המנגנון (`_ensure_column` + ריצה לינארית); זורקים את המיגרציות ההיסטוריות |
| `migrate_all_tenants()` בעליית תהליך | ✅ חובה, אחרת סכימת קבצים-פר-tenant נרקבת בשקט |
| `bot_registry.py` | ✅ עולה מ"אופציונלי" ל**ליבה**: ‏Application פר-tenant על event loop משותף, בנייה עצלה, לעולם לא נופלים לטוקן של tenant אחר |
| `backup_service.py` + ‏`platform_maintenance.py` | ✅ |
| `utils/crypto.py` | ✅ ‏Fernet עם prefix גרסה, ‏fail-closed |
| `platform_cli.py` | 🔧 פקודות לערוץ החדש |

### 3.2 בסיס ידע — השינוי הגדול: בלי RAG

ההחלטה: **בסיס הידע מוזרק לפרומפט במלואו.** ‏KB של קליניקה/מטפלת/קוסמטיקאית הוא 20–100 רשומות, ריאלית עד ~30K טוקנים — נכנס בשלמותו בחלונות ההקשר של היום. ‏retrieval היה מנגנון דחיסה לעידן של חלונות 8K, והוא נכשל בדיוק בשאלות שדורשות שתי רשומות ("יש חניה? ואתם פתוחים בשישי?" — ‏top-k מחזיר חמישה קטעים על חניה ואפס על שעות). הקשר מלא לא מפספס, ועריכה בפאנל נכנסת לתוקף בהודעה הבאה בלי rebuild.

| רכיב | גורל |
|---|---|
| `kb_entries` + ‏CRUD | ✅ ללא שינוי — הפאנל לא מרגיש בהבדל |
| `unanswered_questions` | ✅ עם שינוי טריגר: הרישום עובר מ-`chunks_used == 0` (אין chunks יותר) לזיהוי `[HANDOFF]` |
| `rag/` — ‏chunker, embeddings, vector_store, engine | ❌ ארבעת המודולים נמחקים |
| `kb_chunks`, קבצי FAISS, דגל `.stale`, מנעול בין-תהליכי, ‏LRU registry | ❌ נעלמים, ואיתם הדרישה לדיסק קבוע עבורם |
| `/kb/rebuild` בפאנל | ❌ מוסר. ‏`/kb/search` הופך לחיפוש SQL (‏LIKE) |
| `kb_service.py` | 🆕 ראו למטה |

**‏`kb_service.get_kb_context(tenant)` — התפר.** מחזיר את כל הרשומות הפעילות, מקובצות לפי קטגוריה, בפורמט שהיה של `format_context`. ‏cache פשוט בזיכרון פר-tenant, מפתח לפי `MAX(updated_at)` של `kb_entries` — שאילתה אחת זולה בכל הודעה, ועדכון בפאנל מתבטא מיידית. הפונקציה גם מחזירה אומדן טוקנים; מעל ~50K הפאנל מציג אזהרה ("בסיס הידע גדול — שקול פיצול"). החוזה קבוע: אם יום אחד tenant יחצה את הסף, מימוש retrieval נכנס **מאחורי אותה פונקציה** בלי לגעת באף קורא. אפס תשתית עכשיו, אפס refactor אז.

**עלות:** כל הודעה נושאת את ה-KB. עם prompt caching (נתמך ב-OpenAI וב-Anthropic) החלק הקבוע נחתך בכ-90%; התנאי הארכיטקטוני — סדר הפרומפט בסעיף 3.3.

### 3.3 ליבת עיבוד ו-LLM — 🔧

| מקור | מה עובר | מה משתנה |
|---|---|---|
| `core/message_processor.py` | הצינור הערוץ-אגנוסטי; מנגנון `[HANDOFF]` (‏startswith דטרמיניסטי בלבד; ‏`strip_handoff_marker` תמיד); הסלמת שלוש-פסילות | 🔧 ‏`channel="telegram_business"`; נטרול ענפי booking/כפתורים; ההסלמה השלישית לא שולחת "מעביר לנציג" אלא handoff שקט לבעלים (4.5); רישום unanswered על HANDOFF |
| `llm.py` | בניית ההודעות, זיכרון לקוח מסונן מפני prompt injection, סיכומי שיחה | 🔧 החלפת בלוק ה-context של RAG ב-`get_kb_context()`; **סדר פרומפט מחייב ל-prompt caching**, מהיציב לתנודתי: ‏[תבנית פרסונה] ← ‏[KB מלא] ← ‏[הגדרות tenant] ← ‏[זיכרון לקוח + סיכום שיחה] ← ‏[היסטוריה] ← ‏[שאלה]. ‏caching עובד רק על prefix יציב — הפרה של הסדר מוחקת את החיסכון |
| `llm_client.py` + ‏`openai_client.py` | ✅ בורר ספק פר-tenant, נרמול stop reasons | |
| `config.py:build_system_prompt` | ✅ מקבל `business_name` ו-`channel` | 🔧 ענף `telegram_business` — פרסונה (4.4) |
| `intent.py` | ✅ המנגנון ההיברידי | 🔧 צמצום מרחב הכוונות (בלי booking בשלב 1); ברכות אוטומטיות מוחלפות במעבר דרך ה-LLM — "ברוכים הבאים! 👋" מסגיר בוט |
| `rate_limiter.py` | ✅ חלונות הזזה, מפתח `(tenant, user)`, הפרדת check מ-record | 🔧 בלי דקורטורים — קוד ליניארי ב-handler (4.2); חריגה = שתיקה ללקוח + התראה לבעלים, לא הודעת מערכת |
| `live_chat_service.py` | ‏session ב-DB, ‏timeout ‏120 דק' | 🔧 ההיפוך המרכזי — 4.3 |
| זיכרון לקוחות (`memory/`, ‏`customer_facts`) | ✅ מומלץ — "העוזר של דנה" שזוכר שהלקוח אלרגי ללטקס זה המוצר | |
| סיכום שיחות | ✅ | |

### 3.4 פאנל אדמין — ✅ חבילת ה-KB עוברת נקייה

עוברים כמו-שהם: שש routes של KB (מינוס rebuild) + פערי ידע; התבניות `kb_list` / ‏`kb_form` / ‏`knowledge_gaps` / ‏`login` + ‏partials; ‏`style.css` (RTL, ‏4 ערכות); פילטרי Jinja; התחברות דו-מסלולית + ‏CSRF (כולל HTMX) + ‏audit log; קשירת ה-tenant ב-`before_request` (contextvar + ניתוק tenant מושעה) — זה מה שהופך את הפאנל לפר-tenant בלי לגעת ב-routes.

נבנה מחדש: ה-sidebar (שלד נשאר, ניווט מוחלף). מסכים חדשים: **אשף לקוח חדש** (יצירת tenant ← לינק צימוד ← סטטוסים חיים של שלבי ה-onboarding), **"הבוט שלי"** (סטטוס בוט-בן: נוצר / Secretary פעיל / מחובר / הרשאות / התראת חלון), **שיחות** בגרסה שמראה מי ענה (בוט / בעלים), ומתג autopilot פר-שיחה.

### 3.5 פרטיות ותאימות — ✅ קריטי כאן אף יותר

‏`delete_user_data` + ‏`purge_old_data` (retention), ‏`consent_ledger`, ‏`pii_sanitizer`, ותבנית `privacy_data_matrix` — עוברים. הבוט קורא DM אישיים; המטריצה נפתחת מהיום הראשון וכל טבלה חדשה = שורה בה באותו commit. תוספת של מודל B: מחיקת בוט-בן היא חלק מזכות המחיקה של ה-tenant (סעיף 4.6).

### 3.6 מה לא עובר — ❌

תורים על כל חלקיהם, שידורים/קמפיינים, הפניות, צנרת Twilio/Meta, ‏widget, מצב חופשה, מסך ההסכמה מבוסס-כפתורים, מערך המקלדות והניתוב לפי טקסט כפתור, וכעת גם: כל צנרת ה-RAG, ומנגנוני followup יזומים — האחרונים לא כהחלטת scope אלא כמגבלת API (סעיף 1.4).

### 3.7 הפער שנשאר בעינו: 66 נקודות `update.message`

הממצא מהגרסה הקודמת תקף: ‏`bot/handlers.py` הקיים שבור תחת `business_message` (‏`update.message is None` ⇒ בליעות שקטות, אובדן נתונים, קריסות). לא מתקנים 66 נקודות — כותבים נתיב Business נקי אחד (4.2). ‏`effective_user` / ‏`effective_chat` / ‏`effective_message` כן מכסים עדכוני Business, ולכן הצינור הפנימי עובר ללא שינוי.

---

## 4. הארכיטקטורה של החלק החדש

### 4.1 מבנה הריפו

```
new-repo/
├── tenancy.py                  ← העתקה
├── control_plane.py            ← העתקה + managed_bots / business_connections / pairing_codes
├── database.py                 ← תת-קבוצה: kb_entries, conversations, summaries, users,
│                                  consent, unanswered, live_chats, customer_facts
├── migrations.py               ← המנגנון בלבד
├── config.py                   ← מקוצץ; build_system_prompt עם ענף telegram_business
├── kb_service.py               ← 🆕 get_kb_context + cache + אומדן טוקנים (3.2)
├── llm.py / llm_client.py / openai_client.py / intent.py / rate_limiter.py  ← העתקה
├── core/message_processor.py   ← העתקה מנוקה
├── bot/
│   ├── manager_bot.py          ← 🆕 הבוט המנהל: צימוד, יצירת בנים, managed_bot updates
│   ├── business_bot.py         ← 🆕 בניית Application לבן + רישום handlers
│   └── business_handlers.py    ← 🆕 הלב (4.2)
├── services/
│   ├── takeover_service.py     ← live_chat_service בהיפוך שקט (4.3)
│   └── owner_channel.py        ← 🆕 הודעות לבעלים בצ'אט הבוט-הבן (4.5)
├── admin/                      ← חבילת KB + login + platform + מסכי onboarding (3.4)
├── utils/ (crypto, consent_ledger, pii_sanitizer, dates)  ← העתקה
├── memory/                     ← העתקה
├── bot_registry.py / backup_service.py / platform_maintenance.py / main.py ← העתקה מותאמת
└── tests/                      ← conftest + טסטים של המודולים שהועתקו + fixtures של עדכוני Business
```

### 4.2 נתיב הודעה נכנסת — handler אחד, ליניארי

נקודת כניסה אחת להודעות לקוח, ולכן ה-guards הם קוד ליניארי קריא ולא דקורטורים (התקדים: ה-webhook של WhatsApp בריפו הקיים). ה-tenant נפתר **לפי ה-route של הבן** (`/telegram/webhook/t/<key>`), עוד לפני שנוגעים בתוכן:

```python
async def on_business_message(update, context):
    msg = update.business_message
    # tenant כבר נקבע ע"י ה-route; conn נטען מה-control plane לפי connection_id
    conn = get_connection_for_tenant(msg.business_connection_id)
    if conn is None or not conn.is_enabled:
        log_unknown_connection(msg); return          # הגנת cross-wiring: connection לא רשום ל-tenant הזה

    is_owner_human = (
        msg.from_user and msg.from_user.id == conn.owner_user_id
        and not msg.is_from_offline                   # greeting/away/מתוזמן של טלגרם ≠ התערבות
        and msg.sender_business_bot is None           # הגנת עומק; הודעות-עצמי ממילא לא מגיעות (1.2)
    )
    if is_owner_human:
        takeover_service.on_owner_message(msg)        # השתקה שקטה + חידוש timeout
        save_message(msg, role="assistant", authored_by="owner")
        return

    save_incoming(msg)                                # נשמר תמיד, גם בהשתקה
    if is_blocked(user_id) or takeover_service.is_paused(msg.chat.id):
        return                                        # שקט. בלי הודעות מערכת
    if check_rate_limit(user_id):
        owner_channel.notify_rate_limited_once(conn, user_id); return   # שקט ללקוח
    if not conn.can_reply:
        owner_channel.notify_missing_permission_once(conn); return
    record_message(user_id)
    result = await asyncio.to_thread(process_incoming_message, user_id, msg.text,
                                     user_info, fallbacks, channel="telegram_business")
    await dispatch_result(result, msg, conn)          # reply_text; טיפול בכשל שליחה → 1.4
```

רישום ה-handlers לכל בוט-בן:

```python
app.add_handler(BusinessConnectionHandler(on_business_connection))
app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT, on_business_message))
app.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, on_edited_business_message))
app.add_handler(BusinessMessagesDeletedHandler(on_deleted_business_messages))
# הצ'אט הרגיל של הבן = ערוץ הבעלים: /pause, /resume, /status
app.add_handler(CommandHandler(["pause", "resume", "status"], owner_commands))
```

הודעת מדיה (תמונה/קול) בשלב 1: מענה גישור קצר + התראה לבעלים; לא שומרים מדיה (מזעור).

### 4.3 היפוך ה-live chat: הבעלים לא "מצטרף" — הוא פשוט עונה

| היבט | היום (`live_chat_service`) | בפרויקט (`takeover_service`) |
|---|---|---|
| טריגר כניסה | כפתור בפאנל | ‏`business_message` אנושי מהבעלים (וגם כפתור בפאנל נשאר) |
| הודעות מעבר ללקוח | "בעל העסק הצטרף" / "הבוט חזר" | **אין.** מעברים שקטים לחלוטין |
| מפתח session | ‏`user_id` | ‏`(tenant, chat_id)` |
| חזרת הבוט | ידנית או timeout ‏120 דק' | אותו מנגנון; ‏`updated_at` מתחדש בכל הודעת בעלים |
| שמירת הודעות בהשתקה | כן | כן — זו ההיסטוריה בפאנל |

באותו עיקרון: חריגת rate limit, השתקה, וחוסר הרשאה לא מייצרים הודעת מערכת ללקוח. מאדם, "אנא המתן, יותר מדי הודעות" נשמע מוזר. שתיקה + התראה לבעלים.

### 4.4 פרסונה — ‏Layer A מחדש, והחלטת שקיפות

ענף `channel="telegram_business"` ב-`build_system_prompt` מחליף שלושה דברים: (1) זהות — "אתה העוזר האישי שמנהל את התכתובות של {שם}; עונה מטעמו/ה, גוף ראשון, סגנון אישי וקצר"; (2) פורמט — טקסט נקי, בלי הפניות לכפתורים, בלי אימוג'י-כותרות של בוט, תשובות קצרות (DM אישי, לא צ'אט שירות); (3) **שקיפות** — אם נשאלים במפורש אם זו הודעה אוטומטית, מאשרים. בוט שמכחיש אוטומציה כשנשאל ישירות הוא בעיה אתית ומשפטית, לא פיצ'ר. החלטת מוצר מחייבת. ההגדרות פר-tenant (טון, ניסוחים, פרומפט מותאם) עוברות כמו שהן.

### 4.5 ‏Handoff — הבעלים הוא הנציג, וצ'אט הבן הוא ערוץ הניהול

‏`user_chat_id` שמגיע ב-`BusinessConnection` הוא צ'אט ישיר בוט↔בעלים — קיים עוד לפני שהבעלים שלח לבוט הודעה אחת. במודל B זה הצ'אט של הבעלים עם **הבוט שלו**, ממותג בשם העסק. כש-`[HANDOFF]` מזוהה:

```
לקוח: "יש לכם מכשיר X במלאי? צריך היום"
  └► LLM עונה [HANDOFF]
      ├► ללקוח: משפט גישור קצר ("בודק ואחזור אליך בהקדם") — או כלום, לפי הגדרת tenant
      ├► לבעלים (בצ'אט הבן): "🔔 דנה לוי שואלת על מלאי מכשיר X.
      │   «יש לכם מכשיר X במלאי?...» — עני ישירות בצ'אט, אני אשתוק שם."
      └► takeover_service מסמן ממתין-לבעלים (עם timeout)
```

אותו ערוץ משרת גם: אישור השלמת onboarding, התראות חיבור/הרשאות, התראות "חלון סגור", ‏digest יומי ("עניתי על 34 הודעות, 3 ממתינות לך"), ופקודות `/pause` / ‏`/resume` / ‏`/status`. הבוט המנהל נשאר לענייני פלטפורמה בלבד (צימוד, billing עתידי).

### 4.6 ‏Onboarding מלא (מודל B)

הבעיה שהצימוד פותר: כשמגיע `managed_bot` update, טלגרם אומרת מי המשתמש היוצר — לא לאיזה tenant הוא שייך אצלנו. לכן קושרים `owner_user_id ↔ tenant` **לפני** יצירת הבוט:

```
1. אשף בפאנל: יצירת tenant ⇒ קוד צימוד חד-פעמי (תפוגה שעה) + לינק t.me/<manager>?start=PAIR-xxxx
2. הלקוח פותח את הבוט המנהל, /start עם הקוד ⇒ נשמר owner_user_id → tenant
3. המנהל שולח דיפ-לינק: t.me/newbot/<manager>/<suggested_username>?name=<שם העסק>
   (username מוצע ייחודי, מסתיים ב-bot, נרשם כ-pending)
4. הלקוח מאשר בלחיצה ⇒ managed_bot update ⇒ התאמה לפי creator.user_id (ראשי; suggested_username משני,
   כי המשתמש עשוי לשנות שם תפוס) ⇒ getManagedBotToken ⇒ הצפנה ב-tenant_secrets ⇒
   setWebhook לבן על /telegram/webhook/t/<key> ⇒ setManagedBotAccessSettings (Secretary Mode; V1) ⇒
   הודעה לבעלים: "הבוט @X מוכן — עכשיו חבר אותו: הגדרות ← Chatbots"
5. business_connection נכנס ב-webhook של הבן ⇒ אימות from == owner הרשום ⇒ שמירה,
   סטטוס "מחובר" בפאנל, אישור לבעלים בצ'אט הבן
```

עקרונות: ‏fail-closed — בן בלי connection מאומת לעולם לא עונה ללקוחות; ניתוק (`is_enabled=false`) מסומן מיד ומוצג בפאנל. **Offboarding** (זכות מחיקה + עזיבת לקוח): ניתוק webhook, ‏`replaceManagedBotToken` (נטרול הטוקן הפעיל), מחיקת הסוד, השעיית tenant, ומחיקת נתונים לפי המדיניות. אם קיימת מתודת מחיקת-בוט מלאה — להשתמש בה (בדיקה בתוך V1).

### 4.7 טופולוגיית תהליך

תהליך יחיד: ‏Flask ב-main thread, לולאת asyncio ב-thread נפרד, גשר `run_coroutine_threadsafe`. הבוט המנהל הוא Application קבוע; הבנים נבנים עצלה דרך `bot_registry` וממופים ל-routes. ‏`set_webhook` לכל בן חייב `allowed_updates` שכולל במפורש את ארבעת סוגי עדכוני ה-Business; למנהל — את `managed_bot`.

---

## 5. סכימת נתונים

### 5.1 ‏control plane (‏platform.db)

```sql
-- בוטים-בנים. הטוקן עצמו ב-tenant_secrets (מוצפן), לא כאן.
CREATE TABLE IF NOT EXISTS managed_bots (
    bot_id          INTEGER PRIMARY KEY,           -- Telegram bot id של הבן
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    bot_username    TEXT NOT NULL,
    owner_user_id   INTEGER NOT NULL,              -- המשתמש היוצר = בעל העסק
    status          TEXT NOT NULL DEFAULT 'created', -- created/secretary_on/connected/revoked
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mbots_tenant ON managed_bots(tenant_id);
CREATE INDEX IF NOT EXISTS idx_mbots_owner  ON managed_bots(owner_user_id);

-- מצב חיבור ה-Secretary של כל tenant (לרוב שורה אחת פר-tenant).
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id   TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    owner_user_id   INTEGER NOT NULL,
    user_chat_id    INTEGER,                       -- ערוץ הניהול (4.5)
    is_enabled      INTEGER NOT NULL DEFAULT 1,
    can_reply       INTEGER NOT NULL DEFAULT 0,
    rights_json     TEXT NOT NULL DEFAULT '{}',
    connected_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bizconn_tenant ON business_connections(tenant_id);

-- צימוד מוקדם: קושר owner_user_id ל-tenant לפני יצירת הבוט (4.6).
CREATE TABLE IF NOT EXISTS pairing_codes (
    code            TEXT PRIMARY KEY,              -- secrets.token_urlsafe
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    used_at         TEXT,
    used_by_user_id INTEGER
);
```

הכנסות עם `INSERT OR REPLACE` (חיבור מחדש מעדכן הרשאות). ‏cache לפתרון `connection_id → רשומה` עם TTL קצר + אינבלידציה בכל עדכון, בתבנית cache הסטטוסים הקיים. הטוקנים ב-`tenant_secrets` תחת השם `telegram_bot_token` — התבנית הקיימת.

### 5.2 ‏DB של כל tenant

- ‏`conversations` — כמו שהיא; ‏`channel='telegram_business'`; עמודה חדשה `authored_by TEXT DEFAULT 'bot'` (‏bot / owner) דרך `_ensure_column`.
- ‏`users` — נשאר; נוספות `last_inbound_at` (מעקב חלון 24ש', סעיף 1.4) ו-`consecutive_fallbacks` ‏(ב-DB, לא ב-`context.user_data` — הערוץ הוא webhook).
- ‏`live_chats` — הבסיס ל-takeover; נוספת `started_by` ‏('owner_message' / 'panel' / 'handoff').
- ‏`kb_entries` / ‏`unanswered_questions` / ‏`conversation_summaries` / ‏`customer_facts` — כמו שהם. **אין `kb_chunks`.**
- כל טבלה/עמודה חדשה = שורה ב-`privacy_data_matrix` באותו commit.

---

## 6. פרטיות — הפרק שאסור לדחות

הבוט קורא תכתובת פרטית של אנשים שלא יודעים שבוט קורא אותה. שלוש שכבות:

1. **מול הלקוח הסופי (נושא המידע):** אין מסך הסכמה — אין לו איפה לחיות. הפתרון: (א) שורת גילוי בהודעה הראשונה בצ'אט חדש ("כאן העוזר של {שם} — אני עונה כשהוא לא זמין"; ניתנת לכיבוי ע"י ה-tenant, ברירת מחדל דלוקה + אזהרה בפאנל כשמכבים); (ב) כלל השקיפות בפרומפט (4.4); (ג) בקשת מחיקה בשפה חופשית ("תמחקו את המידע שלי") מזוהה כ-intent וממופה ל-`delete_user_data`, עם אישור לבעלים.
2. **מול בעל העסק (הלקוח שלנו):** הסכם עיבוד נתונים; הצהרה שהוא אחראי ליידע את לקוחותיו (תיקון 13 — חובת היידוע על בעל המאגר); מסמכי legal מהריפו כבסיס. ב-offboarding — מחיקת הבוט-הבן ונתוניו (4.6).
3. **מול טלגרם:** כיבוד `deleted_business_messages` — מחיקה מיידית של העותקים, כולל מזיכרון הלקוח והסיכומים אם נכנסו לשם (פשוט מבעבר — אין chunks); ‏retention אוטומטי; מזעור — טקסט בלבד בשלב 1.

---

## 7. שלבי מימוש

עיקרון: כל שלב מסתיים במשהו שרץ. שלב 1 בכוונה על **בוט ידני מ-BotFather** — כדי שהערוץ יוכח לפני שתלויים בהנחות ה-Managed (שמאומתות במקביל).

| שלב | תכולה | תוצר בדיד |
|---|---|---|
| **0 — שלד** | ריפו; העתקת החבילה מ-4.1; ‏kb_service במקום rag; טסטים שהועתקו ירוקים | ‏seed + ‏`/kb` עובדים; אין בוט |
| **1 — ערוץ Secretary על tenant יחיד (בוט ידני)** | ‏business_handlers + ‏webhook; מענה מלא בשם הבעלים מתוך ה-KB; סינון בעלים (is_from_offline!); טיפול חלון-סגור בסיסי; **משימות אימות V1–V5** | דמו חי: חשבון אחד מחובר, הבוט עונה בשמו |
| **2 — ‏Managed onboarding + ‏multi-tenant** | ‏manager_bot; צימוד; יצירת בנים; ‏bot_registry; אשף בפאנל; ‏offboarding | שני לקוחות, כל אחד בוט משלו, בידוד מלא |
| **3 — דו-קיום עם הבעלים** | ‏takeover_service; ‏handoff לצ'אט הבן; פקודות בעלים; ‏digest | הבעלים עונה — הבוט שותק; ‏HANDOFF מגיע לבעלים |
| **4 — הקשחה ותאימות** | ‏edited/deleted מלא; חלון-סגור מלא; שורת גילוי; ‏retention; ‏privacy matrix; ‏backup; ‏Sentry | מוכן לפיילוט |

---

## 8. סיכונים ומשימות אימות

| # | נושא | סטטוס / המלצה |
|---|---|---|
| V1 | האם `setManagedBotAccessSettings` מדליק Secretary Mode לבן? | **ההנחה הקריטית של מודל B.** אימות בתחילת שלב 1. ‏fallback: הלקוח-היוצר מדליק ידנית ב-BotFather |
| V2 | נקודת החיבור בהגדרות למשתמש ללא Premium | אימות אמפירי; משפיע על מדריך הלקוח |
| V3 | ‏handler ל-`managed_bot` ב-PTB 22.x | אם אין — ‏TypeHandler על update גולמי |
| V4 | ‏`sendMessageDraft` מעל business connection | ניסוי; דגל כבוי כברירת מחדל |
| V5 | חלון 24ש' — האם מושפע מהגדרות החיבור | אימות אמפירי; מעצב את הודעות ה-"חלון סגור" |
| 6 | התנגשות username מוצע | התאמה לפי creator.user_id, לא לפי שם |
| 7 | משפט גישור ב-handoff — לענות "בודק ואחזור" בשם הבעלים, או לשתוק? | הגדרה פר-tenant; ברירת מחדל: משפט גישור — דממה בערוץ אישי נקראת כהתעלמות |
| 8 | שורת הגילוי — ‏tenants ירצו לכבות | ברירת מחדל דלוקה + אזהרה; תיעוד ההחלטה אצל ה-tenant |
| 9 | הודעות מדיה | ‏MVP טקסט בלבד; גישור + התראה לבעלים; תמלול — גרסה 2 |
| 10 | חיקוי סגנון כתיבה של הבעלים | לא בשלב 1; ‏custom_phrases + ‏tone מספקים התחלה |

---

## 9. מסמכים קיימים שמשרתים את הפרויקט

- ‏`docs/chatbot_build_guide.md` — שכבות ה-LLM, סכימת שיחות, זיכרון (לדלג על פרק ה-RAG).
- ‏`docs/multi_tenant_migration_spec.md` — הרציונל של file-per-tenant ו-control plane. הפרויקט מתחיל מהנקודה שהמסמך מסתיים בה.
- ‏`docs/privacy_data_matrix.md` + ‏`docs/legal/` — תבניות לציות.
- ‏`CLAUDE.md` — כללי הפיתוח (migrations, ‏exceptions, טסט באותו commit) חלים כלשונם.
- ‏`docs/rag_extraction_guide.md` — **לא רלוונטי יותר** (אין RAG); נשאר כרפרנס אם יידרש retrieval בעתיד מאחורי `get_kb_context`.
