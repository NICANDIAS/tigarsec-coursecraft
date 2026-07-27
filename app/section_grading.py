"""
Computes Pass/Fail results using real summed arithmetic - deliberately NOT delegated to the
AI, since asking a model to reliably sum several numbers and declare a threshold result is
trusting it with exactly the kind of multi-step arithmetic LLMs are known to get wrong
sometimes, on something with real consequences (a genuine Pass/Fail outcome).

Standing rule (per marker feedback after real submission testing): the pass threshold AND
the weighting of each part are marker-supplied per assignment, never hardcoded (not 70%,
not a 45/55 split) - both are read from what the marker actually gave, and the app states
which values it used so the numbers are auditable.
"""


def compute_grading_results(
    items: list[dict],
    pass_threshold_pct: float | None,
    part_weightings: dict[str, float] | None = None,
) -> dict:
    """
    items: [{"criterion_ref": str|None, "section": str, "score": float, "max_score": float}, ...]
    from BOTH AI-derived numeric marks AND any pre-graded components (e.g. an already-marked
    MCQ), treated identically once they reach this function.

    pass_threshold_pct: the marker-supplied overall pass percentage for THIS assignment -
    always passed in explicitly by the caller (e.g. submission.pass_threshold_pct), never
    defaulted silently here.

    part_weightings: the marker-supplied weight of each section/part, e.g. {"MCQ": 45,
    "Longform": 55}. If None, or if it doesn't cover every section present in `items`, the
    weighted overall falls back to an EQUAL weighting across whichever sections are present -
    and `weighting_source` reports this explicitly so it's never silently mistaken for a
    marker-specified split.

    Returns a dict:
    {
      "sections": {name: {"total", "max", "pct", "passed"}, ...},   # per-section arithmetic,
          each independently checked against pass_threshold_pct (a "two-gate" style check -
          every section must clear the bar on its own, not blended away)
      "all_sections_passed": bool,          # True only if every section above passed
      "weighted_overall_pct": float|None,   # None only if there is no scoreable data at all
      "weighted_overall_passed": bool|None,
      "weighting_used": {name: weight_pct, ...},  # the ACTUAL weights used in the calculation
      "weighting_source": "marker_supplied" | "equal_weighting_fallback",
      "pass_threshold_pct": float,          # echoed back so the output is self-contained/auditable
      "criteria_below_threshold": [
          {"criterion_ref": str, "section": str, "pct": float, "score": float, "max_score": float}
      ],  # EVERY individual criterion below threshold, even if the weighted overall clears it -
          # this rubric-type is criteria-referenced, so a learner can pass on weighted average
          # while still failing individual criteria, and both facts must be reported
    }
    """
    sections: dict[str, dict] = {}
    criteria_below_threshold = []

    for item in items:
        if item.get("score") is None or item.get("max_score") is None:
            continue  # can't include an item with no numeric value in a real sum
        section_name = item.get("section") or "default"
        s = sections.setdefault(section_name, {"total": 0.0, "max": 0.0})
        s["total"] += item["score"]
        s["max"] += item["max_score"]

        if pass_threshold_pct is not None and item["max_score"] > 0:
            item_pct = (item["score"] / item["max_score"]) * 100
            if item_pct < pass_threshold_pct:
                criteria_below_threshold.append({
                    "criterion_ref": item.get("criterion_ref") or "(unlabelled)",
                    "section": section_name,
                    "pct": round(item_pct, 1),
                    "score": item["score"],
                    "max_score": item["max_score"],
                })

    section_results = {}
    for name, s in sections.items():
        pct = (s["total"] / s["max"] * 100) if s["max"] > 0 else 0.0
        section_results[name] = {
            "total": s["total"],
            "max": s["max"],
            "pct": round(pct, 1),
            "passed": (pct >= pass_threshold_pct) if pass_threshold_pct is not None else None,
        }

    all_sections_passed = (
        bool(section_results) and all(r["passed"] for r in section_results.values())
        if pass_threshold_pct is not None else None
    )

    # Weighted overall percentage - uses the marker's own weightings if they were given AND
    # cover every section actually present; otherwise falls back to an equal split across
    # whichever sections are present, and says so via weighting_source rather than pretending
    # a marker-specified split was used.
    weighted_overall_pct = None
    weighted_overall_passed = None
    weighting_used = {}
    weighting_source = None

    if section_results:
        section_names = list(section_results.keys())
        supplied = part_weightings or {}
        covers_all = all(name in supplied for name in section_names)

        if covers_all and supplied:
            weighting_used = {name: float(supplied[name]) for name in section_names}
            weighting_source = "marker_supplied"
        else:
            equal_share = 100.0 / len(section_names)
            weighting_used = {name: equal_share for name in section_names}
            weighting_source = "equal_weighting_fallback"

        total_weight = sum(weighting_used.values()) or 1.0
        weighted_overall_pct = sum(
            section_results[name]["pct"] * (weighting_used[name] / total_weight)
            for name in section_names
        )
        weighted_overall_pct = round(weighted_overall_pct, 1)
        weighted_overall_passed = (
            weighted_overall_pct >= pass_threshold_pct if pass_threshold_pct is not None else None
        )

    return {
        "sections": section_results,
        "all_sections_passed": all_sections_passed,
        "weighted_overall_pct": weighted_overall_pct,
        "weighted_overall_passed": weighted_overall_passed,
        "weighting_used": {k: round(v, 1) for k, v in weighting_used.items()},
        "weighting_source": weighting_source,
        "pass_threshold_pct": pass_threshold_pct,
        "criteria_below_threshold": criteria_below_threshold,
    }


def compute_section_results(items: list[dict], pass_threshold_pct: float = 70.0) -> tuple[dict, bool]:
    """
    DEPRECATED shape, kept only so anything importing the old two-value return signature
    doesn't break. New code should call compute_grading_results() directly, which is what
    this now delegates to. The 70.0 default here is likewise legacy - real callers must pass
    the marker's own threshold explicitly (see routers/submissions.py).
    """
    full = compute_grading_results(items, pass_threshold_pct=pass_threshold_pct)
    return full["sections"], full["all_sections_passed"]
