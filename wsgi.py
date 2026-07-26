"""
נקודת כניסה ל-WSGI (‏gunicorn) — לפרודקשן.

‏`main.py` מריץ את שרת הפיתוח של Flask, שמתאים לפיתוח מקומי אבל לא
לחשיפה לאינטרנט. המודול הזה חושף `app` שגם `gunicorn wsgi:app` יודע
לטעון, **וגם** מרים את לולאת הבוטים ואת ה-scheduler — כי בלעדיהם
ה-webhook היה מחזיר 503 וה-jobs היומיים לא היו רצים כלל.

**‏`--workers 1` הוא אילוץ, לא העדפה.** בטופולוגיה של PLAN §4.7 יש
תהליך יחיד שמחזיק בזיכרון: את לולאת ה-asyncio (‏`app.config['_bot_loop']`),
את אפליקציות ה-PTB, ואת ה-scheduler. ‏worker שני היה מרים עותק שלם של
כל אלה — כלומר **שני digests באותו יום, שני גיבויים, ושתי אפליקציות
שמתחרות על אותו קובץ SQLite**. המקבילוּת מגיעה מ-threads, ולכן:

    gunicorn wsgi:app --workers 1 --threads 8

**בלי `--preload`.** ‏preload מריץ את המודול הזה ב-master ואז forkים
ממנו; ה-thread של לולאת הבוטים אינו שורד fork, וה-webhook היה מקבל
לולאה מתה. בלי preload כל worker מייבא בעצמו — ומכיוון שיש בדיוק אחד,
זה בדיוק מה שצריך.
"""

from __future__ import annotations

import logging

import main

logger = logging.getLogger(__name__)

main.bootstrap()

app = main.create_wsgi_app()
