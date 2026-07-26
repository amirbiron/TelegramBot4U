"""אכיפת בטיחות bidi על כל הריפו (‏Trojan Source).

**מה הבעיה:** תווי override ו-isolate של יוניקוד משנים את סדר ההצגה
בלי לשנות את מה שהמפרש מריץ. קוד שמוסתר מאחוריהם נראה לסוקר כמו הערה
תמימה ומתבצע כמשהו אחר. זה CVE-2021-42574, והוא רלוונטי בדיוק לריפו
כמו זה — שכולו הערות בעברית, כלומר סוקר אנושי כבר מורגל לראות טקסט
מימין לשמאל ולא יחשוד.

**מה שמותר:** ‏RLM ו-LRM (‏U+200E/U+200F) הם סימני **יישור**. הם
אומרים לרנדרר מאיזה כיוון להתחיל פסקה, ואינם פותחים טווח שעוטף תוכן
אחר. בלעדיהם `hmac.compare_digest` בתחילת משפט עברי מוצג הפוך, ולכן
הם פזורים בכל הערות הריפו במכוון.

**מה שאסור:** ‏override (‏U+202D/U+202E) ו-embedding/isolate
(‏U+202A–U+202C, ‏U+2066–U+2069). אלה **פותחים טווח**, וזה מה שמאפשר
להסתיר קוד בתוך מה שנראה כמו מחרוזת או הערה.

הטסט הזה מחליף פטור גורף ל-`PLE2502` ב-ruff: הכלל שם אינו יודע להבחין
בין השניים, והפטור היה מכבה גם את הזיהוי האמיתי.
"""

import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# פותחי טווח — אלה שמאפשרים Trojan Source.
#
# מוגדרים בקודי escape ולא כתווים ממשיים **בכוונה**: קובץ שמכיל את
# התווים עצמם נתפס ע"י הסורק של עצמו. זה לא עקיפה — זה בדיוק הכלל,
# והוא חל גם כאן.
FORBIDDEN_BIDI = {
    "\u202a": "LRE (left-to-right embedding)",
    "\u202b": "RLE (right-to-left embedding)",
    "\u202c": "PDF (pop directional formatting)",
    "\u202d": "LRO (left-to-right override)",
    "\u202e": "RLO (right-to-left override)",
    "\u2066": "LRI (left-to-right isolate)",
    "\u2067": "RLI (right-to-left isolate)",
    "\u2068": "FSI (first strong isolate)",
    "\u2069": "PDI (pop directional isolate)",
}

# סימני יישור — מותרים במפורש
ALLOWED_BIDI = {"\u200e", "\u200f"}

SCANNED_SUFFIXES = (".py", ".html", ".md", ".toml", ".css", ".js", ".sql")
SKIP_DIRS = {".git", "__pycache__", "data", ".venv", "node_modules"}


def _files() -> list[pathlib.Path]:
    out = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        out.append(path)
    return out


class TestNoTrojanSource:
    def test_repo_is_scanned(self):
        """שומר על הטסט עצמו: רשימה ריקה הייתה עוברת בשקט."""
        assert len(_files()) > 50

    def test_no_forbidden_bidi_anywhere(self):
        offences = []
        for path in _files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for ch, name in FORBIDDEN_BIDI.items():
                    if ch in line:
                        rel = path.relative_to(REPO)
                        offences.append(f"{rel}:{lineno} — {name}")
        assert not offences, (
            "תווי bidi שפותחים טווח נמצאו בקוד (‏Trojan Source):\n"
            + "\n".join(offences)
        )

    @pytest.mark.parametrize("ch", sorted(ALLOWED_BIDI))
    def test_alignment_marks_are_not_flagged(self, ch):
        """‏RLM/LRM מותרים — הטסט לא אמור להיות מחמיר מדי."""
        assert ch not in FORBIDDEN_BIDI

    def test_detector_catches_a_planted_override(self, tmp_path, monkeypatch):
        """הטסט באמת תופס — ולא עובר כי החיפוש שבור."""
        planted = tmp_path / "evil.py"
        planted.write_text(
            "x = 1  # \u202eהערה תמימה\u202c\n", encoding="utf-8",
        )
        hits = [
            name for ch, name in FORBIDDEN_BIDI.items()
            if ch in planted.read_text(encoding="utf-8")
        ]
        assert len(hits) == 2
