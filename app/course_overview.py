"""
Course page redesign: the "This week" card - which week of the scheme of work we are in today,
what it teaches, and whether its lesson has been made yet. Pure functions, no database.

Week N runs from start_date + (N-1)*7 days - the same counting the coverage dashboard uses
(app/coverage.py), so the two pages always agree on what week it is.
"""
import datetime


def week_number(start_date, today):
    """1 for the first week, 2 for the next... 0 or less = teaching hasn't started. None = no start date."""
    if not start_date:
        return None
    return (today - start_date).days // 7 + 1


def _pick_scheme(schemes):
    """The newest ready scheme of work that has weeks. schemes are newest first (Course.schemes_of_work)."""
    for sow in schemes or []:
        if sow.get("status") == "ready" and sow.get("weeks"):
            return sow
    return None


def this_week(schemes, course_start, decks, today=None):
    """schemes: [{"id", "title", "status", "start_date", "weeks": [{"week", "title", "learning_outcomes"}]}] newest first.
    decks: [{"id", "status", "source_sow_id", "source_week"}] newest first.
    -> None when there is no ready scheme, else
       {"state": "no_date" | "before" | "teaching" | "finished", "sow_id", "sow_title", "week", "total",
        "title", "outcomes", "next_title", "days_to_start", "deck"}"""
    sow = _pick_scheme(schemes)
    if not sow:
        return None
    today = today or datetime.date.today()
    weeks = sorted((w for w in sow["weeks"] if isinstance(w, dict) and isinstance(w.get("week"), int)), key=lambda w: w["week"])
    if not weeks:
        return None
    by_no = {w["week"]: w for w in weeks}
    total = weeks[-1]["week"]
    out = {"sow_id": sow["id"], "sow_title": sow.get("title") or "Scheme of work", "total": total,
           "week": None, "title": "", "outcomes": [], "next_title": "", "days_to_start": 0, "deck": None}
    start = sow.get("start_date") or course_start
    n = week_number(start, today)
    if n is None:
        out["state"] = "no_date"
        n = weeks[0]["week"]
    elif n < 1:
        out["state"] = "before"
        out["days_to_start"] = (start - today).days
        n = weeks[0]["week"]
    elif n > total:
        out["state"] = "finished"
        return out
    else:
        out["state"] = "teaching"
    # a week the plan skips (half term, an induction row) shows the next planned week instead
    week = by_no.get(n) or next((w for w in weeks if w["week"] > n), None)
    if not week:
        out["state"] = "finished"
        return out
    out.update(week=week["week"], title=week.get("title") or f"Week {week['week']}",
               outcomes=[str(o) for o in (week.get("learning_outcomes") or [])][:6])
    later = next((w for w in weeks if w["week"] > week["week"]), None)
    out["next_title"] = (later.get("title") or "") if later else ""
    out["deck"] = next((d for d in decks if d.get("source_sow_id") == sow["id"] and d.get("source_week") == week["week"]), None)
    return out
