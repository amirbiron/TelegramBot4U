# ROADMAP — בוט Secretary לטלגרם (בוט לכל לקוח)

> מסמך העבודה של Claude Code בריפו הזה. קרא קודם את `PLAN.md` — כל החלטות הארכיטקטורה, הרציונל והסכימות שם; כאן רק ביצוע. סימון התקדמות: מעדכנים `[ ]` ל-`[x]` באותו commit של המשימה.

## כללי עבודה מחייבים

1. **מקור ההעתקות** הוא הריפו `ai-business-bot` (branch main). כשמשימה אומרת "העתק X" — מעתיקים את הקובץ ואת הטסטים שלו יחד, ומריצים אותם לפני שממשיכים. בריפו החדש אין את חבילת ה-aliases ‏`ai_chatbot/` — בעת ההעתקה מנרמלים imports ויעדי `patch` בטסטים (‏`ai_chatbot.X` ⇒ ‏`X`).
2. **פתרונות שורש בלבד.** אין טלאים, אין `except Exception: pass` (תמיד `logger.error`), אין שכפול לוגיקת WHERE בין get/count, אין fuzzy detection ל-handoff — רק הטוקן `[HANDOFF]` בתחילת תשובה.
3. **Migrations** דרך `_ensure_column` בלבד, לינארית, בלי טבלת גרסאות. אינדקס או constraint שתלויים בעמודה חדשה — ב-`migrations.py` **בלבד**, לעולם לא ב-executescript של `init_db`: ב-DB קיים המיגרציה רצה אחרי ה-executescript, והאינדקס נופל על עמודה שעוד לא קיימת (עובר על DB ריק ב-CI, נתפס רק בפרודקשן). הפירוט המלא ב-`CLAUDE.md`.
4. **טסט באותו commit** לכל לוגיקה חדשה. DB זמני (`tmp_path`), אף פעם לא API חיצוני אמיתי בטסטים.
5. **כל טבלה או עמודה חדשה = שורה ב-`docs/privacy_data_matrix.md` באותו commit.**
6. **אין RAG.** אם מתעורר צורך ב-retrieval — לא מממשים; פותחים דיון. החוזה הוא `kb_service.get_kb_context()` בלבד.
7. **הערוץ שקוף ללקוח הסופי:** אין מקלדות, אין `/start`, אין הודעות מערכת ללקוח (rate limit / השתקה / שגיאות = שתיקה ללקוח + התראה לבעלים).
8. סדר הפרומפט מקובע ל-prompt caching: פרסונה ← KB ← הגדרות tenant ← זיכרון/סיכום ← היסטוריה ← שאלה. אין להזריק תוכן תנודתי לפני ה-KB.

---

## שלב 0 — שלד: הליבה רצה בלי אף בוט

- [x] **T0.1 אתחול ריפו.** ‏`pyproject`/`requirements` עם `python-telegram-bot[ext]>=22.0`, ‏Flask, ‏cryptography, ‏openai, ‏anthropic, ‏pytest, ‏ruff. ‏`.env.example` עם כל משתני הסביבה שיצוצו בהמשך (מתעדכן בכל משימה שמוסיפה משתנה). קבלה: ‏`pytest` רץ (ריק) ו-`ruff check` נקי.
- [x] **T0.2 שכבת tenancy.** העתק `tenancy.py` + טסטים כמו-שהם. קבלה: הטסטים המקוריים ירוקים; `TENANCY_STRICT=true` נבדק.
- [x] **T0.3 ‏control plane מקוצץ.** העתק `control_plane.py`; צמצם `ROUTE_TYPES` ל-`telegram_webhook_key` + `public_slug`; צמצם `KNOWN_SECRET_NAMES` ל-`telegram_bot_token` (+ מפתחות LLM). הוסף סכימות `managed_bots`, ‏`business_connections`, ‏`pairing_codes` בדיוק כמו ב-PLAN §5.1, כולל אינדקסים, ופונקציות CRUD: ‏upsert/get/disable ל-connections (‏`INSERT OR REPLACE`), ‏create/consume ל-pairing (בדיקת תפוגה), ‏register/get_by_owner ל-managed_bots. קבלה: טסטים ל-CRUD כולל תפוגת קוד ו-cache invalidation של connections.
- [x] **T0.4 ‏utils.** העתק `utils/crypto.py`, ‏`utils/dates.py`, ‏`utils/pii_sanitizer.py`, ‏`utils/consent_ledger.py` + טסטים.
- [x] **T0.5 ‏DB פר-tenant.** העתק את `database.py` בתת-קבוצה: ‏`get_connection`, ‏`init_db` עם הטבלאות: ‏`kb_entries` (ללא `kb_chunks`!), ‏`conversations` (+ עמודת `authored_by TEXT DEFAULT 'bot'`), ‏`conversation_summaries`, ‏`users` (+ ‏`last_inbound_at`, ‏`consecutive_fallbacks`), ‏`unanswered_questions`, ‏`live_chats` (+ ‏`started_by`), ‏`customer_facts`. העתק את `migrations.py` (מנגנון בלבד, רשימת מיגרציות ריקה) ואת `migrate_all_tenants()`. פתח את `docs/privacy_data_matrix.md` עם שורה לכל טבלה. קבלה: ‏`init_db` על שני tenants יוצר שני קבצים מבודדים; טסט שכתיבה ל-tenant אחד לא נראית בשני.
- [x] **T0.6 ‏kb_service (חדש).** ‏`kb_service.py` עם `get_kb_context(top_hint: str | None = None) -> KBContext` שמחזיר: טקסט מלא של כל הרשומות הפעילות מקובצות לפי קטגוריה בפורמט `--- {קטגוריה} — {כותרת} ---\n{תוכן}`, אומדן טוקנים (tiktoken עם fallback ‏len//3), ו-`is_over_threshold` (סף ב-env, ברירת מחדל 50K). ‏cache בזיכרון פר-tenant שמפתחו `MAX(updated_at)` מ-`kb_entries` — שאילתה אחת בכל קריאה, אינבלידציה אוטומטית בעריכה. קבלה: טסטים — עדכון רשומה מרענן את ה-cache; ‏tenant אחד לא מזהם אחר; אומדן טוקנים סביר על עברית.
- [x] **T0.7 שכבת LLM.** העתק `openai_client.py`, ‏`llm_client.py`. העתק `llm.py` והחלף את בלוק ה-RAG ב-`get_kb_context()`; אכוף את סדר הבלוקים מכלל 8. העתק `config.py:build_system_prompt` והוסף ענף `channel="telegram_business"` לפי PLAN §4.4 (זהות עוזר-אישי, בלי כפתורים, כלל השקיפות). קבלה: טסט שבודק את **סדר** הבלוקים בפרומפט (asserts על אינדקסים של מחרוזות עוגן); טסט שענף הערוץ לא מכיל "לחצו על הכפתור".
- [x] **T0.8 ליבת עיבוד.** העתק `intent.py` (צמצם ל: greeting, farewell, business_hours, pricing, location, human_agent, complaint, general — בלי booking), ‏`rate_limiter.py`, ‏`core/message_processor.py`. נקה מה-processor: ענפי booking, טקסטים עם כפתורים, ותשובות הברכה הסטטיות (greeting/farewell עוברים דרך ה-LLM בערוץ הזה). העבר את רישום `unanswered_questions` מ-`chunks_used == 0` לזיהוי `[HANDOFF]`. קבלה: הטסטים המקוריים של intent/rate_limiter ירוקים; טסטים חדשים — הודעת handoff נרשמת כ-gap; אין מחרוזת "כפתור" באף תשובה.
- [x] **T0.9 פאנל אדמין.** העתק את חבילת ה-KB: ‏login (+ ‏CSRF/HTMX + ‏rate limit + ‏audit), ‏before_request של קשירת tenant, ‏routes ‏`/kb` ‏add/edit/delete/search + פערי ידע, התבניות והפילטרים, ‏`style.css`. **בלי** `/kb/rebuild`; ‏`/kb/search` ממומש כ-LIKE על כותרת+תוכן, עם escaping של `_` ו-`%` בקלט המשתמש (‏wildcard injection — דפוס 8 ב-CLAUDE.md). בנה `base.html` עם sidebar מינימלי: בסיס ידע, פערי ידע, שיחות (placeholder), הבוט שלי (placeholder), פלטפורמה. קבלה: התחברות + CRUD מלא של KB על tenant דמו מהדפדפן; ‏audit log נרשם.
- [x] **T0.10 שלד תהליך.** ‏`main.py`: ‏Flask ב-main thread, לולאת asyncio ב-thread, ‏`migrate_all_tenants()` בעלייה, ‏`/health`, ‏CLI ‏`--seed` (יוצר tenant דמו + רשומות KB לדוגמה). קבלה: התהליך עולה, ‏`/health` מחזיר ok, ‏seed עובד, וכל ה-suite ירוק.

**DoD שלב 0:** אדם עם הריפו בלבד מרים סביבה, מריץ seed, מנהל בסיס ידע בפאנל — בלי אף בוט.

---

## שלב 1 — ערוץ Secretary על tenant יחיד (בוט ידני מ-BotFather)

בכוונה על בוט שנוצר ידנית: מוכיחים את הערוץ לפני שתלויים בהנחות ה-Managed.

- [x] **T1.1 ‏handlers של הערוץ.** ‏`bot/business_handlers.py`: ‏`on_business_connection` (upsert ב-control plane; אימות `from == owner` הרשום ל-tenant; עדכון `can_reply`/`rights_json`; התראת שינוי לבעלים), ‏`on_business_message` בדיוק לפי הקוד ב-PLAN §4.2 — כולל תנאי הבעלים המשולש (`owner_user_id` + ‏`not is_from_offline` + ‏`sender_business_bot is None`), ‏`on_edited_business_message` (עדכון העותק ב-conversations), ‏`on_deleted_business_messages` (מחיקת עותקים + שורת consent_ledger). ‏takeover בשלב הזה מינימלי: הודעת בעלים ⇒ לא עונים ושומרים; מנגנון ההשתקה המלא בשלב 3. קבלה: יחידה על כל handler עם update fixtures מזויפים.
- [x] **T1.2 חיווט הבוט.** ‏`bot/business_bot.py`: בניית Application מטוקן, רישום ה-handlers מ-PLAN §4.2, ‏webhook route ‏`/telegram/webhook/t/<key>` עם אימות `X-Telegram-Bot-Api-Secret-Token`, ‏`set_webhook` עם `allowed_updates=["business_connection","business_message","edited_business_message","deleted_business_messages","message"]` (‏message — לפקודות הבעלים בהמשך). קבלה: טסט שה-allowed_updates מלא; טסט שה-route דוחה secret שגוי.
- [x] **T1.3 ‏dispatch יוצא.** ‏`dispatch_result`: ‏`reply_text` (ה-connection_id עובר אוטומטית), ‏`sendChatAction` typing לפני תשובות ארוכות, פיצול >4096 תווים, ועטיפת שגיאות שליחה: כשל שמתאים לחלון-סגור/אין-הרשאה ⇒ עדכון `users.last_inbound_at`/סימון שיחה + ‏`owner_channel.notify_window_closed` — לא retry עיוור, לא הודעה ללקוח. קבלה: טסטים עם mock ל-bot שמדמה את שני סוגי הכשל.
- [x] **T1.4 ערוץ בעלים בסיסי.** ‏`services/owner_channel.py`: שליחה ל-`user_chat_id` מה-connection, עם דה-דופ (התראה מסוג נתון לא נשלחת פעמיים בחלון זמן — `notify_*_once`). קבלה: טסט דה-דופ.
- [x] **T1.5 ‏fixtures אמיתיים.** הקלט JSONים אמיתיים של ארבעת סוגי העדכונים מהבוט הידני (לנקות PII!) ושמור ב-`tests/fixtures/`. בנה טסט אינטגרציה: ‏עדכון נכנס ⇒ webhook ⇒ תשובה יוצאת עם `business_connection_id` נכון. קבלה: הטסט רץ על ה-fixtures בלי רשת. **בוצע חלקית:** ה-fixtures נבנו לפי מבנה ה-API ומאומתים ב-`Update.de_json` (טסט נכשל אם המבנה ישתנה); הקלטה של תעבורה אמיתית מבוט מחובר טרם בוצעה — דורשת חשבון עם Secretary Mode פעיל.
- [x] **T1.6 הודעות מדיה.** זיהוי `business_message` בלי `text` ⇒ מענה גישור מוגדר-tenant + התראה לבעלים; לא שומרים את המדיה. קבלה: טסט.
- [x] **V1 אימות (חוסם את שלב 2): ‏Secretary דרך API.** בדוק בתיעוד ובניסוי אם `setManagedBotAccessSettings` מדליק Secretary Mode לבוט-בן. תעד את הממצא (כולל שמות שדות מדויקים) ב-`docs/verification_log.md`; אם התשובה שלילית — עדכן את PLAN §4.6 ל-fallback הידני וסמן זאת באשף.
- [x] **V2 אימות: חיבור ללא Premium.** מצא ותעד (צילומי מסך למדריך הלקוח) את נקודת החיבור בהגדרות עבור משתמש חינמי. **בוצע חלקית:** דרישת ה-Premium בוטלה ומתועדת (Bot API 10.0); מיקום מסך החיבור וצילומי המסך ממתינים לאימות על מכשיר.
- [x] **V3 אימות: ‏PTB ו-managed_bot.** בדוק אם PTB 22.x חושף handler/פילטר לעדכון `managed_bot`; תעד; אם לא — הכן `TypeHandler` לדוגמה.
- [x] **V4 אימות: ‏sendMessageDraft על business.** נסה מעל החיבור הידני; תעד; השאר מאחורי `HUMANIZED_DELIVERY=false`.
- [x] **V5 אימות: חלון 24ש'.** שלח מהבוט לצ'אט שלא היה פעיל יממה; תעד את השגיאה המדויקת שחוזרת — היא מה ש-T1.3 תופס. **בוצע:** המגבלה והגדרתה תועדו מהתיעוד הרשמי (`can_reply` = צ'אטים עם הודעה נכנסת ב-24ש'). נוסח השגיאה המדויק טרם נראה — ה-classifier נכתב על סט דפוסים עם ברירת מחדל בטוחה.

**DoD שלב 1:** דמו חי על חשבון אחד — לקוח כותב, הבוט עונה בשם הבעלים מתוך ה-KB; הבעלים כותב — הבוט שותק; חמשת האימותים מתועדים ב-`verification_log.md`.

---

## שלב 2 — ‏Managed onboarding + ‏multi-tenant

- [x] **T2.1 הבוט המנהל.** ‏`bot/manager_bot.py`: ‏`/start PAIR-xxxx` ⇒ אימות קוד (תפוגה, חד-פעמיות) ⇒ שמירת `owner_user_id → tenant` ⇒ שליחת דיפ-לינק `t.me/newbot/<manager>/<suggested>?name=<שם>` עם username מוצע (slug של שם העסק + סיומת `bot`, בדיקת ייחודיות מקומית). קוד שגוי/פג ⇒ הודעה ברורה. קבלה: טסטים על כל המסלולים.
- [x] **T2.2 קליטת בן.** טיפול בעדכון `managed_bot` (לפי ממצא V3): התאמה ל-tenant לפי `creator.user_id` (ראשי; ‏username משני) ⇒ ‏`getManagedBotToken` ⇒ הצפנה ל-`tenant_secrets['telegram_bot_token']` ⇒ רישום ב-`managed_bots` ⇒ ‏`setWebhook` לבן על route חדש ⇒ הפעלת Secretary לפי ממצא V1 (או הודעת הדרכה ל-fallback) ⇒ הודעה לבעלים בצ'אט הבן: "חבר אותי: הגדרות ← Chatbots". עדכון ‏`managed_bot` ללא pairing תואם ⇒ לוג + הודעת "לא מזוהה" ליוצר, בלי יצירת state. קבלה: טסט הזרימה המלאה עם mocks; טסט הדחייה.
- [x] **T2.3 ‏bot_registry לבנים.** העתק `bot_registry.py`; חיבור: ‏route key ⇒ tenant ⇒ Application עצל מהטוקן המוצפן. הבטח שהמנהל הוא Application קבוע נפרד. קבלה: טסט ששני בנים על אותו תהליך מנתבים נכון ולא חולקים טוקן.
- [x] **T2.4 אשף בפאנל.** ‏"לקוח חדש": יצירת tenant ⇒ הצגת קוד+לינק צימוד ⇒ סטטוסים חיים (צומד / בוט נוצר / Secretary פעיל / מחובר) בפולינג HTMX מ-`managed_bots.status` + ‏`business_connections`. מסך "הבוט שלי" פר-tenant: ‏username, סטטוס, הרשאות (rights_json), כפתור "שלח שוב הוראות חיבור". ‏`/platform`: עמודות סטטוס. קבלה: מעבר ידני מלא של האשף מהדפדפן מול mocks.
- [x] **T2.5 ‏Offboarding.** *(נבדק: אין מתודת מחיקת-בוט ב-Bot API — ה-offboarding נשען על `replaceManagedBotToken`; תועד ב-verification_log.)* פקודת פלטפורמה (CLI + כפתור בפאנל): ניתוק webhook של הבן ⇒ ‏`replaceManagedBotToken` לנטרול ⇒ מחיקת הסוד ⇒ ‏`managed_bots.status='revoked'` ⇒ השעיית tenant. בדוק בתיעוד אם קיימת מחיקת-בוט מלאה — אם כן, השתמש; תעד ב-verification_log. שורת privacy matrix. קבלה: טסט שסדר הפעולות נשמר גם בכשל אמצעי (idempotent).
- [x] **T2.6 בידוד תחת עומס.** טסט אינטגרציה: שני tenants, הודעות משולבות בשני ה-webhooks ⇒ כל תשובה עם ה-connection הנכון, כל שיחה ב-DB הנכון; ‏connection של tenant א' שמגיע ל-route של ב' ⇒ נדחה (הגנת cross-wiring מ-4.2).

**DoD שלב 2:** שני לקוחות אמיתיים, כל אחד עם בוט משלו שנוצר בלחיצה, מבודדים לחלוטין — בלי ש-BotFather הוזכר לאף אחד מהם.

---

## שלב 3 — דו-קיום עם הבעלים

- [x] **T3.1 ‏takeover_service.** העתק את `live_chat_service.py` והפוך לפי PLAN §4.3: מפתח `(tenant, chat_id)`, טריגר מהודעת בעלים אנושית, אפס הודעות מעבר ללקוח, ‏timeout ‏120 דק' (env), ‏`started_by`, ניקוי תקופתי. חבר ל-`on_business_message` במקום הלוגיקה המינימלית משלב 1. קבלה: טסטים — בעלים עונה ⇒ הבוט שותק ושומר; פג timeout ⇒ הבוט חוזר; הודעת בעלים נוספת מחדשת.
- [x] **T3.2 ‏Handoff לבעלים.** ‏`[HANDOFF]` ⇒ משפט גישור ללקוח לפי הגדרת tenant (ברירת מחדל: "בודק ואחזור אליך בהקדם") ⇒ הודעת התראה בצ'אט הבן עם שם הלקוח + ההודעה ⇒ ‏takeover במצב ממתין-לבעלים ⇒ רישום ה-gap. קבלה: טסט הזרימה + טסט ש-`strip_handoff_marker` תמיד רץ לפני שליחה.
- [x] **T3.3 פקודות בעלים.** בצ'אט הבן (זיהוי לפי `owner_user_id`, מחוץ ל-business updates): ‏`/pause` ו-`/resume` (autopilot גלובלי לצ'אטים או לצ'אט לפי reply), ‏`/status` (מחובר? הרשאות? כמה שיחות פעילות/מושתקות/ממתינות). קבלה: טסטים; משתמש שאינו הבעלים ⇒ התעלמות.
- [x] **T3.4 ‏Digest יומי.** ‏scheduler פלטפורמתי: פעם ביום לכל tenant מחובר — "עניתי על N הודעות, K ממתינות לך" בצ'אט הבן; שעה לפי env; דילוג אם אפס פעילות. קבלה: טסט חישוב המונים.

**DoD שלב 3:** תרחיש מלא בדמו: לקוח שואל ⇒ בוט עונה ⇒ שאלה קשה ⇒ גישור + התראה ⇒ הבעלים עונה ⇒ שקט ⇒ ‏timeout ⇒ הבוט חוזר.

---

## שלב 4 — הקשחה ותאימות

- [x] **T4.1 ‏edited/deleted מלא.** ‏edited מעדכן גם קלט של סיכומים עתידיים; ‏deleted מוחק מ-conversations, מסיר עובדות זיכרון שנגזרו מההודעות (אם ניתן לשיוך), ורושם ב-consent_ledger. קבלה: טסטים.
- [x] **T4.2 ‏Retention וזכות מחיקה.** העתק `purge_old_data` + ‏`delete_user_data` וודא כיסוי כל הטבלאות החדשות; ‏intent של בקשת מחיקה בשפה חופשית ⇒ אישור לבעלים ⇒ מחיקה. קבלה: טסט שמחיקת user מנקה את כל הטבלאות (הטסט נכשל אוטומטית אם נוספה טבלה עם user_id שלא כוסתה — לולאה על סכימת ה-DB).
- [~] **T4.3 הצגה עצמית — בוטל.** מומש כשורת גילוי ששורשרה מכנית לתשובה הראשונה, ובוטל אחרי דיון: איך הבוט מציג את עצמו הוא חלק מהפרסונה, כלומר החלטה של בעל העסק דרך `build_system_prompt` ו-`custom_prompt` — ולא מחרוזת שהפלטפורמה מוסיפה. הפרסונה כבר פותחת ב"אתה העוזר האישי שמנהל את התכתובות של {שם}". האכיפה הדטרמיניסטית שנשארה היא **כלל השקיפות** (`_build_channel_rules` כלל 6): הבוט מאשר שזו מענה אוטומטי כשנשאל ישירות, ואינו ניתן לכיבוי.
- [x] **T4.4 ‏Backup.** העתק `backup_service.py` + ‏`platform_maintenance.py` (בלי ענפי FAISS); גיבוי לילי של כל tenant + ‏platform.db. קבלה: טסט שחזור מגיבוי.
- [ ] **T4.5 ‏Observability.** ‏Sentry; לוגים: כל קריאת LLM (מודל, משך, טוקנים), כל handoff, כל rate-limit hit, כל כשל שליחה עם סיווג (חלון/הרשאה/אחר) — בלי PII (טלפונים ממוסכים, בלי תוכן הודעות ב-INFO). קבלה: בדיקת גרפ שאין `msg.text` בלוגי INFO.
- [x] **T4.6 מסמכים.** ‏privacy_data_matrix מלאה ומסונכרנת; ‏`docs/client_guide.md` (מדריך חיבור ללקוח עם הצילומים מ-V2); ‏README תפעולי (env, ‏deploy, ‏seed, ‏offboarding).
- [ ] **T4.7 ‏Load sanity.** בדיקה ידנית מתועדת: פרץ הודעות לבוט אחד לא מזיז את הלטנסי של בוט שני; התנהגות תחת 429 מטלגרם (backoff של PTB).

**DoD שלב 4:** פיילוט עם לקוח אמיתי אחד, שבוע רצוף, בלי התערבות ידנית בקוד.

---

## נספח: מפת העתקה מרוכזת (מקור ⇐ יעד)

| מקור ב-ai-business-bot | יעד | משימה |
|---|---|---|
| `tenancy.py` | `tenancy.py` | T0.2 |
| `control_plane.py` | `control_plane.py` | T0.3 |
| `utils/{crypto,dates,pii_sanitizer,consent_ledger}.py` | `utils/` | T0.4 |
| `database.py` (תת-קבוצה) + ‏`migrations.py` | `database.py`, ‏`migrations.py` | T0.5 |
| `openai_client.py`, ‏`llm_client.py`, ‏`llm.py`, ‏`config.py` | כנ"ל | T0.7 |
| `intent.py`, ‏`rate_limiter.py`, ‏`core/message_processor.py` | כנ"ל | T0.8 |
| `admin/` (חבילת KB + login) | `admin/` | T0.9 |
| `bot_registry.py` | `bot_registry.py` | T2.3 |
| `live_chat_service.py` | `services/takeover_service.py` | T3.1 |
| `memory/` + ‏`customer_facts` | `memory/` | T0.5/T0.7 |
| `backup_service.py`, ‏`platform_maintenance.py` | כנ"ל | T4.4 |
| `docs/privacy_data_matrix.md` (תבנית), ‏`docs/legal/` | `docs/` | T0.5, T4.6 |

## נספח: מה במפורש לא בונים

צינור RAG (chunker/embeddings/FAISS), טבלת `kb_chunks`, תורים ויומנים, שידורים, הפניות, ‏Twilio/Meta, מקלדות, מסך הסכמה בכפתורים, ופולו-אפ יזום בערוץ הזה (חסום ע"י חלון ה-24 שעות — PLAN §1.4). אם משימה נראית כאילו היא דורשת אחד מאלה — עצור ופתח דיון.
