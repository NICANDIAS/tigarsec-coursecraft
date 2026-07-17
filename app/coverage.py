"""
Pure logic for computing curriculum coverage gaps - deliberately has zero DB dependency,
so it can be tested thoroughly with mock data before ever touching a real route/template.
"""


def _normalize(code):
    return (code or "").strip().lower()


def classify_mark(final_mark):
    """3-way classification reused from grade aggregation, plus an honest 'unclear' for
    anything that doesn't parse (e.g. a bare numeric rubric score like '7' - we can't say
    met/not-met without knowing that course's scale)."""
    if not final_mark:
        return "unclear"
    text = final_mark.lower()
    if "not" in text:
        return "not_met"
    if "partly" in text or "partial" in text:
        return "partly_met"
    if "met" in text or "achieved" in text or "pass" in text:
        return "met"
    return "unclear"


def _build_taught_lo_codes(units_by_name, schemes_of_work):
    """
    Returns: {unit_name: set_of_normalized_codes_taught}
    Only considers READY (successfully generated) schemes of work. A scheme scoped to
    specific units (selected_unit_names) only contributes its LO codes to THOSE units -
    this is what avoids one unit's SOW accidentally "covering" a same-named LO in another.
    An unscoped scheme (selected_unit_names empty/None) is treated as potentially covering
    ANY unit, since we have no better information to narrow it down.
    """
    taught = {name: set() for name in units_by_name}
    for sow in schemes_of_work:
        if sow.get("status") != "ready" or not sow.get("weeks_json"):
            continue
        scoped_units = sow.get("selected_unit_names") or list(units_by_name.keys())
        codes_this_sow = set()
        for week in sow["weeks_json"]:
            for lo_code in week.get("learning_outcomes", []):
                codes_this_sow.add(_normalize(lo_code))
        for unit_name in scoped_units:
            if unit_name in taught:
                taught[unit_name] |= codes_this_sow
    return taught


def _build_assessment_index(units_by_name, marks):
    """
    Returns: {(unit_name, criterion_code_normalized): [mark_classification, ...]}
    A mark counts as evidence for a unit's criterion if EITHER the submission's own
    target_unit_name matches that unit exactly (reliable), OR the free-text criterion_ref
    contains both the unit name and the criterion code as substrings (fallback for older/
    unscoped submissions) - this is what prevents the "P1 exists in every unit" collision.
    """
    index = {}
    for m in marks:
        ref = (m.get("criterion_ref") or "")
        ref_lower = ref.lower()
        target_unit = m.get("target_unit_name")
        for unit_name in units_by_name:
            unit_matches = (target_unit == unit_name) or (unit_name.lower() in ref_lower)
            if not unit_matches:
                continue
            for lo in units_by_name[unit_name].get("learning_outcomes", []):
                for crit in lo.get("criteria", []):
                    crit_code = crit.get("code", "")
                    if crit_code and crit_code.lower() in ref_lower:
                        key = (unit_name, _normalize(crit_code))
                        index.setdefault(key, []).append(classify_mark(m.get("final_mark")))
    return index


def compute_coverage(units, schemes_of_work, marks):
    """
    units: spec's units list, each {name, learning_outcomes: [{code, title, criteria: [{code, description}]}]}
    schemes_of_work: [{status, selected_unit_names, weeks_json}, ...]
    marks: [{criterion_ref, final_mark, target_unit_name}, ...]

    Returns a per-unit breakdown: for each criterion, one of:
      - "not_taught": its LO code never appeared in any ready scheme of work scoped to this unit
      - "taught_not_assessed": taught, but no marks reference it
      - "assessed": has marks - includes classification counts
    """
    units_by_name = {u["name"]: u for u in units}
    taught_lo_codes = _build_taught_lo_codes(units_by_name, schemes_of_work)
    assessment_index = _build_assessment_index(units_by_name, marks)

    report = []
    for unit in units:
        unit_name = unit["name"]
        criteria_report = []
        for lo in unit.get("learning_outcomes", []):
            lo_taught = _normalize(lo.get("code")) in taught_lo_codes.get(unit_name, set())
            for crit in lo.get("criteria", []):
                crit_code = crit.get("code", "")
                crit_taught = lo_taught or _normalize(crit_code) in taught_lo_codes.get(unit_name, set())
                key = (unit_name, _normalize(crit_code))
                classifications = assessment_index.get(key, [])

                if classifications:
                    status = "assessed"
                elif crit_taught:
                    status = "taught_not_assessed"
                else:
                    status = "not_taught"

                counts = {"met": 0, "partly_met": 0, "not_met": 0, "unclear": 0}
                for c in classifications:
                    counts[c] += 1

                criteria_report.append({
                    "lo_code": lo.get("code"), "criterion_code": crit_code,
                    "description": crit.get("description", ""), "status": status, "counts": counts,
                })
        report.append({"unit_name": unit_name, "criteria": criteria_report})
    return report


def summarize_coverage(report):
    """
    Rolls the per-unit/per-criterion report up into overall counts for the dashboard's
    summary card, plus flags criteria worth surfacing as 'struggling' (more students
    not-met than met) even though they ARE being assessed - a different kind of problem
    than a coverage gap, but worth a teacher's attention too.
    """
    totals = {"not_taught": 0, "taught_not_assessed": 0, "assessed": 0}
    struggling = []
    for unit in report:
        for c in unit["criteria"]:
            totals[c["status"]] += 1
            if c["status"] == "assessed" and c["counts"]["not_met"] > c["counts"]["met"]:
                struggling.append({
                    "unit_name": unit["unit_name"], "criterion_code": c["criterion_code"],
                    "description": c["description"], "counts": c["counts"],
                })
    total_criteria = sum(totals.values())
    return {
        "totals": totals,
        "total_criteria": total_criteria,
        "coverage_pct": round((totals["assessed"] / total_criteria) * 100, 1) if total_criteria else 0,
        "struggling": struggling,
    }
