"""
Pure logic for computing curriculum coverage gaps - deliberately has zero DB dependency,
so it can be tested thoroughly with mock data before ever touching a real route/template.
"""
import re
import datetime


def _week_is_complete(week_number, course_start_date, today) -> bool:
    """
    A week only counts as "taught" once its own final teaching day has actually passed -
    not just because the week has technically started. Confirmed as the intended
    semantics directly: a Friday class should read as taught starting Friday (the day it
    happens), not from the Monday of that same week just because the week has begun.

    Assumes a Monday-Friday teaching week, matching this app's own DAYS_OF_WEEK
    convention: week N runs from course_start_date + (N-1)*7 days through that same date
    + 4 days (its Friday) - "complete" once today reaches that Friday.
    """
    if week_number is None:
        return False
    week_start = course_start_date + datetime.timedelta(days=(week_number - 1) * 7)
    week_end = week_start + datetime.timedelta(days=4)
    return today >= week_end


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


def _build_taught_lo_labels(units_by_name, schemes_of_work, course_start_date=None, today=None):
    """
    Returns: (taught, repeated_lo_codes, taught_leaf_codes)
      taught: {unit_name: [raw_learning_outcome_label, ...]} - for the existing LO-grouping match
      repeated_lo_codes: {unit_name: {lo_code, ...}} - LOs whose code is mentioned across 2+
        distinct weeks, meaning it's being delivered incrementally rather than covered all
        at once in a single week
      taught_leaf_codes: {unit_name: {criterion_code, ...}} - specific criteria evidenced by
        a completed week's own assignment, the one place a SOW records leaf-level codes
        directly rather than the coarser parent LO code

    Only considers READY (successfully generated) schemes of work. A scheme scoped to
    specific units (selected_unit_names) only contributes its LO labels to THOSE units -
    this is what avoids one unit's SOW accidentally "covering" a same-named LO in another.
    An unscoped scheme (selected_unit_names empty/None) is treated as potentially covering
    ANY unit, since we have no better information to narrow it down. Labels are kept as
    the AI actually wrote them (not reduced to a normalized code) - see _code_appears_in
    for why an exact-match set isn't reliable here.

    A week's LOs only count as taught once that week is genuinely COMPLETE (see
    _week_is_complete) - confirmed as a real, necessary fix: without this, generating a
    scheme of "ready" immediately marked every week's content as taught, including weeks
    that haven't happened yet at all. If course_start_date isn't set, falls back to
    counting every week regardless - a deliberate choice, not an oversight: flipping
    straight to "nothing is taught yet" the moment this feature ships would be a jarring,
    unrequested change for anyone who hasn't set a start date. today defaults to the real
    current date but is injectable, keeping this module's existing zero-DB-dependency,
    fully-testable design intact.

    Second real bug found and fixed here: a spec's LO can itself be split into further-
    numbered sub-criteria (e.g. "5.1" grouping 5.1.1/5.1.2/5.1.3), and a course can
    deliberately spread those across SEVERAL weeks rather than one - but the SOW's own
    week-level "learning_outcomes" field records only the coarser parent code ("5.1") for
    EVERY one of those weeks, with nothing to distinguish "this week covered all of 5.1"
    from "this week covered only part of it". Confirmed as a real false positive via the
    user's own SOW: week 1 taught only 5.1.1's content, but the existing LO-grouping
    shortcut (parent code mentioned => everything under it counts as taught) marked 5.1.2
    and 5.1.3 as taught too, even though those are genuinely week 2's content and hadn't
    happened yet. repeated_lo_codes/taught_leaf_codes above are what let compute_coverage
    disable that shortcut specifically where it's unsafe, without disabling it for the far
    more common case of an LO genuinely delivered in one coherent week.
    """
    if today is None:
        today = datetime.date.today()
    taught = {name: [] for name in units_by_name}
    lo_week_appearances = {name: {} for name in units_by_name}
    taught_leaf_codes = {name: set() for name in units_by_name}

    for sow in schemes_of_work:
        if sow.get("status") != "ready" or not sow.get("weeks_json"):
            continue
        scoped_units = sow.get("selected_unit_names") or list(units_by_name.keys())
        labels_this_sow = []
        for week in sow["weeks_json"]:
            week_number = week.get("week")
            week_labels = [lbl for lbl in week.get("learning_outcomes", []) if lbl]
            is_complete = course_start_date is None or _week_is_complete(week_number, course_start_date, today)

            # Tracked regardless of completion - this is about the SOW's own planned
            # structure (an LO spread across several weeks), which is true whether or not
            # those weeks have actually happened yet.
            for unit_name in scoped_units:
                if unit_name not in units_by_name:
                    continue
                for lo in units_by_name[unit_name].get("learning_outcomes", []):
                    lo_code = lo.get("code") or ""
                    if lo_code and any(_code_appears_in(lo_code, lbl) for lbl in week_labels):
                        lo_week_appearances[unit_name].setdefault(_normalize(lo_code), set()).add(week_number)

            if not is_complete:
                continue
            labels_this_sow.extend(week_labels)

            assignment = week.get("assignment") or {}
            for code in (assignment.get("criteria_covered") or []):
                for unit_name in scoped_units:
                    if unit_name in taught_leaf_codes:
                        taught_leaf_codes[unit_name].add(_normalize(code))

        for unit_name in scoped_units:
            if unit_name in taught:
                taught[unit_name].extend(labels_this_sow)

    repeated_lo_codes = {
        unit_name: {code for code, weeks_seen in appearances.items() if len(weeks_seen) > 1}
        for unit_name, appearances in lo_week_appearances.items()
    }
    return taught, repeated_lo_codes, taught_leaf_codes


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


def compute_coverage(units, schemes_of_work, marks, course_start_date=None, today=None):
    """
    units: spec's units list, each {name, learning_outcomes: [{code, title, criteria: [{code, description}]}]}
    schemes_of_work: [{status, selected_unit_names, weeks_json}, ...]
    marks: [{criterion_ref, final_mark, target_unit_name}, ...]
    course_start_date: when teaching actually began, used to work out which SOW weeks
    have genuinely happened yet - if not given, every week counts (see
    _build_taught_lo_labels for why that's a deliberate fallback, not an oversight).

    Returns a per-unit breakdown: for each criterion, one of:
      - "not_taught": its LO code never appeared in any ready scheme of work scoped to this unit
      - "taught_not_assessed": taught, but no marks reference it
      - "assessed": has marks - includes classification counts
    """
    units_by_name = {u["name"]: u for u in units}
    taught_lo_labels, repeated_lo_codes, taught_leaf_codes = _build_taught_lo_labels(units_by_name, schemes_of_work, course_start_date, today)
    assessment_index = _build_assessment_index(units_by_name, marks)

    report = []
    for unit in units:
        unit_name = unit["name"]
        criteria_report = []
        unit_labels = taught_lo_labels.get(unit_name, [])
        unit_repeated_lo_codes = repeated_lo_codes.get(unit_name, set())
        unit_leaf_codes = taught_leaf_codes.get(unit_name, set())
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
            #
            # Also disabled when the LO's own code is repeated across multiple weeks (see
            # repeated_lo_codes) - that's a spec-level LO deliberately spread across
            # several weeks (e.g. "5.1" split into 5.1.1/5.1.2/5.1.3 taught in different
            # weeks), where the SOW records only the coarser parent code every time,
            # giving no way to tell "this week covered all of it" from "this week covered
            # only part of it". Confirmed as a real false positive via the user's own
            # scheme of work - falls back to a direct leaf-code match or the week's own
            # assignment evidence instead, both handled below per criterion.
            lo_taught = (
                not lo_code.startswith("general-")
                and _normalize(lo_code) not in unit_repeated_lo_codes
                and any(_code_appears_in(lo_code, label) for label in unit_labels)
            )
            for crit in lo.get("criteria", []):
                crit_code = crit.get("code", "")
                crit_taught = (
                    lo_taught
                    or any(_code_appears_in(crit_code, label) for label in unit_labels)
                    or _normalize(crit_code) in unit_leaf_codes
                )
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
