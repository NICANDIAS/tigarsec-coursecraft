"""
Plan the term: the parts with no database - which weeks to offer, the quick picks ("next 4
weeks", "to half term", "rest of term"), how long it will take, and how far along a plan is.
The router (app/routers/term_plan.py) makes the lessons; this only decides and counts.
"""

MAX_WEEKS = 40          # a whole year's scheme at most in one plan
MIN_MINUTES, MAX_MINUTES = 30, 6 * 60
MIN_QUIZ, MAX_QUIZ, DEFAULT_QUIZ = 3, 15, 8


def plan_rows(weeks: list, current_week, made_weeks: set) -> list:
    """weeks: the scheme's [{"week", "title"}]. -> rows in order, one per week from the first to
    the last; a number the scheme has no week for (half term, a closure) is {"week", "gap": True}.
    Other rows: {"week", "title", "made" (a lesson already exists), "now" (this week), "past"}."""
    by_no = {w["week"]: w for w in weeks or [] if isinstance(w, dict) and isinstance(w.get("week"), int)}
    if not by_no:
        return []
    rows = []
    for n in range(min(by_no), max(by_no) + 1):
        w = by_no.get(n)
        if not w:
            rows.append({"week": n, "gap": True})
            continue
        rows.append({"week": n, "title": w.get("title") or f"Week {n}", "made": n in made_weeks,
                     "now": current_week == n, "past": bool(current_week) and n < current_week, "gap": False})
    return rows


def quick_pick(rows: list, current_week, preset: str) -> list:
    """The week numbers a quick-pick button ticks: never weeks already made, never gaps, never
    past weeks. "next4" = the next 4 teaching weeks; "half" = up to the next gap in the plan
    (half term); "rest" = every week left."""
    start = current_week if current_week and current_week > 0 else 1
    ahead = [r for r in rows if r["week"] >= start]
    picked = []
    for r in ahead:
        if r["gap"]:
            if preset == "half" and picked:
                break
            continue
        if r["made"]:
            continue
        picked.append(r["week"])
        if preset == "next4" and len(picked) == 4:
            break
    return picked


def estimate_minutes(lessons: int, with_quiz: bool, with_versions: bool) -> tuple:
    """A rough (low, high) in minutes for the whole plan - lessons are made one after another."""
    each_low, each_high = 2, 4
    if with_quiz:
        each_low, each_high = each_low + 1, each_high + 1
    if with_versions:
        each_high += 1
    return lessons * each_low, lessons * each_high


def clamp_minutes(hours) -> int:
    try:
        minutes = round(float(hours) * 60)
    except (TypeError, ValueError):
        minutes = 60
    return max(MIN_MINUTES, min(MAX_MINUTES, minutes))


def clamp_quiz(count) -> int:
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = DEFAULT_QUIZ
    return max(MIN_QUIZ, min(MAX_QUIZ, count))


def item_status(deck_status, quiz_status, wants_quiz: bool) -> str:
    """One week's overall state: "waiting", "working", "ready", "failed" or "gone" (deleted)."""
    if deck_status is None:
        return "gone"
    if deck_status == "failed" or (wants_quiz and quiz_status == "failed"):
        return "failed"
    if deck_status == "ready" and (not wants_quiz or quiz_status == "ready"):
        return "ready"
    if deck_status == "queued" and not quiz_status:
        return "waiting"
    return "working"


def progress(statuses: list) -> dict:
    """statuses: item_status() for each week -> {"done", "total", "ready", "failed", "pct", "finished"}."""
    live = [s for s in statuses if s != "gone"]
    ready = live.count("ready")
    failed = live.count("failed")
    total = len(live)
    done = ready + failed
    return {"done": done, "total": total, "ready": ready, "failed": failed,
            "pct": round(100 * done / total) if total else 100, "finished": done == total}
