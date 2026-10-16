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
    # A single piece of text (e.g. an activity's instructions written as one paragraph, as
    # older decks store them) is ONE item - never split into its letters.
    if isinstance(values, str):
        values = [values]
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
            if _clean(slide.get("guiding_question")):
                # The PowerPoint shows this as TWO slides: the question first (students answer it
                # on their phones), then the reveal with the key points - live follows the same steps.
                screens.append({"kind": "content_question", "title": screen["title"], "body": [],
                                "questions": [{"prompt": _clean(slide["guiding_question"]), "kind": "text"}]})
            screen["body"] = _list(slide.get("key_points"))
            if _clean(slide.get("check_question")):
                screen["questions"] = [{"prompt": _clean(slide["check_question"]), "kind": "text"}]
        elif kind == "real_life_example":
            screen["body"] = _list([slide.get("scenario")])
            if _clean(slide.get("discussion_question")):
                screen["questions"] = [{"prompt": _clean(slide["discussion_question"]), "kind": "text"}]
        elif kind in ("activity", "practical"):
            screen["body"] = _list(slide.get("instructions"))
            versions = slide.get("differentiation") or {}  # Stage 10: support / stretch, opened privately on a phone
            if (versions.get("support") or {}).get("steps"):
                screen["support"] = {"steps": _list(versions["support"]["steps"]), "help": _list(versions["support"].get("scaffolds"))}
            if (versions.get("stretch") or {}).get("steps"):
                screen["stretch"] = {"steps": _list(versions["stretch"]["steps"]),
                                     "challenge": _clean(versions["stretch"].get("challenge"), 300)}
        elif kind == "objectives_review":
            screen["title"] = screen["title"] or "How confident are you?"
            screen["questions"] = [{"prompt": _clean(o.get("objective") or o.get("self_assess_prompt")), "kind": "confidence",
                                    "choices": CONFIDENCE} for o in (slide.get("objectives_check") or []) if isinstance(o, dict)]
        else:
            screen["body"] = _list(slide.get("key_points") or [slide.get("subtitle")])
        if screen["title"] or screen["body"] or screen["questions"]:
            screens.append(screen)
    return _number(screens)


def attach_pptx_slides(screens: list[dict], outline: dict, pptx_slide_count: int) -> bool:
    """Gives each screen the number of the PowerPoint slide it shows, so the live lesson can
    show the real slide. Only when the deck's file has exactly the slides its outline makes
    (otherwise - e.g. an old or edited file - the live lesson keeps its simple text view)."""
    from .pptx_html import outline_slide_map
    mapping = outline_slide_map(outline)
    if sum(len(m) for m in mapping) != pptx_slide_count:
        return False
    flat = [i for m in mapping for i in m]
    if len(flat) != len(screens):
        return False
    for screen, index in zip(screens, flat):
        screen["slide"] = index
    return True


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
        if q["kind"] in ("confidence", "mcq"):
            entry["counts"] = [sum(1 for r in answers if r.get("choice") == i) for i in range(len(q.get("choices", [])))]
            entry["choices"] = q.get("choices", [])
        if q["kind"] == "mcq":  # Stage 8 quiz question
            entry["answer"] = q.get("answer")
            entry["explanation"] = q.get("explanation", "")
            entry["correct_count"] = sum(1 for r in answers if r.get("choice") == q.get("answer"))
            for r in answers:
                r["correct"] = r.get("choice") == q.get("answer")
        out[q["key"]] = entry
    return out


LOCAL_HOSTS = ("localhost", "127.", "0.0.0.0", "[::1]", "::1")


def clean_public_url(value) -> str | None:
    """An admin-typed address like '192.168.1.20:8000' or 'https://coursecraft.college.ac.uk/'
    -> 'http://192.168.1.20:8000' / 'https://coursecraft.college.ac.uk'. None if it is not a
    usable web address (spaces, other characters) - nothing odd can end up in a QR code."""
    import re
    text = str(value or "").strip().rstrip("/")
    if not text:
        return None
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "http://" + text
    if len(text) > 200 or not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$", text):
        return None
    return text


def join_link(base_url: str, code: str) -> str:
    return f"{base_url.rstrip('/')}/join?code={code}"


def is_local_only(base_url: str) -> bool:
    """True for an address a student's phone can never open (this computer's own name)."""
    host = base_url.split("://", 1)[-1].split("/", 1)[0].lower()
    return any(host.startswith(h) for h in LOCAL_HOSTS)
