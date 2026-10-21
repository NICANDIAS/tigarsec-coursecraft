"""
Course page redesign: the "This week" card - which week of the scheme of work we are in today,
what it teaches, and whether its lesson has been made yet. Pure functions, no database.

Which week is "now" comes from, best first:
  1. the lecturer's own correction ("we're actually in week 5"), counted on in calendar weeks;
  2. the real dates of a scheme built from a college template - its weeks are the template's
     free rows, so induction weeks and half terms are skipped exactly as the college calendar does;
  3. otherwise week N runs from start_date + (N-1)*7 days (the coverage dashboard's counting).
"""
import datetime


def week_number(start_date, today):
    """1 for the first week, 2 for the next... 0 or less = teaching hasn't started. None = no start date."""
    if not start_date:
        return None
    return (today - start_date).days // 7 + 1


def monday(day):
    return day - datetime.timedelta(days=day.weekday())


def current_week(weeks, start, today, week_dates=None, anchor=None):
    """weeks: the scheme's week numbers in order. week_dates: {week number: week-commencing date}.
    anchor: {"week": N, "monday": "YYYY-MM-DD"} - the lecturer said week N was the week of that Monday.
    -> {"state": "no_date" | "before" | "teaching" | "break" | "finished", "week" (this week, or the next
        teaching week during a break / before the start), "days_to_start", "source": "corrected" |
        "calendar" | "start_date" | None}"""
    weeks = sorted(w for w in weeks if isinstance(w, int))
    out = {"state": "no_date", "week": weeks[0] if weeks else None, "days_to_start": 0, "source": None}
    if not weeks:
        return out
    if anchor and anchor.get("week"):
        try:
            since = (monday(today) - datetime.date.fromisoformat(str(anchor["monday"]))).days // 7
        except (ValueError, TypeError):
            since = None
        if since is not None:
            n = int(anchor["week"]) + since
            return _place(out, weeks, n, "corrected")
    dated = sorted((d, w) for w, d in (week_dates or {}).items() if d and w in weeks)
    if dated:
        out["source"] = "calendar"
        if today < dated[0][0]:
            return dict(out, state="before", week=dated[0][1], days_to_start=(dated[0][0] - today).days)
        d, w = [x for x in dated if x[0] <= today][-1]
        if today < d + datetime.timedelta(days=7):
            return dict(out, state="teaching", week=w)
        later = [x for x in dated if x[0] > today]
        if later:  # between teaching weeks: half term, a closure
            return dict(out, state="break", week=later[0][1], days_to_start=(later[0][0] - today).days)
        return dict(out, state="finished", week=None)
    n = week_number(start, today)
    if n is None:
        return out
    if n < 1:
        return dict(out, state="before", days_to_start=(start - today).days, source="start_date")
    return _place(out, weeks, n, "start_date")


def _place(out, weeks, n, source):
    if n > weeks[-1]:
        return dict(out, state="finished", week=None, source=source)
    if n < weeks[0]:
        return dict(out, state="before", source=source)
    # a week number the plan skips (half term, an induction row) shows the next planned week instead
    nxt = n if n in weeks else next(w for w in weeks if w > n)
    return dict(out, state="teaching", week=nxt, source=source)


def _pick_scheme(schemes):
    """The newest ready scheme of work that has weeks. schemes are newest first (Course.schemes_of_work)."""
    for sow in schemes or []:
        if sow.get("status") == "ready" and sow.get("weeks"):
            return sow
    return None


def this_week(schemes, course_start, decks, today=None):
    """schemes: [{"id", "title", "status", "start_date", "weeks": [{"week", "title", "learning_outcomes"}]}] newest first.
    decks: [{"id", "status", "source_sow_id", "source_week"}] newest first.
    A scheme may also carry "week_dates" and "anchor" (see current_week).
    -> None when there is no ready scheme, else
       {"state": "no_date" | "before" | "teaching" | "break" | "finished", "source", "sow_id", "sow_title", "week", "total",
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
    out = {"sow_id": sow["id"], "sow_title": sow.get("title") or "Scheme of work", "total": total, "part": 1, "resume": None,
           "week": None, "title": "", "outcomes": [], "next_title": "", "days_to_start": 0, "deck": None}
    now = sow.get("now") or current_week([w["week"] for w in weeks], sow.get("start_date") or course_start, today,
                                         sow.get("week_dates"), sow.get("anchor"))  # "now": worked out by app/sow_calendar.scheme_now
    out.update(state=now["state"], days_to_start=now["days_to_start"], source="corrected" if sow.get("corrected") else now["source"])
    if now["state"] == "finished":
        return out
    n = now["week"]
    # a week the plan skips (half term, an induction row) shows the next planned week instead
    week = by_no.get(n) or next((w for w in weeks if w["week"] > n), None)
    if not week:
        out["state"] = "finished"
        return out
    part = now.get("part", 1) if week["week"] == n else 1
    out.update(part=part, resume=sow.get("resume", {}).get((week["week"], part)))
    out.update(week=week["week"], title=week.get("title") or f"Week {week['week']}",
               outcomes=[str(o) for o in (week.get("learning_outcomes") or [])][:6])
    later = next((w for w in weeks if w["week"] > week["week"]), None)
    out["next_title"] = (later.get("title") or "") if later else ""
    out["deck"] = next((d for d in decks if d.get("source_sow_id") == sow["id"] and d.get("source_week") == week["week"]), None)
    return out
