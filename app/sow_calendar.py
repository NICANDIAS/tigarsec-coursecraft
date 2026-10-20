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
    """-> {"from_template": bool, "week_dates": {week number: date}, "breaks": [{"after_week", "date", "label"}]}.
    breaks are the template rows the scheme skips, between its first and last week."""
    empty = {"from_template": False, "week_dates": {}, "breaks": []}
    path = getattr(sow, "format_sample_file_path", None)
    weeks = [w for w in (getattr(sow, "weeks_json", None) or []) if isinstance(w, dict) and isinstance(w.get("week"), int)]
    if not path or not path.lower().endswith(".docx") or not weeks:
        return empty
    rows = _template_rows(path, getattr(sow, "append_eligible_labels", None))
    if not rows:
        return empty
    week_dates, breaks, pending, k = {}, [], [], 0
    for row in rows:
        if row["available"]:
            if k >= len(weeks):
                break
            n = weeks[k]["week"]  # the fill writes the k-th scheme week into the k-th available row
            week_dates[n] = row["date"]
            if k > 0:  # rows skipped since the previous scheme week are breaks between them
                breaks += [dict(b, after_week=weeks[k - 1]["week"]) for b in pending]
            pending, k = [], k + 1
        elif k > 0 and (row["label"] or row["date"]):
            pending.append({"date": row["date"], "label": row["label"] or "No teaching"})
    if not any(week_dates.values()):
        return empty
    return {"from_template": True, "week_dates": week_dates, "breaks": breaks}


def scheme_now(sow, course_start=None, today=None):
    """Which week of this scheme we are in today, and its calendar - the one answer every page uses.
    -> (current_week(...) result, scheme_calendar(sow) result)"""
    import datetime
    from .course_overview import current_week
    cal = scheme_calendar(sow)
    weeks = [w["week"] for w in (sow.weeks_json or []) if isinstance(w, dict) and isinstance(w.get("week"), int)]
    now = current_week(weeks, sow.start_date or course_start, today or datetime.date.today(),
                       cal["week_dates"], getattr(sow, "week_anchor_json", None))
    return now, cal
