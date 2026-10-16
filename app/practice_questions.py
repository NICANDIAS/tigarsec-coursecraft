"""
Stage 6 (A): exam-style practice questions for exam-assessed courses (A level, GCSE).

The AI is given the spec's OWN statements for the chosen topics, the assessment objectives
AO1-AO4 with this unit's weighting, the exam format and the quantitative skills - and writes
a short set of questions in the style of the real papers: short knowledge questions, applied
questions on a short case, and an extended "evaluate" question with a levels mark scheme.
Everything comes back as data and is checked here before it is shown: a question with no
text, silly marks or an unknown objective is dropped, not displayed.
"""
import re

from .spec_context import exam_context, quantitative_context

MAX_QUESTIONS = 10
AO_RE = re.compile(r"^AO[1-9]$")

PRACTICE_SYSTEM_PROMPT = """You write exam-style practice questions for a UK A level / GCSE lecturer, \
strictly from the specification content you are given. Write questions in the style of a real exam \
paper for this qualification:
- a mix of short questions (1-4 marks, mostly knowledge and application) and longer ones (6-12 marks, \
analysis and evaluation);
- applied questions are set on a SHORT made-up business/case context (2-4 sentences, invented names - \
never a real company or person);
- label every question with the assessment objective(s) it tests, using ONLY the codes given (AO1, AO2...), \
and spread the questions roughly in line with the weighting given;
- every question has a mark scheme: indicative content points an examiner would credit; questions worth \
8 marks or more ALSO get level descriptors (e.g. Level 3 7-9 marks: ...);
- only use content from the statements given - do not test anything outside them.

Return ONLY compact raw JSON (no markdown fences, no commentary), exactly this shape:
{"questions": [{"question": "string", "context": "string or empty", "marks": 4, "ao": ["AO2"], \
"mark_scheme": ["string", ...], "levels": [{"level": "Level 2 (4-6 marks)", "descriptor": "string"}]}]}"""


def build_prompt(spec_json, unit, los, count):
    lines = [f"Write {count} practice questions.", f"Unit: {unit.get('name')}", "", "Specification content to test:"]
    for lo in los:
        lines.append(f"{lo.get('code')} {lo.get('title')}")
        for c in lo.get("criteria", []):
            lines.append(f"  - {c.get('description')}")
    context = exam_context(spec_json, unit)
    if context:
        lines += ["", context]
    quant = quantitative_context(spec_json)
    if quant:
        lines += ["", quant]
    return "\n".join(lines)


def _text(value, limit=2000):
    return " ".join(str(value or "").split())[:limit]


def clean_questions(result, allowed_aos):
    """Keeps only well-formed questions. Returns (questions, dropped_count)."""
    raw = result.get("questions") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return [], 0
    allowed = {a.upper() for a in allowed_aos} or {"AO1", "AO2", "AO3", "AO4"}
    out, dropped = [], 0
    for q in raw[:MAX_QUESTIONS]:
        if not isinstance(q, dict) or not _text(q.get("question")):
            dropped += 1
            continue
        try:
            marks = int(q.get("marks"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not 1 <= marks <= 30:
            dropped += 1
            continue
        aos = [str(a).strip().upper() for a in (q.get("ao") or []) if AO_RE.match(str(a).strip().upper())]
        aos = [a for a in aos if a in allowed]
        if not aos:
            dropped += 1
            continue
        scheme = [_text(p, 500) for p in (q.get("mark_scheme") or []) if _text(p)]
        levels = [{"level": _text(l.get("level"), 80), "descriptor": _text(l.get("descriptor"), 600)}
                  for l in (q.get("levels") or []) if isinstance(l, dict) and _text(l.get("descriptor"))]
        out.append({"question": _text(q["question"]), "context": _text(q.get("context"), 1200), "marks": marks,
                    "ao": sorted(set(aos)), "mark_scheme": scheme[:15], "levels": levels[:5]})
    return out, dropped


def ao_summary(questions):
    """How many marks test each objective - shown above the set, so a lecturer can see the balance."""
    totals = {}
    for q in questions:
        share = q["marks"] / max(len(q["ao"]), 1)
        for a in q["ao"]:
            totals[a] = totals.get(a, 0) + share
    return {a: round(v) for a, v in sorted(totals.items())}
