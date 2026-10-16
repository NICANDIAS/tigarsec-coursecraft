"""
Import a class list from ProSolution (or any student records system) instead of typing every
student in again. Pure functions, no database - the router (routers/students.py) does the saving.

Accepts what a lecturer actually has:
  - a CSV or Excel (.xlsx) export, even when the report puts a title and a date above the
    column headings (the heading row is found by itself),
  - or rows copied straight off a ProSolution screen / register and pasted in (tab-separated).

Columns are matched by their headings (Student Ref / Learner ID / Forename / Surname / Name /
Group / Course Code ...), and the lecturer can change any match. ONLY each student's name and
reference are saved; every other column (emails, dates of birth, addresses...) is ignored and
never stored.
"""
import csv
import io
import re

MAX_ROWS = 3000
MAX_CELL = 200

# heading words -> what the column holds (checked in this order; first match wins)
FIELD_WORDS = {
    "ref": ["student ref", "student reference", "student id", "student no", "student number", "learner ref",
            "learner reference", "learner id", "learner no", "learner number", "person code", "refno", "ref no",
            "reference", "student code", "id"],
    "preferred": ["preferred forename", "preferred first name", "preferred name", "known as"],
    "forename": ["forename", "first name", "firstname", "given name", "legal forename"],
    "surname": ["surname", "last name", "lastname", "family name"],
    "fullname": ["student name", "learner name", "full name", "fullname", "name"],
    "group": ["group code", "group", "class", "offering code", "offering", "course code", "register", "tutor group", "course"],
}
FIELD_LABELS = {"ref": "Student reference", "forename": "First name", "preferred": "Preferred first name (used when filled in)",
                "surname": "Surname",
                "fullname": "Full name (one column)", "group": "Class / group / course code"}


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Excel stores 12345 as 12345.0 - a student number must not become "12345.0"
    return " ".join(str(value).split())[:MAX_CELL]


def read_table(filename: str, data: bytes) -> list[list[str]]:
    """All rows of an uploaded .csv / .xlsx / .txt as lists of text. Raises ValueError with a
    plain message for anything it cannot read."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise ValueError("That Excel file could not be opened. Save it again as .xlsx or CSV and try again.")
        sheet = book.worksheets[0]
        rows = []
        for row in sheet.iter_rows(values_only=True):
            rows.append([_cell(v) for v in row])
            if len(rows) > MAX_ROWS + 20:
                break
        book.close()
        return _trim(rows)
    if name.endswith(".xls"):
        raise ValueError("Old-style .xls files can't be read. In Excel choose File > Save As > Excel Workbook (.xlsx) or CSV.")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return read_text(text)


def read_text(text: str) -> list[list[str]]:
    """Rows from CSV text or text pasted from a screen (tabs)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    sample = "\n".join(text.split("\n")[:20])
    if "\t" in sample:
        delimiter = "\t"
    else:
        counts = {d: sample.count(d) for d in (",", ";", "|")}
        delimiter = max(counts, key=counts.get) if max(counts.values()) else ","
    rows = [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return _trim(rows[:MAX_ROWS + 20])


def _trim(rows):
    out = []
    for row in rows:
        while row and not row[-1]:
            row = row[:-1]
        out.append(row)
    while out and not any(out[-1]):
        out.pop()
    return out


def _norm_heading(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def _field_for(heading: str):
    h = _norm_heading(heading)
    if not h:
        return None
    for field, words in FIELD_WORDS.items():
        for w in words:
            if h == w or (len(w) > 3 and (h.startswith(w + " ") or h.endswith(" " + w) or f" {w} " in f" {h} ")):
                return field
    return None


def find_header_row(rows: list[list[str]]) -> int:
    """Reports often start with a title, the college name and a date - the heading row is the
    first one (in the top 15) where at least two cells look like known headings. -1 if none."""
    best, best_score = -1, 1
    for i, row in enumerate(rows[:15]):
        score = len({_field_for(c) for c in row if _field_for(c)})
        if score > best_score:
            best, best_score = i, score
    return best


def guess_columns(headers: list[str]) -> dict:
    """{"ref": column index or None, "forename": ..., "surname": ..., "fullname": ..., "group": ...}"""
    found = {f: None for f in FIELD_WORDS}
    for field in FIELD_WORDS:  # each field takes the best-ranked heading for it
        for rank_word in FIELD_WORDS[field]:
            for i, h in enumerate(headers):
                if found[field] is None and i not in found.values() and _field_for(h) == field and \
                        (_norm_heading(h) == rank_word or rank_word in _norm_heading(h)):
                    found[field] = i
            if found[field] is not None:
                break
    if found["forename"] is not None and found["surname"] is not None:
        found["fullname"] = None  # separate name columns are clearer than a combined one
    return found


def _nice_case(word: str) -> str:
    # ProSolution often exports surnames in CAPITALS ("SMITH") - shown as "Smith".
    # Mixed-case names ("McDonald", "de Souza") are left exactly as typed.
    if not word.isupper() or len(word) < 2:
        return word
    word = re.sub(r"(^|[-'’])([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), word.lower())
    return re.sub(r"(^|-)Mc([^\W\d_])", lambda m: m.group(1) + "Mc" + m.group(2).upper(), word)  # MCDONALD -> McDonald


def tidy_name(text: str) -> str:
    text = " ".join((text or "").split())
    if "," in text:  # "SMITH, John" -> "John SMITH"
        last, _, first = text.partition(",")
        text = f"{first.strip()} {last.strip()}".strip()
    return " ".join(_nice_case(w) for w in text.split())[:255]


def build_rows(rows: list[list[str]], header_index: int, columns: dict) -> list[dict]:
    """[{"name", "ref", "group"}] - only these three things leave this function."""
    out = []
    for row in rows[header_index + 1:]:
        def get(field):
            i = columns.get(field)
            return row[i] if i is not None and 0 <= i < len(row) else ""
        if columns.get("forename") is not None or columns.get("surname") is not None or columns.get("preferred") is not None:
            name = f"{get('preferred') or get('forename')} {get('surname')}"
        else:
            name = get("fullname")
        name = tidy_name(name)
        ref = get("ref").strip()
        if not name or _norm_heading(name) in ("total", "count") or name.lower().startswith(("total ", "page ")):
            continue
        out.append({"name": name, "ref": ref[:255], "group": get("group")})
        if len(out) >= MAX_ROWS:
            break
    return out


def groups_in(rows: list[dict]) -> list[tuple[str, int]]:
    counts = {}
    for r in rows:
        counts[r["group"]] = counts.get(r["group"], 0) + 1
    return sorted(counts.items(), key=lambda kv: (kv[0] == "", kv[0].lower()))


def _key(name: str) -> str:
    # letters only (any alphabet - "Zoë", "Łukasz"), so spacing, hyphens and capitals don't matter
    return "".join(ch for ch in (name or "").lower() if ch.isalpha())


def _ref_key(ref: str) -> str:
    return "".join(ch for ch in (ref or "").lower() if ch.isalnum())


def plan_import(existing: list[dict], rows: list[dict], archive_missing: bool = False) -> dict:
    """What importing `rows` would do to a course's students.
    existing: [{"id", "name", "ref", "active"}].  Matching: same reference first (case and
    spaces ignored), otherwise the same name. Nobody is ever deleted - at most archived (and
    only if asked), so marks and history stay safe.
    Returns {"add": [...], "update": [...], "reactivate": [...], "same": [...], "archive": [...],
             "duplicates": n} - each item has "key" (stable id for the tick boxes) and "name"."""
    by_ref = {_ref_key(s["ref"]): s for s in existing if _ref_key(s.get("ref"))}
    by_name = {}
    for s in existing:
        by_name.setdefault(_key(s["name"]), s)
    plan = {"add": [], "update": [], "reactivate": [], "same": [], "archive": [], "duplicates": 0}
    seen_refs, seen_names, matched = set(), set(), set()
    for r in rows:
        rk, nk = _ref_key(r["ref"]), _key(r["name"])
        if (rk and rk in seen_refs) or (not rk and nk in seen_names):
            plan["duplicates"] += 1
            continue
        if rk:
            seen_refs.add(rk)
        seen_names.add(nk)
        student = (by_ref.get(rk) if rk else None) or by_name.get(nk)
        if student and student["id"] in matched:
            student = None
        if student and rk and student.get("ref") and _ref_key(student["ref"]) != rk:
            student = None  # same name, different student number -> a different person
        if not student:
            plan["add"].append({"key": f"add:{rk or nk}", "name": r["name"], "ref": r["ref"], "group": r["group"]})
            continue
        matched.add(student["id"])
        changes = {}
        if r["name"] != student["name"]:
            changes["name"] = r["name"]
        if r["ref"] and r["ref"] != (student.get("ref") or ""):
            changes["ref"] = r["ref"]
        item = {"key": f"s:{student['id']}", "id": student["id"], "name": r["name"], "old_name": student["name"],
                "ref": r["ref"] or student.get("ref") or "", "changes": changes, "group": r["group"]}
        if not student["active"]:
            plan["reactivate"].append(item)
        elif changes:
            plan["update"].append(item)
        else:
            plan["same"].append(item)
    if archive_missing:
        plan["archive"] = [{"key": f"x:{s['id']}", "id": s["id"], "name": s["name"], "ref": s.get("ref") or ""}
                           for s in existing if s["active"] and s["id"] not in matched]
    return plan
