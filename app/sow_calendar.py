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


def _monday(d):
    import datetime
    return d - datetime.timedelta(days=d.weekday())


def scheme_calendar(sow, course_start=None, pace_json=None) -> dict:
    """The scheme as it will really be taught, dated:
    {"from_template", "timeline": [{"kind": "week", "week", "part", "parts", "with", "date"} |
                                   {"kind": "gap", "label", "date"}] in teaching order,
     "week_dates": {week: date of its first part}, "breaks": [{"after_week", "date", "label"}],
     "shifted", "overflow": how many teaching weeks run past the end of the calendar / the scheme's end date}.

    With a college template, the k-th teaching entry goes in the k-th free row (as the printed scheme
    does), so half terms and induction weeks fall where the college put them. The lecturer's correction
    ("this week is week N", sow.week_anchor_json) slides the scheme along those rows. Carried-on and
    combined weeks (sow.pace_json, see app/pace.py) take one row each / share one row.
    Without a template, entries are a calendar week apart from the start date (or the correction), and
    a missing week NUMBER is a week with no teaching."""
    import datetime
    from .pace import entries
    pace = pace_json if pace_json is not None else getattr(sow, "pace_json", None)
    weeks = [w["week"] for w in (getattr(sow, "weeks_json", None) or []) if isinstance(w, dict) and isinstance(w.get("week"), int)]
    out = {"from_template": False, "timeline": [], "week_dates": {}, "breaks": [], "shifted": False, "overflow": 0}
    if not weeks:
        return out
    anchor = getattr(sow, "week_anchor_json", None) or {}
    try:
        anchor_monday = _monday(datetime.date.fromisoformat(str(anchor["monday"]))) if anchor.get("week") in weeks else None
    except (ValueError, TypeError, KeyError):
        anchor_monday = None
    path = getattr(sow, "format_sample_file_path", None)
    rows = _template_rows(path, getattr(sow, "append_eligible_labels", None)) \
        if path and path.lower().endswith(".docx") else None
    if rows and any(r["available"] and r["date"] for r in rows):
        ents = entries(weeks, pace)
        slots, after_slot, pending = [], {}, []  # free rows (dates) in order, and the rows skipped after each
        for row in rows:
            if row["available"]:
                if slots and pending:
                    after_slot[len(slots) - 1] = pending
                slots.append(row["date"])
                pending = []
            elif slots and (row["label"] or row["date"]):
                pending.append({"date": row["date"], "label": row["label"] or "No teaching"})
        shift = 0
        if anchor_monday:
            first = next(i for i, e in enumerate(ents) if e["week"] == anchor["week"])
            slot = next((i for i, d in enumerate(slots) if d and _monday(d) >= anchor_monday), None)
            if slot is not None:
                shift = slot - first
        last_known = max(i for i, d in enumerate(slots) if d)
        timeline = []
        for k, e in enumerate(ents):
            i = k + shift
            if 0 <= i < len(slots) and slots[i]:
                date = slots[i]
            elif i > last_known:  # past the template's last row: carry on a week at a time
                date = slots[last_known] + datetime.timedelta(days=7 * (i - last_known))
            else:
                date = None
            timeline.append(dict(e, kind="week", date=date))
            if k < len(ents) - 1:
                timeline += [dict(b, kind="gap") for b in after_slot.get(i, [])]
        out.update(from_template=True, shifted=shift != 0,
                   overflow=max(0, (len(ents) - 1 + shift) - (len(slots) - 1)))
    else:
        ents = entries(weeks, pace, keep_number_gaps=True)
        if anchor_monday:
            first = next(i for i, e in enumerate(ents) if not e["gap"] and e["week"] == anchor["week"])
            base = anchor_monday - datetime.timedelta(days=7 * first)
        else:
            start = getattr(sow, "start_date", None) or course_start
            base = start
        timeline = []
        for k, e in enumerate(ents):
            date = base + datetime.timedelta(days=7 * k) if base else None
            if e["gap"]:
                timeline.append({"kind": "gap", "label": f"No week {e['week']} in the scheme", "date": date})
            else:
                timeline.append(dict(e, kind="week", date=date))
        end = getattr(sow, "end_date", None)
        if end and base:
            out["overflow"] = sum(1 for t in timeline if t["kind"] == "week" and t["date"] and t["date"] > end)
    out["timeline"] = timeline
    last_week = None
    for t in timeline:
        if t["kind"] == "week":
            if t["part"] == 1 and t["date"]:
                out["week_dates"].setdefault(t["week"], t["date"])
            last_week = t["week"]
        elif last_week is not None and out["from_template"]:
            out["breaks"].append({"after_week": last_week, "date": t["date"], "label": t["label"]})
    return out


def now_from_timeline(timeline, today):
    """Where today falls: {"state": "no_date" | "before" | "teaching" | "break" | "finished", "week", "part",
    "days_to_start"}. In a break (half term, a gap) "week" is the next teaching week."""
    import datetime
    weeks = [t for t in timeline if t["kind"] == "week"]
    out = {"state": "no_date", "week": weeks[0]["week"] if weeks else None, "part": 1, "days_to_start": 0}
    dated = [t for t in timeline if t.get("date")]
    if not weeks or not dated:
        return out
    if today < dated[0]["date"]:
        nxt = next((t for t in dated if t["kind"] == "week"), weeks[0])
        return dict(out, state="before", week=nxt["week"], part=nxt.get("part", 1), days_to_start=(dated[0]["date"] - today).days)
    current = [t for t in dated if t["date"] <= today][-1]
    if current["kind"] == "week" and today < current["date"] + datetime.timedelta(days=7):
        return dict(out, state="teaching", week=current["week"], part=current["part"])
    later = [t for t in dated if t["date"] > today and t["kind"] == "week"]
    if later:
        return dict(out, state="break", week=later[0]["week"], part=later[0]["part"], days_to_start=(later[0]["date"] - today).days)
    return dict(out, state="finished", week=None)


def scheme_now(sow, course_start=None, today=None, pace_json=None):
    """Which week of this scheme we are in today, and its calendar - the one answer every page uses.
    -> ({"state", "week", "part", "days_to_start", "source"}, scheme_calendar(...))"""
    import datetime
    cal = scheme_calendar(sow, course_start, pace_json)
    now = now_from_timeline(cal["timeline"], today or datetime.date.today())
    if getattr(sow, "week_anchor_json", None):
        now["source"] = "corrected"
    elif cal["from_template"]:
        now["source"] = "calendar"
    elif getattr(sow, "start_date", None) or course_start:
        now["source"] = "start_date"
    else:
        now["source"] = None
    return now, cal
