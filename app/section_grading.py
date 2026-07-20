"""
Computes Pass/Fail per "section" (e.g. an MCQ component vs a Longform component) using
real summed arithmetic - deliberately NOT delegated to the AI, since asking a model to
reliably sum several numbers and declare a threshold result is trusting it with exactly
the kind of multi-step arithmetic LLMs are known to get wrong sometimes, on something with
real consequences (a genuine Pass/Fail outcome).
"""


def compute_section_results(items: list[dict], pass_threshold_pct: float = 70.0) -> tuple[dict, bool]:
    """
    items: [{"section": str, "score": float, "max_score": float}, ...] - from BOTH
    AI-derived numeric marks AND any pre-graded components (e.g. an already-marked MCQ),
    treated identically once they reach this function.

    Returns (per_section_results, overall_passed) - overall_passed requires EVERY section
    to independently clear the threshold (a two-gate rule, e.g. "MCQ >=70% AND Longform
    >=70% separately", not a single blended average across everything).
    """
    sections: dict[str, dict] = {}
    for item in items:
        if item.get("score") is None or item.get("max_score") is None:
            continue  # can't include an item with no numeric value in a real sum
        s = sections.setdefault(item["section"], {"total": 0.0, "max": 0.0})
        s["total"] += item["score"]
        s["max"] += item["max_score"]

    results = {}
    for name, s in sections.items():
        pct = (s["total"] / s["max"] * 100) if s["max"] > 0 else 0.0
        results[name] = {
            "total": s["total"],
            "max": s["max"],
            "pct": round(pct, 1),
            "passed": pct >= pass_threshold_pct,
        }

    overall_passed = bool(results) and all(r["passed"] for r in results.values())
    return results, overall_passed
