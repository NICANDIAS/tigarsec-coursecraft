"""
Pure logic for computing curriculum coverage gaps - deliberately has zero DB dependency,
so it can be tested thoroughly with mock data before ever touching a real route/template.
"""
import re


def _normalize(code):
    return (code or "").strip().lower()


def _code_appears_in(code, label) -> bool:
    """
    Standing fix: a scheme of work's own "learning_outcomes" field isn't always the
    spec's clean criterion code alone - confirmed as a real problem for specs with no
    named LO layer (a flat content area like "2.1, 2.2, ... 2.25" with no "LO1/LO2"
    grouping above it), where the AI's own week-by-week label style tends to look like
    "Unit LO general-2 (2.1)" rather than just "2.1". An exact match against that whole
    string was silently marking genuinely-taught criteria as "not taught".

    This checks whether the code appears as a whole token inside the label instead (word-
    boundary matched, the same technique used for the P1/P10 rubric-matching collision) -
    so "2.1" is found inside "Unit LO general-2 (2.1)", but "2.1" is NOT wrongly matched
    inside "2.10" or "12.1".
    """
    code = _normalize(code)
    label = _normalize(label)
    if not code or not label:
        return False
    if code == label:
        return True
    pattern = re.compile(r'(?<![a-z0-9.])' + re.escape(code) + r'(?![a-z0-9.])')
    return pattern.search(label) is not None


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


def _build_taught_lo_labels(units_by_name, schemes_of_work):
    """
    Returns: {unit_name: [raw_learning_outcome_label, ...]}
    Only considers READY (successfully generated) schemes of work. A scheme scoped to
    specific units (selected_unit_names) only contributes its LO labels to THOSE units -
    this is what avoids one unit's SOW accidentally "covering" a same-named LO in another.
    An unscoped scheme (selected_unit_names empty/None) is treated as potentially covering
    ANY unit, since we have no better information to narrow it down. Labels are kept as
    the AI actually wrote them (not reduced to a normalized code) - see _code_appears_in
    for why an exact-match set isn't reliable here.
    """
    taught = {name: [] for name in units_by_name}
    for sow in schemes_of_work:
        if sow.get("status") != "ready" or not sow.get("weeks_json"):
            continue
        scoped_units = sow.get("selected_unit_names") or list(units_by_name.keys())
        labels_this_sow = []
        for week in sow["weeks_json"]:
            for lo_label in week.get("learning_outcomes", []):
                if lo_label:
                    labels_this_sow.append(lo_label)
        for unit_name in scoped_units:
            if unit_name in taught:
                taught[unit_name].extend(labels_this_sow)
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
    taught_lo_labels = _build_taught_lo_labels(units_by_name, schemes_of_work)
    assessment_index = _build_assessment_index(units_by_name, marks)

    report = []
    for unit in units:
        unit_name = unit["name"]
        criteria_report = []
        unit_labels = taught_lo_labels.get(unit_name, [])
        for lo in unit.get("learning_outcomes", []):
            lo_code = lo.get("code") or ""
            # A real named LO (e.g. "LO2") genuinely groups criteria taught together, so
            # the whole LO being referenced is good evidence every criterion under it was
            # covered. The synthetic "general-N" placeholder (see spec_parser.py) is NOT
            # that - it's just a bucket holding many otherwise-ungrouped criteria, and the
            # AI's own week labels often mention it by name incidentally (e.g. "Unit LO
            # general-2 (2.1)") without that implying every OTHER criterion in the bucket
            # was also taught. Confirmed as a real false-positive risk via testing - a
            # criterion never taught was showing as taught purely because a sibling
            # criterion's label happened to mention the shared bucket name.
            lo_taught = (
                not lo_code.startswith("general-")
                and any(_code_appears_in(lo_code, label) for label in unit_labels)
            )
            for crit in lo.get("criteria", []):
                crit_code = crit.get("code", "")
                crit_taught = lo_taught or any(_code_appears_in(crit_code, label) for label in unit_labels)
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
