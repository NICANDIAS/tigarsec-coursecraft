"""
The real calendar of a scheme of work: the week-commencing date of each scheme week, and the
breaks (half term, closures, induction rows) between them - read from the college template the
scheme was built from, using the same row choice as the template fill, so the planning page and
the "This week" card agree with the printed scheme of work.

A scheme without a template (or one whose template is gone) has no calendar here - week numbers
are then counted from its start date, and the lecturer can correct "this week" by hand.
"""
import os

_CACHE = {}  # (path, modified time, labels) -> template rows - templates are read once, not on every page


def _template_rows(path, labels_text):
    from .sow_docx_fill import template_calendar, _parse_append_labels
    try:
        key = (path, os.path.getmtime(path), labels_text or "")
    except OSError:
        return None
    if key not in _CACHE:
        if len(_CACHE) > 50:
            _CACHE.clear()
        _CACHE[key] = template_calendar(path, _parse_append_labels(labels_text))
    return _CACHE[key]


def scheme_calendar(sow) -> dict:
    """-> {"from_template", "week_dates": {week number: date}, "breaks": [{"after_week", "date", "label"}], "shifted"}.
    breaks are the template rows the scheme skips, between its first and last week.

    With the lecturer's correction ("this week is week N", sow.week_anchor_json) the scheme slides along
    the college calendar: week N goes in the teaching slot of that week, the weeks after it in the
    teaching slots after that - so a class running a week behind the printed plan still gets half term
    in the right place, and the dates shown are when each week will really be taught."""
    import datetime
    empty = {"from_template": False, "week_dates": {}, "breaks": [], "shifted": False}
    path = getattr(sow, "format_sample_file_path", None)
    weeks = [w["week"] for w in (getattr(sow, "weeks_json", None) or []) if isinstance(w, dict) and isinstance(w.get("week"), int)]
    if not path or not path.lower().endswith(".docx") or not weeks:
        return empty
    rows = _template_rows(path, getattr(sow, "append_eligible_labels", None))
    if not rows:
        return empty
    slots, after_slot, pending = [], {}, []  # teaching slots (dates) in order, and the breaks after each slot
    for row in rows:
        if row["available"]:
            if slots and pending:
                after_slot[len(slots) - 1] = pending
            slots.append(row["date"])
            pending = []
        elif slots and (row["label"] or row["date"]):
            pending.append({"date": row["date"], "label": row["label"] or "No teaching"})
    if not any(slots):
        return empty
    shift = 0  # the fill writes the k-th scheme week into the k-th teaching slot
    anchor = getattr(sow, "week_anchor_json", None) or {}
    if anchor.get("week") in weeks:
        try:
            monday = datetime.date.fromisoformat(str(anchor["monday"]))
            week_of = lambda d: d - datetime.timedelta(days=d.weekday())  # noqa: E731 - template dates aren't always Mondays
            slot = next((i for i, d in enumerate(slots) if d and week_of(d) >= week_of(monday)), None)
            if slot is not None:
                shift = slot - weeks.index(anchor["week"])
        except (ValueError, TypeError, KeyError):
            shift = 0
    week_dates, breaks = {}, []
    last_known = max(i for i, d in enumerate(slots) if d)
    for k, n in enumerate(weeks):
        i = k + shift
        if 0 <= i < len(slots) and slots[i]:
            week_dates[n] = slots[i]
        elif i > last_known:  # beyond the template's end: carry on a week at a time
            week_dates[n] = slots[last_known] + datetime.timedelta(days=7 * (i - last_known))
        for b in after_slot.get(i, []) if k < len(weeks) - 1 else []:
            breaks.append(dict(b, after_week=n))
    return {"from_template": True, "week_dates": week_dates, "breaks": breaks, "shifted": shift != 0}


def scheme_now(sow, course_start=None, today=None):
    """Which week of this scheme we are in today, and its calendar - the one answer every page uses.
    -> (current_week(...) result, scheme_calendar(sow) result)"""
    import datetime
    from .course_overview import current_week
    cal = scheme_calendar(sow)
    weeks = [w["week"] for w in (sow.weeks_json or []) if isinstance(w, dict) and isinstance(w.get("week"), int)]
    anchor = getattr(sow, "week_anchor_json", None)
    # with a college template the correction is already in the dates (see scheme_calendar)
    now = current_week(weeks, sow.start_date or course_start, today or datetime.date.today(),
                       cal["week_dates"], None if cal["from_template"] else anchor)
    if anchor and cal["from_template"]:
        now["source"] = "corrected"
    return now, cal
