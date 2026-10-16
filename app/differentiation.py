"""
Stage 10 (B): ready-made differentiation. For every activity and practical in a lesson deck,
CourseCraft writes two more versions of the same task:
  - SUPPORT: the same goal in smaller steps, with sentence starters, key words and hints - for
    students who need more help to get started;
  - STRETCH: a harder extension that pushes towards the higher grade (analyse, evaluate,
    justify) - for students who finish early.
The core version is the one already on the slide. Pure functions, no database.

They are printed on the activity worksheets (Download activity resources) and - in a live
lesson - each student can open the support or stretch version on their own phone, privately.
"""

DIFF_SYSTEM_PROMPT = """You are an experienced UK further education lecturer planning differentiation \
for a lesson. For each activity you are given its title, the core instructions and the lesson's \
objectives. Write two other versions of the SAME task, with the same goal and topic:

SUPPORT - for students who need more help to get started: the same task broken into smaller, \
concrete steps (simpler words, one action per step), plus "scaffolds": 2-5 sentence starters, key \
words with a short meaning, or hints. It must still reach the core learning, not a different easier topic.

STRETCH - for students who finish early or are aiming for a higher grade: 1-3 steps that extend the \
same task towards analysis, evaluation or justification (e.g. compare, judge, recommend with reasons), \
plus one "challenge" question.

Keep every step short and actionable. Return ONLY compact raw JSON (no markdown fences, no commentary), \
exactly this shape:
{"activities": [{"slide": 0, "support": {"steps": ["string", ...], "scaffolds": ["string", ...]}, \
"stretch": {"steps": ["string", ...], "challenge": "string"}}]}"""

KINDS = ("activity", "practical")


def _text(value, limit=400):
    return " ".join(str(value or "").split())[:limit]


def _list(values, limit=8, size=300):
    if isinstance(values, str):
        values = [values]
    return [_text(v, size) for v in (values or []) if _text(v)][:limit]


def activity_slides(outline: dict) -> list:
    """[(slide index, slide)] for the deck's activities and practicals."""
    return [(i, s) for i, s in enumerate((outline or {}).get("slides", [])) if s.get("type") in KINDS]


def build_prompt(outline: dict) -> str:
    objectives = []
    for s in outline.get("slides", []):
        if s.get("type") == "objectives":
            objectives += _list(s.get("objectives"))
    lines = [f"Lesson: {_text(outline.get('lesson_title'), 200)}"]
    if objectives:
        lines += ["Objectives:"] + [f"- {o}" for o in objectives]
    lines.append("")
    for i, s in activity_slides(outline):
        lines.append(f"Activity slide {i}: {_text(s.get('title'), 200)} ({s.get('type')}, {s.get('grouping') or 'any grouping'})")
        for step in _list(s.get("instructions"), 12):
            lines.append(f"  - {step}")
        resource = s.get("resource") or {}
        if resource.get("needed") and resource.get("content"):
            lines.append(f"  Handout used: {_text(resource.get('title'), 120)} - {_text(resource.get('content'), 500)}")
        lines.append("")
    return "\n".join(lines).strip()


def clean_differentiation(result, outline: dict) -> dict:
    """{slide index: {"support": {"steps", "scaffolds"}, "stretch": {"steps", "challenge"}}} - only for
    real activity/practical slides, and only versions that have steps."""
    valid = {i for i, _ in activity_slides(outline)}
    out = {}
    for item in (result.get("activities") if isinstance(result, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("slide"))
        except (TypeError, ValueError):
            continue
        if index not in valid:
            continue
        support = item.get("support") if isinstance(item.get("support"), dict) else {}
        stretch = item.get("stretch") if isinstance(item.get("stretch"), dict) else {}
        version = {}
        if _list(support.get("steps")):
            version["support"] = {"steps": _list(support.get("steps")), "scaffolds": _list(support.get("scaffolds"), 6, 200)}
        if _list(stretch.get("steps")):
            version["stretch"] = {"steps": _list(stretch.get("steps"), 4), "challenge": _text(stretch.get("challenge"), 300)}
        if version:
            out[index] = version
    return out


def apply(outline: dict, versions: dict) -> dict:
    """A NEW outline dict with each activity's support/stretch versions added (so the database sees the change)."""
    slides = []
    for i, s in enumerate(outline.get("slides", [])):
        s = dict(s)
        if i in versions:
            s["differentiation"] = versions[i]
        slides.append(s)
    return {**outline, "slides": slides}


def has_differentiation(outline: dict) -> bool:
    return any(s.get("differentiation") for _, s in activity_slides(outline or {}))


def needs_differentiation(outline: dict) -> bool:
    return bool(activity_slides(outline or {})) and not has_differentiation(outline)
