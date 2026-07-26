# בוט ה-Secretary

בוט טלגרם שקורא את ההודעות שנכנסות לחשבון האישי של בעל עסק ועונה
עליהן **בשמו**, מתוך בסיס ידע שהוא מנהל בפאנל. הלקוח רואה תשובה
מהחשבון של בעל העסק — לא מבוט, ובלי כפתורים או תפריטים.

הפלטפורמה רב-לקוחית: כל לקוח מקבל בוט משלו וקובץ נתונים משלו.

| מסמך | מה יש בו |
|---|---|
| [`docs/Plan.md`](docs/Plan.md) | החלטות ארכיטקטורה והרציונל שמאחוריהן |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | ביצוע משימה-משימה, עם סימון מה הושלם |
| [`docs/client_guide.md`](docs/client_guide.md) | מדריך חיבור ללקוח + מה להסביר לו |
| [`docs/privacy_data_matrix.md`](docs/privacy_data_matrix.md) | מה כל טבלה מכילה ומה קורה לה במחיקה |
| [`docs/verification_log.md`](docs/verification_log.md) | מה נבדק מול ה-API בפועל ומה עדיין הנחה |
| [`CLAUDE.md`](CLAUDE.md) | כללי הפיתוח המחייבים בריפו הזה |

---

## הרצה מקומית

```bash
pip install -r requirements.txt
cp .env.example .env          # ומלאו — ראה "משתני סביבה" למטה
python -m platform_cli gen-key   # ⇒ SECRETS_ENCRYPTION_KEY

python main.py --seed         # ‏tenant דמו + בסיס ידע לדוגמה
python main.py                # פאנל + בוטים  →  http://localhost:5000
python main.py --admin        # פאנל בלבד, בלי בוטים
python -m pytest tests/ -v
```

`python main.py` מריץ את שרת הפיתוח של Flask. **לפרודקשן — ראה למטה.**

---

## ארכיטקטורה בשלוש שורות

**תהליך יחיד** (‏PLAN §4.7): ‏Flask ב-main thread, לולאת asyncio
ב-thread נפרד, וגשר `run_coroutine_threadsafe` ביניהם. עדכוני טלגרם
נכנסים כ-routes של Flask, מקבלים 200 מיד, והעבודה נמסרת ללולאה.

**שני מישורים.** ‏`platform.db` (control plane) מחזיק את רישום
הלקוחות, הסודות המוצפנים והחיבורים; לכל לקוח יש
`tenants/<slug>/chatbot.db` משלו (data plane). ‏`get_connection()` פותח
את הקובץ לפי ה-tenant הנוכחי — אין `sqlite3.connect` ישיר בשום מקום.

**בלי RAG.** בסיס הידע נכנס לפרומפט בשלמותו דרך
`kb_service.get_kb_context()`. אין chunks ואין FAISS.

---

## פריסה — Render

`render.yaml` הוא blueprint מוכן. שלושה דברים שחשוב להבין לפני:

**‏worker אחד, instance אחד — אילוץ, לא חיסכון.** התהליך מחזיק בזיכרון
את לולאת ה-asyncio, את אפליקציות ה-PTB של כל הבוטים, ואת ה-scheduler.
עותק שני פירושו שני digests באותו יום, שני גיבויים, ושתי אפליקציות
שכותבות לאותו קובץ SQLite. המקבילוּת מגיעה מ-threads:

```bash
gunicorn wsgi:app --workers 1 --threads 8 --timeout 60
```

**בלי `--preload`.** הוא מייבא את האפליקציה ב-master ואז forkים ממנו;
ה-thread של לולאת הבוטים אינו שורד fork, וה-webhook היה מקבל לולאה
מתה. ההסבר המלא ב-[`wsgi.py`](wsgi.py).

**הדיסק הוא הנתונים.** בלי disk mounted, כל deploy מוחק את כל
הלקוחות. ‏`DATA_DIR` חייב להצביע עליו, וכך גם `BACKUP_DIR`.

### אחרי הפריסה הראשונה

```bash
# ‏1. משתמש פלטפורמה
python -m platform_cli create-admin --email you@example.com --platform-admin

# ‏2. לקוח ראשון (או דרך האשף בפאנל — ראה client_guide)
python -m platform_cli create-tenant --id salon-dana --name "סלון דנה"
python -m platform_cli set-secret --tenant salon-dana --name telegram_bot_token
```

שני הצעדים האחרונים — הדלקת **Secretary Mode** ב-BotFather וחיבור
בהגדרות ← Chatbots — נעשים **אצל הלקוח**. אין דרך לעשות אותם בשבילו
(‏V1 ב-`verification_log.md`).

---

## משתני סביבה

`.env.example` הוא הרשימה המלאה והמעודכנת. אלה שבלעדיהם התהליך לא
עולה כמו שצריך:

| משתנה | למה |
|---|---|
| `SECRETS_ENCRYPTION_KEY` | ‏Fernet. בלעדיו כתיבת סודות נחסמת (fail-closed). **חייב** להיות מפתח תקין — סיסמה חופשית נדחית בעליית התהליך. |
| `LEDGER_PEPPER_V1` | ‏HMAC ל-`consent_ledger`. חי בנפרד מה-DB ומהמפתח שמעליו. |
| `ADMIN_SECRET_KEY` | חתימת sessions ו-CSRF. |
| `DATA_DIR` | הדיסק המתמיד. |
| `WEBHOOK_BASE_URL` | ה-URL הציבורי לרישום webhooks. |
| `TENANCY_STRICT=true` | בפרודקשן: גישה ל-DB בלי tenant context מרימה חריגה במקום להגיש בשקט את ה-DB הלא נכון. |

---

## עבודות מתוזמנות

‏`services/scheduler.py` — ‏task אחד שמתעורר כל דקה ובודק לכל עבודה אם
הגיע זמנה. הסימון ב-`platform_meta` הוא מה שמונע ריצה כפולה אחרי
deploy.

| שעה (ישראל) | עבודה | הערה |
|---|---|---|
| 03:00 | גיבוי | **לפני** ה-retention. ‏purge שרץ ראשון היה מוציא מהגיבוי בדיוק את מה שנמחק. |
| 04:00 | ‏retention | ‏`purge_old_data` על ה-DB של כל לקוח. |
| 20:00 | ‏digest | סיכום יומי לבעל העסק. יום שקט — לא נשלח כלום. |

השעה של ה-digest ניתנת לשינוי ב-`DIGEST_HOUR_LOCAL`.

### שחזור מגיבוי

```python
python - <<'PY'
import backup_service
backup_service.restore_tenant("salon-dana", "2026-07-15")   # ⇐ שם התיקייה
PY
```

הגיבוי נעשה דרך ה-online backup API של SQLite (לא `cp`), ולכן הוא
עקבי גם באמצע כתיבה. ‏`backup_service.set_upload_hook` הוא ה-seam
להעלאה ל-object storage.

---

## פקודות CLI

```bash
python -m platform_cli gen-key                     # מפתח Fernet
python -m platform_cli create-tenant --id X --name "שם"
python -m platform_cli create-admin --email a@b.com --tenant X
python -m platform_cli set-secret --tenant X --name telegram_bot_token
python -m platform_cli pair --tenant X             # קוד צימוד לאשף ההקמה
python -m platform_cli offboard --tenant X         # ניתוק לקוח
```

---

## כללי הברזל של הערוץ

מפורטים ב-`CLAUDE.md`. השלושה שהכי קל להפר בטעות:

1. **נקודת כניסה אחת ללקוח** — ‏`on_business_message`. אסור נתיב חדש
   שפונה ללקוח מחוץ לצינור הזה.
2. **ללקוח הסופי אין** מקלדות, פקודות, הודעות מערכת, או טוקן
   `[HANDOFF]` שדלף. **שתיקה עדיפה על הסגרת אוטומציה.**
3. **כל קריאה יוצאת** בצ'אט לקוח נושאת `business_connection_id`.
   בלעדיו ההודעה יוצאת מהבוט במקום מהחשבון של הבעלים.
