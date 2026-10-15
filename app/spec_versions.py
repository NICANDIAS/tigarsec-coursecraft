"""
Spec versions - "is this spec in use?" and "what changed between two parses?".

Why this exists (Stage 1, Oct 2026): "Re-parse" used to overwrite a spec in place. Nine
other tables point at a spec, and several of them (schemes of work, timetabling, teacher
qualifications, exemplars) remember UNIT NAMES as text. If a re-parse renamed a unit,
those records silently lost their link. Now a spec that is in use is never overwritten:
re-parsing it creates a NEW version, and the old one stays exactly as it was.

compare_units() is pure (no database), so it can be tested without running the app.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import Base
from .models import Course, SpecVersion

# Friendly names for the tables we know about. Any table NOT listed here is still found and
# counted automatically (see linked_columns) - it just shows its raw table name instead.
TABLE_LABELS = {
    "courses": "course(s)",
    "schemes_of_work": "scheme(s) of work",
    "submissions": "marked submission(s)",
    "iv_requests": "IV request(s)",
    "exam_exemplars": "exam exemplar(s)",
    "teacher_unit_qualifications": "teacher unit qualification(s)",
    "teacher_spec_experience": "teacher experience record(s)",
    "teacher_cpd_records": "CPD record(s)",
    "teacher_iv_outcomes": "IV outcome record(s)",
}


def linked_columns():
    """
    Every (table, column) in the whole app that points at spec_versions.id - found
    AUTOMATICALLY from the models, not from a hand-written list. A hand-written list was
    the first version of this, and it was out of date within weeks (TeacherSpecExperience
    was added and would have been missed - a spec used only there would have counted as
    "not in use" and been re-parsed in place). Now a new table can't be forgotten.
    spec_versions.reparsed_from_id is skipped: a newer re-parse is not "use".
    """
    found = []
    for table in Base.metadata.sorted_tables:
        if table.name == SpecVersion.__tablename__:
            continue
        for column in table.columns:
            if any(fk.column.table.name == SpecVersion.__tablename__ for fk in column.foreign_keys):
                found.append((table, column))
    return found


def spec_usage(db: Session, spec_id: int) -> dict:
    """Returns {"counts": {label: n, ...only non-zero}, "course_names": [...], "in_use": bool}."""
    counts = {}
    for table, column in linked_columns():
        n = db.execute(select(func.count()).select_from(table).where(column == spec_id)).scalar() or 0
        if n:
            label = TABLE_LABELS.get(table.name, table.name.replace("_", " ") + " record(s)")
            counts[label] = counts.get(label, 0) + n
    course_names = [c.name for c in db.query(Course).filter(Course.spec_version_id == spec_id).all()]
    return {"counts": counts, "course_names": course_names, "in_use": bool(counts)}


def usage_sentence(usage: dict) -> str:
    """e.g. 'used by 2 course(s), 5 scheme(s) of work'."""
    return ", ".join(f"{n} {label}" for label, n in usage["counts"].items())


def version_family(db: Session, spec: SpecVersion) -> dict:
    """The version this was re-parsed from (if any) and versions re-parsed from this one."""
    parent = db.get(SpecVersion, spec.reparsed_from_id) if spec.reparsed_from_id else None
    children = (
        db.query(SpecVersion).filter(SpecVersion.reparsed_from_id == spec.id).order_by(SpecVersion.id).all()
    )
    return {"parent": parent, "children": children}


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def compare_units(old_units: list, new_units: list) -> dict:
    """
    Plain comparison of two parsed specs, matched by unit NAME, then LO code, then
    criterion code (the same keys the rest of the app links on). Returns:
      units_added / units_removed: [unit name]
      units: [{"name", "los_added", "los_removed", "criteria_added", "criteria_removed",
               "criteria_changed": [(lo_code, crit_code)]}] - only units with a difference
      same: True if nothing differs at all
    "Removed" matters most: anything that pointed at a removed unit name would lose its
    link if courses were moved to the new version.
    """
    old_by_name = {u.get("name"): u for u in old_units}
    new_by_name = {u.get("name"): u for u in new_units}
    report = {
        "units_added": [n for n in new_by_name if n not in old_by_name],
        "units_removed": [n for n in old_by_name if n not in new_by_name],
        "units": [],
    }
    for name in [n for n in old_by_name if n in new_by_name]:
        old_los = {lo.get("code"): lo for lo in old_by_name[name].get("learning_outcomes", [])}
        new_los = {lo.get("code"): lo for lo in new_by_name[name].get("learning_outcomes", [])}
        entry = {
            "name": name,
            "los_added": [c for c in new_los if c not in old_los],
            "los_removed": [c for c in old_los if c not in new_los],
            "criteria_added": [],
            "criteria_removed": [],
            "criteria_changed": [],
        }
        for lo_code in [c for c in old_los if c in new_los]:
            old_c = {c.get("code"): c.get("description") for c in old_los[lo_code].get("criteria", [])}
            new_c = {c.get("code"): c.get("description") for c in new_los[lo_code].get("criteria", [])}
            entry["criteria_added"] += [f"{lo_code} / {c}" for c in new_c if c not in old_c]
            entry["criteria_removed"] += [f"{lo_code} / {c}" for c in old_c if c not in new_c]
            entry["criteria_changed"] += [
                f"{lo_code} / {c}" for c in old_c if c in new_c and _norm(old_c[c]) != _norm(new_c[c])
            ]
        if any(entry[k] for k in ("los_added", "los_removed", "criteria_added", "criteria_removed", "criteria_changed")):
            report["units"].append(entry)
    report["same"] = not (report["units_added"] or report["units_removed"] or report["units"])
    return report
