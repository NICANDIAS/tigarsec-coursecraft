"""
Live quizzes (Kahoot-style) for CourseCraft: multiple-choice questions, a countdown, points for
being right AND quick, the answer revealed with how the class voted, and a leaderboard.
Pure functions, no database.

Questions come from a lesson deck's own content (AI writes them, the lecturer checks them) or
are typed in by the lecturer - so it works with no AI at all.
Each question: {"question", "choices": [2-4 strings], "answer": index of the right choice,
"explanation": why it is right (shown after the reveal)}.
"""
import random

MAX_QUESTIONS = 20
MIN_SECONDS, MAX_SECONDS = 5, 120
GRACE_SECONDS = 2          # a tap sent just as the clock hits zero still counts (phones on slow Wi-Fi)
BASE_POINTS, SPEED_POINTS = 500, 500

QUIZ_SYSTEM_PROMPT = """You write a short multiple-choice quiz for a UK further education lesson, strictly \
from the lesson content you are given. Rules:
- each question tests ONE idea from the content, in clear, simple English a 16-18 year old understands quickly;
- exactly 4 options, ONE clearly correct; the 3 wrong options are plausible (common misconceptions), \
similar in length to the right one, and never "all of the above" / "none of the above";
- keep questions under 25 words and options under 12 words - students read them on a phone against a timer;
- a one-sentence explanation of why the right answer is right;
- mix recall and application ("Which of these is an example of..."); no trick questions.

Return ONLY compact raw JSON (no markdown fences, no commentary), exactly this shape:
{"questions": [{"question": "string", "choices": ["string", "string", "string", "string"], "answer": 0, \
"explanation": "string"}]}"""


def _text(value, limit=300):
    return " ".join(str(value or "").split())[:limit]


def build_quiz_prompt(outline: dict, count: int) -> str:
    lines = [f"Write {count} multiple-choice questions.", f"Lesson: {_text(outline.get('lesson_title'), 200)}", "",
             "Lesson content:"]
    for s in (outline or {}).get("slides", []):
        kind = s.get("type")
        if kind == "objectives":
            lines += [f"Objective: {_text(o)}" for o in (s.get("objectives") or [])]
        elif kind == "content":
            lines.append(f"- {_text(s.get('title'))}")
            lines += [f"    {_text(p)}" for p in (s.get("key_points") or [])]
        elif kind == "real_life_example":
            lines.append(f"- Example: {_text(s.get('title'))}: {_text(s.get('scenario'), 600)}")
        elif kind in ("retrieval_starter", "retrieval_plenary"):
            lines += [f"- Question used in class: {_text(q)}" for q in (s.get("questions") or [])]
    return "\n".join(lines)


def clean_question(q, shuffle_with=None):
    """One well-formed question, or None. Blank options are dropped (the answer index follows),
    repeated options are dropped, and 2-4 options are kept."""
    if not isinstance(q, dict) or not _text(q.get("question")):
        return None
    raw = q.get("choices") if isinstance(q.get("choices"), list) else []
    try:
        answer = int(q.get("answer"))
    except (TypeError, ValueError):
        return None
    if not 0 <= answer < len(raw) or not _text(raw[answer], 150):
        return None
    right = _text(raw[answer], 150)
    choices, seen = [], set()
    for i, c in enumerate(raw):
        c = _text(c, 150)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            choices.append(c)
    choices = choices[:4] if right in choices[:4] else choices[:3] + [right]
    if len(choices) < 2:
        return None
    if shuffle_with is not None:
        shuffle_with.shuffle(choices)  # the AI tends to put the right answer first - mix the positions
    return {"question": _text(q["question"]), "choices": choices, "answer": choices.index(right),
            "explanation": _text(q.get("explanation"), 400)}


def clean_quiz(result, seed=None):
    """(questions, dropped_count) from the AI's JSON."""
    raw = result.get("questions") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return [], 0
    rng = random.Random(seed)
    out, dropped = [], 0
    for q in raw[:MAX_QUESTIONS]:
        cleaned = clean_question(q, shuffle_with=rng)
        if cleaned and cleaned["question"].lower() not in {o["question"].lower() for o in out}:
            out.append(cleaned)
        else:
            dropped += 1
    return out, dropped


def clamp_seconds(value, default=20):
    try:
        return max(MIN_SECONDS, min(MAX_SECONDS, int(value)))
    except (TypeError, ValueError):
        return default


def screens_from_quiz(questions: list, seconds: int, title: str = "") -> list:
    """Live-session screens: a "get ready" screen (the clock doesn't start while students are
    still joining), one per question, then the final leaderboard."""
    total = len(questions)
    screens = [{"kind": "quiz_intro", "title": _text(title, 200) or "Quiz time",
                "body": [f"{total} question{'s' if total != 1 else ''} · {seconds} seconds each",
                         "Right answers score points - quicker right answers score more."], "questions": []}]
    for n, q in enumerate(questions, start=1):
        screens.append({"kind": "quiz_question", "title": f"Question {n} of {total}", "body": [],
                        "questions": [{"key": f"s{n}q0", "kind": "mcq", "prompt": q["question"], "choices": list(q["choices"]),
                                       "answer": q["answer"], "explanation": q.get("explanation", ""), "seconds": seconds}]})
    screens.append({"kind": "leaderboard", "title": "Final leaderboard", "body": [], "questions": []})
    return screens


def public_screen(screen: dict, revealed: bool) -> dict:
    """The screen as a student's phone may see it: the right answer and explanation are only
    sent AFTER the lecturer reveals them (otherwise a student could read them in the page)."""
    if revealed:
        return screen
    out = dict(screen)
    out["questions"] = [{k: v for k, v in q.items() if k not in ("answer", "explanation")} for q in screen.get("questions", [])]
    return out


def points_for(correct: bool, elapsed: float, seconds: int) -> int:
    """Right answers score 500, plus up to 500 more for answering fast. Wrong answers score 0."""
    if not correct:
        return 0
    seconds = max(1, seconds or 20)
    speed = max(0.0, min(1.0, 1 - (max(0.0, elapsed) / seconds)))
    return BASE_POINTS + round(SPEED_POINTS * speed)


def is_open(elapsed, seconds, revealed) -> bool:
    """Can a student still answer? Not once the answer is revealed, nor after the time runs out."""
    if revealed:
        return False
    if elapsed is None or not seconds:
        return True
    return elapsed <= seconds + GRACE_SECONDS


def leaderboard(responses: list, names: dict) -> list:
    """responses: [{"participant", "points", "correct"}]; names: {participant_id: display name}.
    -> [{"rank", "name", "points", "correct", "participant"}] best first (ties share a rank)."""
    totals = {pid: {"participant": pid, "name": name, "points": 0, "correct": 0} for pid, name in names.items()}
    for r in responses:
        t = totals.get(r["participant"])
        if t is None:
            continue
        t["points"] += r.get("points") or 0
        t["correct"] += 1 if r.get("correct") else 0
    ranked = sorted(totals.values(), key=lambda t: (-t["points"], -t["correct"], t["name"].lower()))
    last, rank = None, 0
    for i, t in enumerate(ranked, start=1):
        if (t["points"], t["correct"]) != last:
            rank, last = i, (t["points"], t["correct"])
        t["rank"] = rank
    return ranked


def revision_for(screens: list, mine: dict) -> list:
    """Stage 12 (4): a student's personal revision after a quiz - every question they got wrong or
    didn't answer, with the right answer and why. mine: {question key: chosen index}.
    -> [{"question", "choices", "answer", "explanation", "picked"}] in quiz order."""
    out = []
    for s in screens or []:
        for q in s.get("questions", []):
            if q.get("kind") != "mcq":
                continue
            picked = mine.get(q["key"])
            if picked == q.get("answer"):
                continue
            out.append({"question": q["prompt"], "choices": list(q["choices"]), "answer": q["answer"],
                        "explanation": q.get("explanation", ""), "picked": picked})
    return out
