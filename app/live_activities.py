"""
Stage 6 (B): turns a lesson deck (or a practice question set) into the SCREENS of a live
session - what the class sees, and the questions each student can answer on their own device.
Pure functions, no database.

A lesson deck already holds its questions as data (see pptx_generator's outline), so every
existing deck can be run live with no extra AI:
  starter ("Do Now") questions, each slide's check question, a real-life example's discussion
  question, the plenary questions -> free-text answers;
  the end-of-lesson objectives review -> a 3-point confidence poll per objective.
"""
import secrets

CONFIDENCE = ["Not yet", "Getting there", "Got it"]
JOIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L - easy to read off a projector
MAX_ANSWER_CHARS = 1000
MAX_NAME_CHARS = 30


def _clean(value, limit=600):
    return " ".join(str(value or "").split())[:limit]


def _list(values, limit=12):
    return [_clean(v) for v in (values or []) if _clean(v)][:limit]


def screens_from_deck(outline: dict) -> list[dict]:
    screens = []
    for slide in (outline or {}).get("slides", []):
        kind = slide.get("type") or "content"
        screen = {"kind": kind, "title": _clean(slide.get("title"), 200), "body": [], "questions": []}
        if kind == "title":
            screen["body"] = _list([slide.get("subtitle")])
        elif kind in ("retrieval_starter", "retrieval_plenary"):
            screen["title"] = screen["title"] or ("Do now" if kind == "retrieval_starter" else "Check your understanding")
            screen["questions"] = [{"prompt": q, "kind": "text"} for q in _list(slide.get("questions"))]
        elif kind == "objectives":
            screen["title"] = screen["title"] or "Today we will"
            screen["body"] = _list(slide.get("objectives"))
        elif kind == "content":
            screen["body"] = _list(slide.get("key_points"))
            if _clean(slide.get("check_question")):
                screen["questions"] = [{"prompt": _clean(slide["check_question"]), "kind": "text"}]
        elif kind == "real_life_example":
            screen["body"] = _list([slide.get("scenario")])
            if _clean(slide.get("discussion_question")):
                screen["questions"] = [{"prompt": _clean(slide["discussion_question"]), "kind": "text"}]
        elif kind in ("activity", "practical"):
            screen["body"] = _list(slide.get("instructions"))
        elif kind == "objectives_review":
            screen["title"] = screen["title"] or "How confident are you?"
            screen["questions"] = [{"prompt": _clean(o.get("objective") or o.get("self_assess_prompt")), "kind": "confidence",
                                    "choices": CONFIDENCE} for o in (slide.get("objectives_check") or []) if isinstance(o, dict)]
        else:
            screen["body"] = _list(slide.get("key_points") or [slide.get("subtitle")])
        if screen["title"] or screen["body"] or screen["questions"]:
            screens.append(screen)
    return _number(screens)


def screens_from_practice(questions: list[dict]) -> list[dict]:
    screens = []
    for n, q in enumerate(questions or [], start=1):
        tags = " · ".join([f"{q.get('marks')} mark{'s' if q.get('marks') != 1 else ''}"] + list(q.get("ao") or []))
        screens.append({"kind": "exam_question", "title": f"Question {n} ({tags})", "body": _list([q.get("context")]),
                        "questions": [{"prompt": _clean(q.get("question"), 1500), "kind": "text", "long": (q.get("marks") or 0) >= 6}]})
    return _number(screens)


def _number(screens):
    for s, screen in enumerate(screens):
        for k, q in enumerate(screen["questions"]):
            q["key"] = f"s{s}q{k}"
    return screens


def new_join_code(taken: set, length=6) -> str:
    while True:
        code = "".join(secrets.choice(JOIN_ALPHABET) for _ in range(length))
        if code not in taken:
            return code


def clean_name(name) -> str:
    return _clean(name, MAX_NAME_CHARS)


def clean_answer(text) -> str:
    return str(text or "").strip()[:MAX_ANSWER_CHARS]


def tally(screen: dict, responses: list) -> dict:
    """Per question on a screen: the answers (latest per student) and, for a poll, the counts.
    responses: [{"question_key", "name", "text", "choice"}]"""
    out = {}
    for q in screen.get("questions", []):
        answers = [r for r in responses if r["question_key"] == q["key"]]
        entry = {"prompt": q["prompt"], "kind": q["kind"], "answers": answers, "count": len(answers)}
        if q["kind"] == "confidence":
            entry["counts"] = [sum(1 for r in answers if r.get("choice") == i) for i in range(len(q.get("choices", [])))]
            entry["choices"] = q.get("choices", [])
        out[q["key"]] = entry
    return out
