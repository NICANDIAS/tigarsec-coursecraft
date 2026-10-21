"""
Teaching at the class's pace: "how well, not how far". A week's lesson can carry on into the next
teaching week (picking up the SAME PowerPoint from the slide reached), and everything after it moves
one teaching week later along the college calendar. If that pushes the scheme past the end of term,
CourseCraft suggests two later weeks to combine. Pure functions, no database - the lecturer decides.

The scheme's pace is kept on the scheme (SchemeOfWork.pace_json):
  {"extensions": [{"week", "deck_id", "from_slide", "reason", "at"}],   one per extra teaching week
   "merges": [[a, b], ...]}                                             week b taught together with week a
"""
from .live_activities import tally

ASSESSMENT_WORDS = ("assess", "exam", "mock", "test", "deadline", "submission", "hand in", "hand-in", "moderation")


def get_pace(pace_json) -> dict:
    pace = pace_json if isinstance(pace_json, dict) else {}
    exts = [e for e in pace.get("extensions") or [] if isinstance(e, dict) and isinstance(e.get("week"), int)]
    merges = [m for m in pace.get("merges") or [] if isinstance(m, (list, tuple)) and len(m) == 2
              and all(isinstance(x, int) for x in m)]
    return {"extensions": exts, "merges": [list(m) for m in merges]}


def with_extension(pace_json, week, deck_id=None, from_slide=None, reason="", at=""):
    pace = get_pace(pace_json)
    pace["extensions"].append({"week": week, "deck_id": deck_id, "from_slide": from_slide, "reason": (reason or "")[:200], "at": at})
    return pace


def entries(weeks: list, pace_json=None, keep_number_gaps=False) -> list:
    """The scheme in teaching order, one entry per teaching week it will take:
    {"week", "part", "parts", "with": [weeks taught alongside it], "gap": False}. A carried-on week
    appears twice (part 1, part 2); a week merged into another doesn't appear on its own.
    keep_number_gaps: a week NUMBER the scheme has no week for becomes {"gap": True, "week": n}
    (used when there's no college template - the number gap is then the only sign of a break)."""
    pace = get_pace(pace_json)
    weeks = sorted({w for w in weeks if isinstance(w, int)})
    if not weeks:
        return []
    extra = {}
    for e in pace["extensions"]:
        extra[e["week"]] = extra.get(e["week"], 0) + 1
    merged_into = {b: a for a, b in pace["merges"] if a in weeks and b in weeks and a != b}
    out = []
    numbers = range(weeks[0], weeks[-1] + 1) if keep_number_gaps else weeks
    for n in numbers:
        if n not in weeks:
            out.append({"week": n, "gap": True, "part": 1, "parts": 1, "with": []})
            continue
        if n in merged_into:
            continue
        parts = 1 + extra.get(n, 0)
        alongside = sorted(b for b, a in merged_into.items() if a == n)
        for p in range(1, parts + 1):
            out.append({"week": n, "part": p, "parts": parts, "with": alongside, "gap": False})
    return out


def resume_slide(pace_json, week, part):
    """The slide part `part` of `week` picks up from (the extension that made that part)."""
    exts = [e for e in get_pace(pace_json)["extensions"] if e["week"] == week]
    if part < 2 or part - 2 >= len(exts):
        return None
    return exts[part - 2].get("from_slide")


def paced_weeks(weeks_json: list, pace_json=None) -> list:
    """The scheme as it will really be taught, for the PRINTED scheme of work: a carried-on week is
    printed again as "(continued)", and merged weeks are printed together in one week's row."""
    by_no = {w["week"]: w for w in weeks_json or [] if isinstance(w, dict) and isinstance(w.get("week"), int)}
    out = []
    for e in entries(list(by_no), pace_json):
        week = dict(by_no[e["week"]])
        for b in e["with"]:
            other = by_no[b]
            week["title"] = f"{week.get('title', '')} + {other.get('title', '')}".strip(" +")
            week["learning_outcomes"] = list(dict.fromkeys((week.get("learning_outcomes") or []) + (other.get("learning_outcomes") or [])))
            week["activities"] = (week.get("activities") or []) + (other.get("activities") or [])
        if e["part"] > 1:
            slide = resume_slide(pace_json, e["week"], e["part"])
            week["title"] = f"{week.get('title', '')} (continued)"
            week["activities"] = [f"Recap and re-teach what the class found hard, then continue the lesson"
                                  + (f" from slide {slide}" if slide else "")]
            week.pop("practical", None)
            week.pop("assignment", None)
        out.append(week)
    return out


def is_assessment_week(week: dict, planned_assessment_weeks=()) -> bool:
    title = (week.get("title") or "").lower()
    return week.get("week") in set(planned_assessment_weeks or ()) or any(w in title for w in ASSESSMENT_WORDS) \
        or bool(week.get("assignment"))


def suggest_merges(weeks_json: list, after_week: int, pace_json=None, planned_assessment_weeks=(), limit=3) -> list:
    """Two NEIGHBOURING later weeks that could be taught together to win a week back - the pairs
    whose criteria overlap most (then the lightest pairs). Never an assessment week, never a week
    already carried on or merged. -> [{"a", "b", "a_title", "b_title", "shared": [codes], "reason"}]"""
    pace = get_pace(pace_json)
    busy = {e["week"] for e in pace["extensions"]} | {x for m in pace["merges"] for x in m}
    later = sorted((w for w in weeks_json or [] if isinstance(w, dict) and isinstance(w.get("week"), int)
                    and w["week"] > after_week), key=lambda w: w["week"])
    pairs = []
    for a, b in zip(later, later[1:]):
        if a["week"] in busy or b["week"] in busy:
            continue
        if is_assessment_week(a, planned_assessment_weeks) or is_assessment_week(b, planned_assessment_weeks):
            continue
        la, lb = set(a.get("learning_outcomes") or []), set(b.get("learning_outcomes") or [])
        shared = sorted(la & lb)
        overlap = len(shared) / len(la | lb) if la | lb else 0
        reason = (f"Both cover {', '.join(shared[:4])}" if shared else "The two lightest neighbouring weeks")
        pairs.append({"a": a["week"], "b": b["week"], "a_title": a.get("title", ""), "b_title": b.get("title", ""),
                      "shared": shared, "reason": reason, "_rank": (-overlap, len(la | lb), a["week"])})
    pairs.sort(key=lambda p: p["_rank"])
    for p in pairs:
        p.pop("_rank")
    return pairs[:limit]


def lesson_signals(screens: list, responses: list) -> dict:
    """How the class did in a live lesson: {"not_sure_pct" (confidence checks answered "Not yet" or
    "Getting there"), "quiz_pct" (quiz/MCQ questions right), "answers"} - None where there were none."""
    unsure = total_conf = right = total_mcq = 0
    for i, screen in enumerate(screens or []):
        results = tally(screen, [r for r in responses if r.get("screen", i) == i])
        for q in screen.get("questions", []):
            t = results.get(q["key"])
            if not t or not t.get("count"):
                continue
            if q["kind"] == "confidence":
                unsure += sum(t["counts"][:2])
                total_conf += t["count"]
            elif q["kind"] == "mcq":
                right += t.get("correct_count", 0)
                total_mcq += t["count"]
    return {"not_sure_pct": round(100 * unsure / total_conf) if total_conf else None,
            "quiz_pct": round(100 * right / total_mcq) if total_mcq else None,
            "answers": total_conf + total_mcq}


def suggestion(reached, total_slides, signals) -> dict:
    """CourseCraft's suggestion - the lecturer decides. -> {"carry_on": bool, "reasons": [plain-English]}"""
    reasons = []
    if reached and total_slides and reached < total_slides * 0.8:
        reasons.append(f"you reached slide {reached} of {total_slides}")
    if signals.get("not_sure_pct") is not None and signals["not_sure_pct"] >= 40:
        reasons.append(f"{signals['not_sure_pct']}% weren't sure yet")
    if signals.get("quiz_pct") is not None and signals["quiz_pct"] < 60:
        reasons.append(f"only {signals['quiz_pct']}% of quiz answers were right")
    return {"carry_on": bool(reasons), "reasons": reasons}
