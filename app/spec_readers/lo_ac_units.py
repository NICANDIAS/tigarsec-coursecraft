"""
Reader for "unit / learning outcome / assessment criteria" specs - the Ofqual-regulated unit
layout used by City & Guilds, and (from the same RQF template, not yet tested with real
files) many NCFE, Highfield and similar qualifications.

Tested on: City & Guilds Level 4 Certificate in Education and Training (6502), handbook v1.1.

Layout this reader expects (all confirmed in the real C&G handbook):
  Unit 401 Planning to meet the needs of        <- title may wrap over several lines
  learners in education and training
  UAN: A/505/1189     Level: 4     Credit value: 3     GLH: 15
  Aim: ...
  Learning outcome
  The learner will:
  1. be able to use initial and diagnostic assessment ...
  Assessment criteria
  The learner can:
  1.1 analyse the role and use of initial and diagnostic assessment ...
  1.2 ...
  Supporting information / Guidance ...                <- kept as unit guidance text
And a structure table: "Mandatory - Group A" / "Optional - Group B" rows with the unit
accreditation number, unit number, title, credits, GLH, level - plus the rules of
combination ("a minimum of 36 credits ...").

Traps handled: the unit title is repeated as a page header on every page of the unit
(it would otherwise be glued onto the last criterion of the previous page); the summary
list near the front ("Unit 401 ... (4 learning outcomes) • ...") is NOT a unit page
because no UAN/Level/Credit lines follow it; a handbook may LIST optional units whose
content is in a separate "unit pack" - those are recorded, not invented.
"""
import re
from collections import Counter

UNIT_HEADER_RE = re.compile(r"^Unit\s+(\d{2,4})\s+(\S.*)$")
META_RE = re.compile(r"^(UAN|Unit accreditation number|Level|Credit value|GLH|Guided learning hours|Assessment|Aim|"
                     r"Relationship to NOS|Endorsement by a)\b\s*:?\s*(.*)$", re.IGNORECASE)
LO_WILL_RE = re.compile(r"^The learner will:?\s*(.*)$", re.IGNORECASE)
AC_CAN_RE = re.compile(r"^The learner can:?\s*(.*)$", re.IGNORECASE)
LO_START_RE = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")
AC_START_RE = re.compile(r"^(\d{1,2}\.\d{1,2})\s+(\S.*)$")
UAN_RE = re.compile(r"\b([A-Z]/\d{3}/\d{4})\b")
STOP_HEADINGS = {"supporting information", "guidance", "range", "assessment guidance", "delivery guidance"}


def looks_like(text: str) -> bool:
    return (
        len(re.findall(r"^The learner will", text, re.MULTILINE | re.IGNORECASE)) >= 2
        and len(re.findall(r"^The learner can", text, re.MULTILINE | re.IGNORECASE)) >= 2
        and bool(re.search(r"^(UAN|Credit value|Level)\s*:", text, re.MULTILINE | re.IGNORECASE))
    )


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _drop_running_footers(lines):
    """Removes page footers like "City & Guilds Level 4 Certificate in ... (6502) 27".
    A footer = a line carrying a PAGE NUMBER at its start or end, whose text (number removed)
    repeats on 4+ pages. Lines without a page number are never dropped: text such as
    "Aim: The purpose of the unit is to enable the" or "education and training" repeats in
    every unit too, but it is real content."""
    def key(line):
        return re.sub(r"^\d+\s+|\s+\d+$", "", line.strip())

    def has_page_number(line):
        return bool(re.match(r"^\d+\s+\S|.*\S\s+\d+$", line.strip()))
    counts = Counter(key(l) for l in lines if has_page_number(l) and len(key(l)) > 20)
    return [l for l in lines if not (has_page_number(l) and len(key(l)) > 20 and counts[key(l)] >= 4)]


def _find_unit_pages(lines):
    """Start lines of REAL unit pages: a 'Unit NNN title' line followed (within 8 lines,
    after the title wraps) by UAN / Level / Credit value lines."""
    starts = []
    for i, line in enumerate(lines):
        m = UNIT_HEADER_RE.match(line.strip())
        if not m:
            continue
        window = [l.strip() for l in lines[i + 1:i + 9]]
        if sum(1 for l in window if re.match(r"^(UAN|Level|Credit value|GLH)\s*:", l, re.IGNORECASE)) >= 2:
            title_parts, j = [m.group(2).strip()], i + 1
            while j < len(lines) and not META_RE.match(lines[j].strip()) and len(title_parts) < 6:
                title_parts.append(lines[j].strip())
                j += 1
            starts.append({"line": i, "number": m.group(1), "title": _norm(" ".join(title_parts)), "body_from": j})
    # The same unit can't start twice; keep the first real page of each number.
    seen, unique = set(), []
    for s in starts:
        if s["number"] not in seen:
            seen.add(s["number"])
            unique.append(s)
    return unique


def _skip_repeated_header(lines, k, number, title):
    """If lines[k] is the unit's own page header repeated, return the index after it (and
    after the wrapped rest of the title); otherwise return k unchanged."""
    m = UNIT_HEADER_RE.match(lines[k].strip())
    if not m or m.group(1) != number:
        return k
    target = _norm(title).lower()
    collected = _norm(m.group(2)).lower()
    k += 1
    while k < len(lines) and collected != target and target.startswith(_norm(collected + " " + lines[k]).lower()):
        collected = _norm(collected + " " + lines[k]).lower()
        k += 1
    return k


def _read_unit_body(lines, start, end, number, title):
    meta, los, guidance = {}, [], []
    mode, current_lo, current_ac, aim = None, None, None, []
    k = start
    while k < end:
        skipped = _skip_repeated_header(lines, k, number, title)
        if skipped != k:
            k = skipped
            continue
        line = lines[k].strip()
        k += 1
        if not line:
            continue
        low = line.lower().rstrip(":")
        if low in STOP_HEADINGS:
            mode = "guidance"
            continue
        if mode == "guidance":
            if low == "learning outcome":  # some handbooks interleave guidance and outcomes
                mode = None
            else:
                guidance.append(line)
                continue
        meta_match = META_RE.match(line)
        if meta_match and mode in (None, "aim"):
            key = meta_match.group(1).lower()
            if key == "aim":
                mode, aim = "aim", [meta_match.group(2)]
            elif key not in ("relationship to nos", "endorsement by a"):
                meta[key] = meta_match.group(2).strip()
                mode = None
            continue
        if low == "learning outcome" or low == "learning outcomes":
            mode = "lo"
            continue
        if low in ("assessment criteria",):
            mode = "ac"
            continue
        will = LO_WILL_RE.match(line)
        if will:
            mode, line = "lo", will.group(1).strip()
            if not line:
                continue
        can = AC_CAN_RE.match(line)
        if can:
            mode, line = "ac", can.group(1).strip()
            if not line:
                continue
        if mode == "aim":
            aim.append(line)
            continue
        if mode == "lo":
            m = LO_START_RE.match(line)
            if m:
                current_lo = {"code": f"LO{m.group(1)}", "title": m.group(2), "criteria": []}
                los.append(current_lo)
                current_ac = None
            elif current_lo:
                current_lo["title"] += " " + line
            continue
        if mode == "ac" and current_lo is not None:
            # A line with several criteria run together ("1.1 x 1.2 y") - split it first.
            pieces = re.split(r"(?<=\S)\s+(?=\d{1,2}\.\d{1,2}\s+[a-z])", line)
            for piece in pieces:
                m = AC_START_RE.match(piece)
                if m:
                    current_ac = {"code": m.group(1), "description": m.group(2)}
                    current_lo["criteria"].append(current_ac)
                elif current_ac:
                    current_ac["description"] += " " + piece
    for lo in los:
        lo["title"] = _norm(lo["title"]).rstrip(".")
        for c in lo["criteria"]:
            c["description"] = _norm(c["description"])
    return meta, _norm(" ".join(aim)), los, _norm(" ".join(guidance))


def _read_structure(lines):
    """Mandatory/optional groups and rules of combination from the structure table."""
    groups, rules, current, catalogue = {}, [], None, {}
    for i, line in enumerate(lines):
        s = line.strip()
        if re.match(r"^(Mandatory|Optional)\b.*Group\s+[A-Z]", s, re.IGNORECASE) or re.match(r"^(Mandatory|Optional) units?$", s, re.IGNORECASE):
            current = "mandatory" if s.lower().startswith("mandatory") else "optional"
            continue
        uan = UAN_RE.search(s)
        if current and uan:
            m = re.search(r"\b(\d{3})\b\s+(.*?)\s+(\d{1,2})\s+(\d{1,3})\s+(\d)\s*$", s)
            number = re.search(r"[A-Z]/\d{3}/\d{4}\s+(\d{3})\b", s)
            if number:
                groups[number.group(1)] = current
                if m:
                    catalogue[number.group(1)] = {"uan": uan.group(1), "credits": int(m.group(3)),
                                                  "glh": int(m.group(4)), "level": int(m.group(5)), "group": current}
                else:
                    catalogue[number.group(1)] = {"uan": uan.group(1), "group": current}
        elif current and s and not uan and re.match(r"^\d+\s+City & Guilds", s):
            continue
        if not rules and re.search(r"(minimum|must achieve|must be achieved).*credits", s, re.IGNORECASE):
            # The rules of combination: this line and the following lines up to the table.
            block = [s]
            for nxt in lines[i + 1:i + 8]:
                nxt = nxt.strip()
                if not nxt or UAN_RE.search(nxt) or re.match(r"^(Unit|Mandatory|Optional)\b", nxt):
                    break
                block.append(nxt)
            rules.append(_norm(" ".join(block)))
    return groups, rules, catalogue


def read(text: str, pdf_path: str | None = None) -> dict | None:
    if not looks_like(text):
        return None
    lines = _drop_running_footers(text.split("\n"))
    pages = _find_unit_pages(lines)
    if not pages:
        return None
    groups, rules, catalogue = _read_structure(lines)

    units, problems, notes = [], [], []
    for idx, page in enumerate(pages):
        end = pages[idx + 1]["line"] if idx + 1 < len(pages) else len(lines)
        meta, aim, los, guidance = _read_unit_body(lines, page["body_from"], end, page["number"], page["title"])
        unit = {"name": f"Unit {page['number']}: {page['title']}", "code": page["number"], "learning_outcomes": los}
        for key, out_key in (("uan", "uan"), ("unit accreditation number", "uan"), ("level", "level"),
                             ("credit value", "credits"), ("glh", "glh"), ("guided learning hours", "glh"),
                             ("assessment", "assessment")):
            if key in meta and meta[key]:
                value = meta[key]
                unit[out_key] = int(value) if value.isdigit() else value
        if page["number"] in groups:
            unit["group"] = groups[page["number"]]
        if aim:
            unit["aim"] = aim
        if guidance:
            unit["guidance"] = guidance[:6000]
        if not los:
            problems.append(f"Unit {page['number']}: no learning outcomes found on its page.")
        units.append(unit)

    found = {u["code"] for u in units}
    listed_only = sorted(n for n in catalogue if n not in found)
    if listed_only:
        optional = [n for n in listed_only if catalogue[n].get("group") == "optional"]
        notes.append(
            f"{len(listed_only)} unit(s) are listed in the structure table but their details are not in this document "
            f"({', '.join(listed_only[:12])}{'...' if len(listed_only) > 12 else ''})"
            + (" - these are optional units, usually in a separate 'optional unit pack'. Upload that as its own spec if the college teaches them."
               if len(optional) == len(listed_only) else ".")
        )
    lo_count = sum(len(u["learning_outcomes"]) for u in units)
    ac_count = sum(len(lo["criteria"]) for u in units for lo in u["learning_outcomes"])
    notes.insert(0, f"Read as 'learning outcome / assessment criteria' units: {len(units)} unit(s), "
                    f"{lo_count} learning outcome(s), {ac_count} assessment criteria.")
    extras = {
        "spec_style": "lo_ac_units",
        "qualification": {
            "rules": rules,
            "units_listed": [{"number": n, **catalogue[n]} for n in sorted(catalogue)],
            "pass_rule": "Each unit is achieved when the learner meets every assessment criterion (no Merit/Distinction grades in this spec)."
            if not re.search(r"\bDistinction\b", text) else None,
        },
        "grading": {"type": "met_not_met"} if not re.search(r"\bDistinction\b", text) else {"type": "unknown"},
    }
    return {
        "units": units,
        "unmatched": [],
        "extras": extras,
        "ruleset": "Learning outcome / assessment criteria units (City & Guilds style)",
        "problems": problems,
        "notes": notes,
    }


def read_unit_text(text: str, name: str) -> dict | None:
    """One unit's outcomes and criteria when there is NO "Unit NNN" page header - e.g. a
    lecturer pasted just the learning outcomes / assessment criteria part of a unit."""
    if not (re.search(r"^The learner will", text, re.MULTILINE | re.IGNORECASE)
            and re.search(r"^The learner can", text, re.MULTILINE | re.IGNORECASE)):
        return None
    lines = _drop_running_footers(text.split("\n"))
    meta, aim, los, guidance = _read_unit_body(lines, 0, len(lines), "", name)
    if not los:
        return None
    unit = {"name": name, "learning_outcomes": los}
    if aim:
        unit["aim"] = aim
    return unit
