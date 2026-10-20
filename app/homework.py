"""
Homework mode: the parts with no database - finding a student on the class list from the name
they type, marking their answers, and how the class did on each question.

Students TYPE their name (they never see the class list), so a shared link doesn't show anyone
who is in the class. Matching is forgiving about case, accents and extra spaces, and accepts a
first + last name ("Ben Okafor" for "Benjamin Tunde Okafor" only if first AND last match).
"""
import datetime
import os
import re
import unicodedata

MAX_NAME = 80


def local_now() -> datetime.datetime:
    """The time now in the college's time zone (UK by default; COURSECRAFT_TZ changes it), without a
    zone attached - due dates are typed as UK times, and the server's own clock is usually UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(os.getenv("COURSECRAFT_TZ", "Europe/London"))).replace(tzinfo=None)
    except Exception:  # no time-zone data on this machine - fall back to its own clock
        return datetime.datetime.now()


def _norm(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = text.lower().replace("'", "").replace("\u2019", "")  # O'Neill = ONeill
    return " ".join(re.sub(r"[^a-z0-9 -]", " ", text).replace("-", " ").split())


def _ref(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def match_student(typed_name: str, students: list, typed_ref: str = ""):
    """students: [{"id", "name", "ref"}] (active only). -> ("ok", id), ("many", None) when two
    students share the name (ask for their student reference), or ("none", None)."""
    name = _norm(typed_name)[:MAX_NAME]
    ref = _ref(typed_ref)
    if ref:
        hits = [s for s in students if _ref(s.get("ref")) == ref]
        if len(hits) == 1 and (not name or _name_matches(name, hits[0]["name"])):
            return "ok", hits[0]["id"]
    if not name:
        return "none", None
    exact = [s for s in students if _norm(s["name"]) == name]
    hits = exact or [s for s in students if _name_matches(name, s["name"])]
    if not hits:  # spacing typed differently ("O Neill" / "ONeill", "Mc Donald" / "McDonald")
        squashed = name.replace(" ", "")
        hits = [s for s in students if _norm(s["name"]).replace(" ", "") == squashed]
    if len(hits) == 1:
        return "ok", hits[0]["id"]
    if len(hits) > 1:
        if ref:
            narrowed = [s for s in hits if _ref(s.get("ref")) == ref]
            if len(narrowed) == 1:
                return "ok", narrowed[0]["id"]
        return "many", None
    return "none", None


def _name_matches(typed: str, full_name: str) -> bool:
    want, have = typed.split(), _norm(full_name).split()
    if not want or not have:
        return False
    if want == have:
        return True
    return len(want) >= 2 and len(have) >= 2 and want[0] == have[0] and want[-1] == have[-1]


def mark(questions: list, answers: dict) -> dict:
    """answers: {"0": choice index, ...} -> {"score", "total", "right": [True/False/None per question]}
    (None = not answered)."""
    right = []
    for i, q in enumerate(questions or []):
        picked = answers.get(str(i)) if answers else None
        right.append(None if picked is None else picked == q.get("answer"))
    return {"score": sum(1 for r in right if r), "total": len(right), "right": right}


def clean_choice(value, question) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value < len(question.get("choices") or []) else None


def question_stats(questions: list, all_answers: list) -> list:
    """all_answers: one {"0": choice} dict per HANDED-IN attempt -> per question
    {"question", "pct_right" (None if nobody answered), "answered", "common_wrong" (choice text or None)}."""
    out = []
    for i, q in enumerate(questions or []):
        picks = [a.get(str(i)) for a in all_answers if a.get(str(i)) is not None]
        wrong = [p for p in picks if p != q.get("answer")]
        common = max(set(wrong), key=wrong.count) if wrong else None
        choices = q.get("choices") or []
        out.append({"question": q.get("question", ""), "answered": len(picks),
                    "pct_right": round(100 * (len(picks) - len(wrong)) / len(picks)) if picks else None,
                    "common_wrong": choices[common] if common is not None and 0 <= common < len(choices) else None})
    return out


def is_late(due_at, submitted_at) -> bool:
    return bool(due_at and submitted_at and submitted_at > due_at)


def due_text(due_at, now=None) -> str:
    """"Due Fri 24 Oct, 23:59" or "Was due Fri 24 Oct, 23:59" (no date -> "")."""
    if not due_at:
        return ""
    now = now or local_now()
    when = due_at.strftime("%a %-d %b, %H:%M")
    return f"Was due {when}" if now > due_at else f"Due {when}"


def parse_due(date_text: str, time_text: str = ""):
    """"2026-10-24" + "17:00" -> a datetime (time defaults to 23:59). Bad or empty -> None."""
    try:
        day = datetime.date.fromisoformat((date_text or "").strip())
    except ValueError:
        return None
    try:
        hh, mm = (time_text or "23:59").strip().split(":")[:2]
        at = datetime.time(int(hh), int(mm))
    except (ValueError, TypeError):
        at = datetime.time(23, 59)
    return datetime.datetime.combine(day, at)


def review(questions: list, answers: dict, show_answers: bool) -> list:
    """The results page after handing in: each question, what they picked, and - if the lecturer
    allows - the right answer and why."""
    out = []
    for i, q in enumerate(questions or []):
        picked = (answers or {}).get(str(i))
        choices = q.get("choices") or []
        item = {"question": q.get("question", ""), "picked": choices[picked] if picked is not None and 0 <= picked < len(choices) else None,
                "right": None if picked is None else picked == q.get("answer")}
        if show_answers:
            a = q.get("answer")
            item["answer"] = choices[a] if isinstance(a, int) and 0 <= a < len(choices) else None
            item["explanation"] = q.get("explanation") or ""
        out.append(item)
    return out
