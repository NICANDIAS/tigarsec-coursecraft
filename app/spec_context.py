"""
Stage 6 (A): tells the AI tools what the SPEC itself says about how a unit is taught and
assessed - not just the list of criteria. Used by lesson decks, schemes of work and exam
practice questions. Pure functions, no database, no AI.

What a spec can now hold (from the readers), and what each tool is told about it:
  * exam specs (A level / GCSE): the unit's exam ("Written examination: 2 hours 15 minutes,
    33 1/3% of qualification") and the assessment objectives AO1-AO4 with their weighting
    for that component -> questions and activities are pitched at, and labelled with, the AO
    they practise;
  * Appendix A quantitative skills -> a short calculation where it fits naturally;
  * BTEC 'Practice' teaching content (Media) -> the indicative content for the outcome being
    taught, so lessons teach what the spec lists, not a guess;
  * statement guidance (an "Additional information" column) and unit aims / guidance (City &
    Guilds) -> kept next to the statement / unit.
A spec that holds none of this (e.g. the Digital T Level specs) gives "" - prompts for those
are unchanged.
"""

MAX_CONTEXT_CHARS = 3500
MAX_AREA_CHARS = 1200


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _component_weighting(assessment, unit):
    """The weighting row for this unit's component ("Component 1" -> AO1 12-14% ...)."""
    code = (unit.get("code") or "").lower()
    for row in assessment.get("weightings") or []:
        if code and row.get("name", "").lower() == code:
            return ", ".join(f"{k} {v}" for k, v in row.items() if k != "name")
    return ""


def exam_context(spec_json, unit) -> str:
    assessment = (spec_json or {}).get("assessment")
    if not isinstance(assessment, dict) or not assessment.get("objectives"):
        return ""
    lines = ["HOW THIS IS ASSESSED (from the spec): by written examination, marked against these assessment objectives:"]
    for ao in assessment["objectives"]:
        lines.append(f"  {ao.get('code')}: {_short(ao.get('description'), 260)}")
    if isinstance(unit.get("assessment"), str) and unit["assessment"]:
        lines.append(f"This unit's exam: {unit['assessment']}.")
    weighting = _component_weighting(assessment, unit)
    if weighting:
        lines.append(f"Weighting of each objective in this unit's exam: {weighting}.")
    lines.append("Pitch questions and activities at these objectives (knowledge, application, analysis, evaluation) "
                 "and label each question you write with the objective it practises, e.g. \"(AO2)\".")
    return "\n".join(lines)


def quantitative_context(spec_json) -> str:
    for u in (spec_json or {}).get("units", []):
        if (u.get("code") or "").lower().startswith("appendix") and "quantitative" in (u.get("name") or "").lower():
            skills = [c.get("description", "") for lo in u.get("learning_outcomes", []) for c in lo.get("criteria", [])]
            if skills:
                return ("QUANTITATIVE SKILLS the spec says are assessed across the exams - include a short calculation "
                        "or data task where it fits this topic naturally: " + "; ".join(_short(s, 120) for s in skills[:10]) + ".")
    return ""


def teaching_content_for(spec_json, unit, lo) -> str:
    """The BTEC 'Practice' indicative teaching content for one outcome (Media), or ""."""
    areas = (spec_json or {}).get("content_areas")
    area = lo.get("content_area")
    if not isinstance(areas, dict) or not area:
        return ""
    module = unit.get("module")
    entry = (areas.get(module) or {}).get(area) if module else None
    if not entry:
        entry = next((m.get(area) for m in areas.values() if isinstance(m, dict) and m.get(area)), None)
    if not isinstance(entry, dict):
        return ""
    parts = [_short(entry.get("summary"), 300)] if entry.get("summary") else []
    parts += [("- " if i.get("level", 1) <= 1 else "  - ") + _short(i.get("text"), 160) for i in entry.get("items", [])]
    text = "\n".join(parts)
    if len(text) > MAX_AREA_CHARS:  # keep the line breaks; cut at a line end
        text = text[:MAX_AREA_CHARS].rsplit("\n", 1)[0] + "\n  ..."
    return f"Teaching content the spec lists for '{area}' (indicative):\n{text}"


def unit_notes(unit) -> str:
    lines = []
    if unit.get("aim"):
        lines.append(f"Unit aim (from the spec): {_short(unit['aim'], 500)}")
    if unit.get("guidance"):
        lines.append(f"Delivery guidance (from the spec): {_short(unit['guidance'], 700)}")
    return "\n".join(lines)


def spec_teaching_context(spec_json, unit, los=None) -> str:
    """Everything above for one unit (and, if given, the outcomes being taught), as one block
    to add to an AI prompt. "" when the spec holds nothing extra."""
    if not unit:
        return ""
    blocks = [exam_context(spec_json, unit), quantitative_context(spec_json) if exam_context(spec_json, unit) else "", unit_notes(unit)]
    seen = set()
    for lo in los if los is not None else unit.get("learning_outcomes", []):
        text = teaching_content_for(spec_json, unit, lo)
        if text and text not in seen:
            seen.add(text)
            blocks.append(text)
    text = "\n\n".join(b for b in blocks if b)
    return text if len(text) <= MAX_CONTEXT_CHARS else text[:MAX_CONTEXT_CHARS].rsplit("\n", 1)[0] + "\n..."


def criterion_extra(criterion) -> str:
    """Guidance printed next to a statement (an "Additional information" column), or ""."""
    return f"    (guidance: {_short(criterion['guidance'], 300)})" if criterion.get("guidance") else ""


def has_exam_objectives(spec_json) -> bool:
    assessment = (spec_json or {}).get("assessment")
    return isinstance(assessment, dict) and bool(assessment.get("objectives"))
