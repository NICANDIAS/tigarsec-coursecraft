"""
Human review / edit of a parsed spec (Stage 2). Pure functions, no database - the routes in
routers/specs.py call these, and tools/test_spec_editing.py tests them directly.

Rules (enforced by the routes, explained to the user on the screen):
  * Only a spec that is NOT in use can be edited. Editing a spec that courses, schemes of
    work, marks etc. already point at could rename a unit and silently break those links.
    For a spec in use, "Make an editable copy" creates a new version to edit instead.
  * Every saved change is written to the audit log (who, what, when).
  * A person can tick "I have checked this unit". When every unit is checked, the spec's
    status becomes "good" even if the automatic reader had doubts - a human has now
    confirmed it. Structural problems (an empty learning outcome, a unit with no name)
    still keep it at "needs review".
"""
import datetime
import re

BAND_SEPARATOR = " - "


def _clean(value) -> str:
    return re.sub(r"[ \t]+", " ", (value or "").replace("\r\n", "\n")).strip()


def _keep_if_same(submitted, original):
    """Returns the ORIGINAL text when the only difference is spacing. An unchanged save must
    change nothing at all - not even tidy up double spaces the reader left in."""
    cleaned = _clean(submitted)
    return original if cleaned == _clean(original) else cleaned  # _clean(None) == "" so an empty box keeps None


def _band_description(title: str, bands: dict) -> str:
    """Same format the Media reader uses: 'Informing ideas - U: ... | Pass: ... | ...'."""
    text = " | ".join(f"{band}: {words}" for band, words in bands.items() if words)
    return f"{title}{BAND_SEPARATOR}{text}" if text else title


def criterion_title(criterion: dict) -> str:
    """The short title of a banded criterion (the part of the description before the bands)."""
    return (criterion.get("description") or "").split(BAND_SEPARATOR)[0]


def edit_unit(unit: dict, form: dict, other_unit_names: set) -> tuple[dict, list[str], list[str]]:
    """
    Applies one submitted edit form to ONE unit. Returns (new_unit, changes, errors).
    Nothing is changed if there are errors. form keys (all optional except unit-name):
      unit-name
      lo-{i}-code / lo-{i}-title / lo-{i}-delete
      c-{i}-{j}-code / c-{i}-{j}-desc / c-{i}-{j}-delete
      c-{i}-{j}-title + c-{i}-{j}-band-{Band}   (banded criteria, e.g. Media U/Pass/Merit/Distinction)
      new-c-{i}-code / new-c-{i}-desc            (add a criterion to learning outcome i)
      new-lo-code / new-lo-title                 (add a learning outcome)
    Extra keys on units / LOs / criteria (bands, content_area, glh...) are kept.
    """
    changes, errors = [], []
    new_unit = {k: v for k, v in unit.items() if k != "learning_outcomes"}

    name = _keep_if_same(form.get("unit-name", unit.get("name")), unit.get("name"))
    if not name:
        errors.append("The unit needs a name.")
    elif name != unit.get("name"):
        if name.lower() in {n.lower() for n in other_unit_names}:
            errors.append(f"Another unit is already called '{name}'.")
        else:
            changes.append(f"unit renamed '{unit.get('name')}' -> '{name}'")
    new_unit["name"] = name

    new_los = []
    for i, lo in enumerate(unit.get("learning_outcomes", [])):
        lo_label = lo.get("code") or f"#{i + 1}"
        if form.get(f"lo-{i}-delete"):
            changes.append(f"learning outcome {lo_label} deleted ({len(lo.get('criteria', []))} criteria with it)")
            continue
        new_lo = {k: v for k, v in lo.items() if k != "criteria"}
        for field in ("code", "title"):
            key = f"lo-{i}-{field}"
            if key in form:
                value = _keep_if_same(form[key], lo.get(field))
                if value != lo.get(field):
                    changes.append(f"learning outcome {lo_label}: {field} '{lo.get(field) or ''}' -> '{value}'")
                new_lo[field] = value
        if not new_lo.get("code"):
            errors.append(f"Learning outcome {lo_label} needs a code.")

        criteria = []
        for j, crit in enumerate(lo.get("criteria", [])):
            prefix = f"c-{i}-{j}"
            crit_label = f"{new_lo.get('code')}/{crit.get('code')}"
            if form.get(f"{prefix}-delete"):
                changes.append(f"criterion {crit_label} deleted")
                continue
            new_crit = dict(crit)
            code = _keep_if_same(form.get(f"{prefix}-code", crit.get("code")), crit.get("code"))
            if code != crit.get("code"):
                changes.append(f"criterion {crit_label}: code -> '{code}'")
            new_crit["code"] = code
            if crit.get("bands"):
                bands = {}
                for band, old_text in crit["bands"].items():
                    text = _keep_if_same(form.get(f"{prefix}-band-{band}", old_text), old_text)
                    if text != old_text:
                        changes.append(f"criterion {crit_label}: {band} wording changed")
                    bands[band] = text
                title = _keep_if_same(form.get(f"{prefix}-title", criterion_title(crit)), criterion_title(crit))
                if title != criterion_title(crit):
                    changes.append(f"criterion {crit_label}: title -> '{title}'")
                new_crit["bands"] = bands
                if bands != crit["bands"] or title != criterion_title(crit):
                    new_crit["description"] = _band_description(title, bands)
            elif f"{prefix}-desc" in form:
                desc = _keep_if_same(form[f"{prefix}-desc"], crit.get("description"))
                if desc != crit.get("description"):
                    changes.append(f"criterion {crit_label}: wording changed")
                new_crit["description"] = desc
            if not code:
                errors.append(f"A criterion in {lo_label} has no code.")
            criteria.append(new_crit)

        add_code, add_desc = _clean(form.get(f"new-c-{i}-code")), _clean(form.get(f"new-c-{i}-desc"))
        if add_code or add_desc:
            if not add_code:
                errors.append(f"The new criterion for {lo_label} needs a code.")
            else:
                criteria.append({"code": add_code, "description": add_desc})
                changes.append(f"criterion {new_lo.get('code')}/{add_code} added")

        codes = [c["code"].lower() for c in criteria if c.get("code")]
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        if duplicates:
            errors.append(f"Learning outcome {lo_label} has the same criterion code twice: {', '.join(duplicates)}.")
        new_lo["criteria"] = criteria
        new_los.append(new_lo)

    add_lo_code, add_lo_title = _clean(form.get("new-lo-code")), _clean(form.get("new-lo-title"))
    if add_lo_code or add_lo_title:
        if not add_lo_code:
            errors.append("The new learning outcome needs a code.")
        else:
            new_los.append({"code": add_lo_code, "title": add_lo_title, "criteria": []})
            changes.append(f"learning outcome {add_lo_code} added")

    lo_codes = [lo["code"].lower() for lo in new_los if lo.get("code")]
    lo_dupes = sorted({c for c in lo_codes if lo_codes.count(c) > 1})
    if lo_dupes:
        errors.append(f"Two learning outcomes have the same code: {', '.join(lo_dupes)}.")

    new_unit["learning_outcomes"] = new_los
    if errors:
        return unit, [], errors
    return new_unit, changes, []


def structure_problems(units: list) -> list[str]:
    """Problems a person can fix on the edit screen (recomputed after every save)."""
    problems = []
    if not units:
        problems.append("This spec has no units.")
    for unit in units:
        name = unit.get("name") or "(no name)"
        if name.lower().startswith("unknown"):
            problems.append(f"'{name}' has no real name.")
        if not unit.get("learning_outcomes"):
            problems.append(f"'{name}' has no learning outcomes.")
        for lo in unit.get("learning_outcomes", []):
            if not lo.get("criteria"):
                problems.append(f"'{name}' / {lo.get('code')}: no criteria.")
    return problems


def updated_quality(quality: dict | None, units: list, reviewed_units: list[str], editor: str) -> dict:
    """Recomputes the honesty report after a human edit or review tick."""
    q = dict(quality or {})
    # Problems found while READING the document (skipped text, AI limit...) are kept until
    # a person has checked every unit; structural problems are recomputed from the units.
    if "parse_problems" not in q:
        q["parse_problems"] = [p for p in q.get("problems", [])
                               if not re.search(r"no real name|no real code|have no learning outcomes|have no criteria", p)]
    unit_names = [u.get("name") for u in units]
    reviewed = [n for n in reviewed_units if n in unit_names]
    all_checked = bool(unit_names) and len(reviewed) == len(unit_names)
    structural = structure_problems(units)

    q["reviewed_units"] = reviewed
    q["units"] = len(units)
    q["learning_outcomes"] = sum(len(u.get("learning_outcomes", [])) for u in units)
    q["criteria"] = sum(len(lo.get("criteria", [])) for u in units for lo in u.get("learning_outcomes", []))
    q["last_edited_by"] = editor
    q["last_edited_at"] = datetime.datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
    q["problems"] = structural + ([] if all_checked else q["parse_problems"])
    if not units:
        q["verdict"] = "failed"
    elif structural or (q["parse_problems"] and not all_checked):
        q["verdict"] = "needs_review"
    else:
        q["verdict"] = "good"
    notes = [n for n in q.get("notes", []) if not n.startswith("Checked by a person:")]
    notes.append(f"Checked by a person: {len(reviewed)} of {len(unit_names)} unit(s) (last change by {editor}, {q['last_edited_at']}).")
    q["notes"] = notes
    return q
