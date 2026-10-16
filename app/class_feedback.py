"""
Stage 9: what last lesson's live checks say the class found hard - so the NEXT lesson's
PowerPoint revisits it. Pure functions, no database.

Only answers that can be marked automatically are used:
  - quiz questions (multiple choice): a question few students got right is a gap, and the most
    popular WRONG answer is the likely misconception;
  - the end-of-lesson confidence poll: an objective many students said "Not yet" or
    "Getting there" to is a gap.
Free-text answers are not judged (they need a lecturer to read them).

What goes to the AI is anonymous: questions, answers and percentages - never a student's name.
"""
from .live_activities import tally

MIN_ANSWERS = 3          # fewer answers than this is not enough evidence to call it a gap
QUIZ_GAP_BELOW = 0.7     # under 70% right
CONFIDENCE_GAP_FROM = 0.5  # half or more said "Not yet" / "Getting there"
MAX_GAPS = 6


def _pct(n, total):
    return round(100 * n / total) if total else 0


def learning_gaps(screens: list, responses: list) -> list:
    """responses: [{"question_key", "choice", "text", "name", ...}] for one live session.
    -> [{"key", "kind": "quiz"|"confidence", "text", "answered", "percent", "right", "common_wrong"}]
       hardest first."""
    gaps = []
    for i, screen in enumerate(screens or []):
        results = tally(screen, [r for r in responses if r.get("screen", i) == i])
        for q in screen.get("questions", []):
            t = results.get(q["key"])
            if not t or t["count"] < MIN_ANSWERS:
                continue
            if q["kind"] == "mcq":
                right = t["correct_count"] / t["count"]
                if right >= QUIZ_GAP_BELOW:
                    continue
                wrong = [(n, c) for idx, (n, c) in enumerate(zip(t["counts"], t["choices"])) if idx != q.get("answer") and n]
                common = max(wrong)[1] if wrong else ""
                gaps.append({"key": q["key"], "kind": "quiz", "text": q["prompt"], "answered": t["count"],
                             "percent": _pct(t["correct_count"], t["count"]), "right": q["choices"][q["answer"]],
                             "common_wrong": common, "score": right})
            elif q["kind"] == "confidence":
                unsure = sum(t["counts"][:2])
                share = unsure / t["count"]
                if share < CONFIDENCE_GAP_FROM:
                    continue
                gaps.append({"key": q["key"], "kind": "confidence", "text": q["prompt"], "answered": t["count"],
                             "percent": _pct(unsure, t["count"]), "right": "", "common_wrong": "", "score": 1 - share})
    gaps.sort(key=lambda g: g["score"])
    return gaps[:MAX_GAPS]


def describe(gap: dict) -> str:
    """One plain-English line (shown to the lecturer, and given to the AI)."""
    if gap["kind"] == "quiz":
        line = (f'Quiz question "{gap["text"]}" - only {gap["percent"]}% answered correctly ({gap["answered"]} answers). '
                f'Right answer: "{gap["right"]}".')
        if gap.get("common_wrong"):
            line += f' Most common wrong answer: "{gap["common_wrong"]}".'
        return line
    return (f'Objective "{gap["text"]}" - {gap["percent"]}% of the class said "Not yet" or "Getting there" '
            f'({gap["answered"]} answers).')


HEADER = "RESULTS FROM LAST LESSON'S LIVE CHECKS (anonymous class results - the class found these hard, revisit them):"


def prompt_block(gaps: list) -> str:
    if not gaps:
        return ""
    return "\n".join([HEADER] + [f"- {describe(g)}" for g in gaps])


def with_revisit(previous_context: str, gaps: list) -> str:
    """The lecturer's own "last lesson" text plus the results block (any older block is replaced)."""
    text = (previous_context or "").split(HEADER)[0].rstrip()
    block = prompt_block(gaps)
    return f"{text}\n\n{block}".strip() if block else text
