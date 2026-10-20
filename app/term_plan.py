"""
Plan the term: the parts with no database - which weeks to offer, the quick picks ("next 4
weeks", "to half term", "rest of term"), how long it will take, and how far along a plan is.
The router (app/routers/term_plan.py) makes the lessons; this only decides and counts.
"""

MAX_WEEKS = 40          # a whole year's scheme at most in one plan
MIN_MINUTES, MAX_MINUTES = 30, 6 * 60
MIN_QUIZ, MAX_QUIZ, DEFAULT_QUIZ = 3, 15, 8


def plan_rows(weeks: list, current_week, made_weeks: set, week_dates=None, breaks=None, today=None) -> list:
    """weeks: the scheme's [{"week", "title"}]. -> rows in teaching order. Week rows:
    {"week", "title", "date", "made" (a lesson exists), "now" (this week), "past", "gap": False}.
    Gap rows {"week": None, "gap": True, "label", "date"} are weeks with no teaching: with a college
    template (week_dates + breaks from app/sow_calendar.py) they are its own rows, e.g. "Half Term";
    without one, a week NUMBER the scheme has no week for."""
    by_no = {w["week"]: w for w in weeks or [] if isinstance(w, dict) and isinstance(w.get("week"), int)}
    if not by_no:
        return []
    week_dates = week_dates or {}
    after = {}
    for b in breaks or []:
        after.setdefault(b.get("after_week"), []).append(b)
    rows = []
    numbers = sorted(by_no) if week_dates else range(min(by_no), max(by_no) + 1)
    for n in numbers:
        w = by_no.get(n)
        if not w:
            rows.append({"week": None, "gap": True, "label": f"No week {n} in the scheme", "date": None})
            continue
        date = week_dates.get(n)
        if date and today:
            past = (today - date).days >= 7
        else:
            past = bool(current_week) and n < current_week
        rows.append({"week": n, "title": w.get("title") or f"Week {n}", "date": date, "made": n in made_weeks,
                     "now": current_week == n, "past": past, "gap": False})
        for b in after.get(n, []):
            rows.append({"week": None, "gap": True, "label": b.get("label") or "No teaching", "date": b.get("date")})
    return rows


def timeline_rows(timeline: list, weeks_json: list, now: dict, made_weeks: set, today=None, pace_json=None) -> list:
    """The planning page's rows from the dated teaching timeline (app/sow_calendar.py): a week carried
    on into the next shows again as "(continued)" with the slide it picks up from; combined weeks show
    as one; breaks are rows of their own. Same row shape as plan_rows, plus "part", "continued", "with", "resume"."""
    import datetime
    from .pace import resume_slide
    by_no = {w["week"]: w for w in weeks_json or [] if isinstance(w, dict) and isinstance(w.get("week"), int)}
    rows = []
    for t in timeline:
        if t["kind"] == "gap":
            rows.append({"week": None, "gap": True, "label": t["label"], "date": t["date"]})
            continue
        n, w = t["week"], by_no.get(t["week"], {})
        title = w.get("title") or f"Week {n}"
        for b in t.get("with") or []:
            title += f" + {by_no.get(b, {}).get('title') or f'Week {b}'}"
        date = t.get("date")
        past = bool(date and today and (today - date).days >= 7)
        if not date and now.get("week") and now.get("state") in ("teaching", "break", "before"):
            past = n < now["week"]
        continued = t["part"] > 1
        rows.append({"week": n, "part": t["part"], "parts": t["parts"], "continued": continued, "with": t.get("with") or [],
                     "title": title + (" (continued)" if continued else ""), "date": date,
                     "made": n in made_weeks or continued, "resume": resume_slide(pace_json, n, t["part"]) if continued else None,
                     "now": now.get("week") == n and now.get("part", 1) == t["part"], "past": past, "gap": False})
    return rows


def quick_pick(rows: list, current_week, preset: str) -> list:
    """The week numbers a quick-pick button ticks: never weeks already made, never gaps, never
    past weeks. "next4" = the next 4 teaching weeks; "half" = up to the next break (half term);
    "rest" = every week left."""
    start = current_week if current_week and current_week > 0 else 1
    first = next((i for i, r in enumerate(rows) if not r["gap"] and r["week"] >= start), len(rows))
    picked = []
    for r in rows[first:]:
        if r["gap"]:
            if preset == "half" and picked:
                break
            continue
        if r.get("made") or r.get("past") or r.get("continued"):
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
