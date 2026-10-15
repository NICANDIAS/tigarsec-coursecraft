"""
Reader for Pearson BTEC Nationals "Practice" style specs - first built for and tested on
"BTEC Level 3 National 540 Diploma / Extended Diploma in Creative Media Practice" (Issue 3,
May 2021). Pearson uses the same layout for other "Practice" titles, but only Creative
Media Practice has been tested with a real file - any other title should be checked on the
review screen before use.

Layout this reader expects (all confirmed in the real Media spec):
  * Modules (A, B) made of ASSESSMENT UNITS with hours:  "A1: Skills Development (405 GLH)"
  * Five objectives per unit:                             "A1.1 Explore and develop ..."
  * ONE shared grade grid AC1-AC5 x U/Pass/Merit/Distinction. The spec states (p.44):
    "Assessment criterion 1: Informing ideas is used to assess objectives A1.1, A2.1, B1.1
    and B2.1" - so objective X.n is assessed by ACn. That is how criteria are attached.
  * Per module, five CONTENT AREAS in capitals (INFORMING IDEAS ... COMMUNICATION) with
    "e.g." bullet lists (• topic, ○ sub-topic, – item) that the spec calls indicative.
  * A points table (A1/A2/B1 = 0/1/2/3, B2 = 0/3/6/9) and the rule "minimum of Pass in all
    criteria for each assessment unit".

What it produces (shape every tool already reads, plus extra keys older code ignores):
  unit  "Assessment unit A1: Skills Development"   (+ code, module, glh)
  LO    "A1.1" Explore and develop investigation skills ...   (+ content_area, assessed_by)
  crit  "A1.AC1" "Informing ideas - U: ... | Pass: ... | Merit: ... | Distinction: ..."
        (+ bands {U, Pass, Merit, Distinction}) - the code carries the unit prefix because
        AC1 appears in every unit and codes must be unique for coverage/marking matching.

The grade grid is a 4-column table. Plain text extraction mixes the columns line by line,
so the grid is read from the PDF's page LAYOUT (word positions). Without the PDF (e.g. a
spec uploaded before CourseCraft kept the file) the criteria still get their titles but no
band text, and the quality report says so.
"""
import re
from collections import Counter

BANDS = ["U", "Pass", "Merit", "Distinction"]

UNIT_RE = re.compile(r"^([A-Z])(\d):\s+(.+?)\s+\((\d+)\s*GLH\)\s*$", re.MULTILINE)
OBJECTIVE_RE = re.compile(r"^([A-Z]\d)\.(\d)\s+(.+)$", re.MULTILINE)
AC_LINE_RE = re.compile(r"^AC(\d):", re.MULTILINE)
CONTENT_END_RE = re.compile(r"^ESSENTIAL INFORMATION FOR ASSESSMENT\s*$", re.MULTILINE)
CAPS_HEADING_RE = re.compile(r"^([A-Z][A-Z ]{5,40})\s*$")
POINTS_START_RE = re.compile(r"^Points per assessment criterion\s*$", re.MULTILINE)
PASS_RULE_RE = re.compile(r"[^.\n]*minimum of Pass in all criteria[^.\n]*\.?")

# Text that is NOT an objective even though it starts like one (sentence fragments).
_OBJECTIVE_NOISE = re.compile(r"\s+(\d+\.\s+[A-Z].*|assessment criteria\.?|of learning for the assessment)$")


def looks_like(text: str) -> bool:
    """Cheap fingerprint: units with GLH in brackets, X.n objectives, and an ACn grid."""
    return (
        len({m.group(1) + m.group(2) for m in UNIT_RE.finditer(text)}) >= 2
        and len(OBJECTIVE_RE.findall(text)) >= 5
        and len(AC_LINE_RE.findall(text)) >= 3
    )


# ----------------------------------------------------------------------------- helpers
def _clean_title(title: str) -> str:
    """Repairs PDF word-splits like 'D emonstrate' / 'A ssessment' and strips column noise."""
    title = _OBJECTIVE_NOISE.sub("", title.strip())
    title = re.sub(r"^([A-Z]) ([a-z]{2,})", r"\1\2", title)
    return re.sub(r"\s+", " ", title).strip()


def _drop_running_footers(lines: list[str]) -> list[str]:
    """Removes page headers/footers: lines that repeat 4+ times once page numbers are ignored."""
    def key(line):
        return re.sub(r"^\d+\s+|\s+\d+$", "", line.strip())
    counts = Counter(key(l) for l in lines if len(l.strip()) > 20)
    return [l for l in lines if not (len(l.strip()) > 20 and counts[key(l)] >= 4)]


def _area_key(name: str) -> str:
    """'TECHNICAL SKILLS' / 'Technical skill' -> 'technical'; matches content areas to ACs."""
    return (name.strip().split() or [""])[0].lower()


# ----------------------------------------------------------------------------- units + objectives
def _read_units(text: str):
    units, order = {}, []
    for m in UNIT_RE.finditer(text):
        code = m.group(1) + m.group(2)
        if code not in units:
            units[code] = {"code": code, "module": m.group(1), "title": m.group(3).strip(), "glh": int(m.group(4))}
            order.append(code)
    objectives = {}
    for m in OBJECTIVE_RE.finditer(text):
        unit_code, n, title = m.group(1), int(m.group(2)), _clean_title(m.group(3))
        # A real objective starts with a capital and doesn't mention other objective codes
        # (rejects sentence fragments like "B1.1 and B2.1." from the guidance pages).
        if unit_code not in units or len(title) < 12 or not title[0].isupper() or re.search(r"[A-Z]\d\.\d", title):
            continue
        # The spec prints the objectives twice (a guide page with a second column of prose
        # beside it, and the module page). Keep the SHORTEST version - the clean one.
        key = (unit_code, n)
        if key not in objectives or len(title) < len(objectives[key]):
            objectives[key] = title
    return units, order, objectives


# ----------------------------------------------------------------------------- grade grid (PDF layout)
def _header_row(words):
    for p in (w for w in words if w["text"] == "Pass"):
        same = {w["text"]: w for w in words if abs(w["top"] - p["top"]) < 3 and w["text"] in BANDS}
        if all(b in same for b in BANDS):
            return same
    return None


def _column_starts(body_words, min_hits=4):
    """x positions where many lines start a new cell (a gap > 9pt after the previous word)."""
    lines = {}
    for w in body_words:
        lines.setdefault(round(w["top"]), []).append(w)
    hits = Counter()
    for ws in lines.values():
        ws.sort(key=lambda w: w["x0"])
        prev = None
        for w in ws:
            if prev is None or w["x0"] - prev["x1"] > 9:
                hits[round(w["x0"] / 4) * 4] += 1
            prev = w
    starts = []
    for x in sorted(x for x, c in hits.items() if c >= min_hits):
        if not starts or x - starts[-1] >= 12:
            starts.append(x)
    return starts


def _words_text(words, x_from, x_to, y_from, y_to) -> str:
    """Text of the words whose left edge is in [x_from, x_to) and top in [y_from, y_to),
    in reading order. Works even when a table is printed partly outside the page's visible
    area (the spec's appendix copy is), where cropping the page is not allowed."""
    picked = [w for w in words if x_from <= w["x0"] < x_to and y_from <= w["top"] < y_to]
    picked.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
    return re.sub(r"\s+", " ", " ".join(w["text"] for w in picked)).strip()


def _cell_text(page, words, x_from, x_to, y_from, y_to) -> str:
    """Cell text. Inside the page's visible area, use pdfplumber's own layout-aware text
    (handles widely-spaced letters on the main grid page); outside it - where cropping is
    not allowed - fall back to joining the words found in that area."""
    x0, top, x1, bottom = page.bbox
    if x_from >= x0 and x_to <= x1 and y_from >= top and y_to <= bottom and x_to > x_from and y_to > y_from:
        return re.sub(r"\s+", " ", page.crop((x_from, y_from, x_to, y_to)).extract_text() or "").strip()
    return _words_text(words, x_from, x_to, y_from, y_to)


def _grid_from_page(page):
    words = page.extract_words()
    header = _header_row(words)
    if not header:
        return None
    ac_words = [w for w in words if re.fullmatch(r"AC\d:?", w["text"]) and w["top"] > header["Pass"]["bottom"]]
    if len(ac_words) < 3:
        return None
    body = [w for w in words if header["Pass"]["bottom"] + 12 < w["top"] < ac_words[-1]["top"] + 150]
    starts = _column_starts(body)
    if len(starts) < 5:  # label column + 4 band columns
        return None
    far_right = max(w["x1"] for w in words) + 1
    edges = starts[-4:] + [far_right]
    label_left = min(w["x0"] for w in ac_words) - 1
    grid = {}
    for i, ac in enumerate(ac_words):
        y0 = ac["top"] - 2
        if i + 1 < len(ac_words):
            y1 = ac_words[i + 1]["top"] - 2
        else:
            # Last row: follow the text lines down until a clear vertical gap (end of table),
            # so the row doesn't swallow whatever is printed under the grid.
            tops = sorted({round(w["top"]) for w in words if w["top"] >= ac["top"] - 1})
            last = tops[0]
            for t in tops[1:]:
                if t - last > 16:
                    break
                last = t
            y1 = last + 12
        y1 = min(y1, max(w["bottom"] for w in words) + 1)
        label = _cell_text(page, words, label_left, edges[0] - 2, y0, y1)
        label = re.sub(r"([a-z])- ([a-z])", r"\1\2", label)  # "Communi- cation" -> "Communication"
        m = re.match(r"AC(\d):?\s*(.*)", label)
        if not m:
            continue
        bands = {}
        for band, (xa, xb) in zip(BANDS, zip(edges, edges[1:])):
            bands[band] = _cell_text(page, words, xa - 2, min(xb - 2, far_right), y0, y1)
        grid[f"AC{m.group(1)}"] = {"title": m.group(2).strip().rstrip(".") or f"AC{m.group(1)}", "bands": bands}
    return grid or None


def read_band_grids(pdf_path: str) -> list[dict]:
    """Every readable AC x band grid in the PDF, in page order (pages printed sideways are skipped)."""
    try:
        import pdfplumber
    except Exception:
        return []
    grids = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if "AC1" not in text or "Distinction" not in text:
                    continue
                grid = _grid_from_page(page)
                if grid:
                    grids.append(grid)
    except Exception:
        return grids
    return grids


# ----------------------------------------------------------------------------- content areas
def _parse_content(lines: list[str]) -> dict:
    """Bullet outline -> {"summary": str, "items": [{"level": 1-4, "text": str}]}.
    Levels: • topic (1), ○ sub-topic (2), – item (3), • inside an item that ends with ':' (4)."""
    summary, items, nested = [], [], False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        marker = line[0]
        if marker in "•○–-" and len(line) > 1 and line[1] == " ":
            body = line[2:].strip()
            if marker == "•":
                level = 4 if nested else 1
            elif marker == "○":
                level, nested = 2, False
            else:
                level, nested = 3, body.endswith(":")
            if level == 1:
                nested = False
            items.append({"level": level, "text": body})
        elif items and items[-1]["level"] == 1 and line[0].isupper() and "note" not in items[-1]:
            items[-1]["note"] = line  # e.g. "Learners can explore technical skills relating to ..."
        elif items and "note" in items[-1] and items[-1]["level"] == 1:
            items[-1]["note"] += " " + line
        elif items:
            items[-1]["text"] += " " + line  # wrapped line continues the previous bullet
        else:
            summary.append(line)
    return {"summary": " ".join(summary), "items": items}


def _read_content_areas(text: str, module_order: list[str], area_names: dict) -> dict:
    """{module: {area title: {"summary", "items"}}}. A module's content sits before its own
    'ESSENTIAL INFORMATION FOR ASSESSMENT' heading; the LAST heading of each area name before
    that point is used (earlier copies are on the overview/guide pages)."""
    lines = _drop_running_footers(text.split("\n"))
    ends = [i for i, l in enumerate(lines) if CONTENT_END_RE.match(l.strip())]
    result, start = {}, 0
    for module, end in zip(module_order, ends):
        headings = {}
        for i in range(start, end):
            m = CAPS_HEADING_RE.match(lines[i].strip())
            if m and _area_key(m.group(1)) in area_names:
                headings[_area_key(m.group(1))] = i  # keeps the last occurrence
        ordered = sorted(headings.items(), key=lambda kv: kv[1])
        areas = {}
        for j, (key, line_no) in enumerate(ordered):
            stop = ordered[j + 1][1] if j + 1 < len(ordered) else end
            areas[area_names[key]] = _parse_content(lines[line_no + 1:stop])
        result[module] = areas
        start = end + 1
    return result


# ----------------------------------------------------------------------------- points + sizes
def _read_points(text: str, unit_codes: list[str]) -> dict:
    m = POINTS_START_RE.search(text)
    if not m:
        return {}
    block = text[m.end():m.end() + 400].strip().split("\n")
    header = block[0] if block else ""
    groups = [re.findall(r"[A-Z]\d", part) for part in re.split(r"Assessment units?", header) if part.strip()]
    points = {}
    for row in block[1:6]:
        parts = row.split()
        if not parts or parts[0] not in BANDS:
            continue
        values = [int(v) for v in parts[1:] if v.isdigit()]
        for group, value in zip(groups, values):
            for code in group:
                points.setdefault(code, {})[parts[0]] = value
    return {code: points[code] for code in unit_codes if code in points}


def _qualification_sizes(text: str, units: dict, order: list[str]) -> list[dict]:
    """Module A alone, then A+B, ... with their total hours. A size is only listed when the
    spec itself mentions that number of GLH; its name is taken from nearby wording if found."""
    modules = []
    for code in order:
        if units[code]["module"] not in modules:
            modules.append(units[code]["module"])
    sizes, included = [], []
    for module in modules:
        included.append(module)
        unit_codes = [c for c in order if units[c]["module"] in included]
        glh = sum(units[c]["glh"] for c in unit_codes)
        if f"{glh} GLH" not in text and f"{glh}GLH" not in text:
            continue
        name = None
        anchor = re.search(rf"\(\s*{glh}\s*GLH\s*\)", text)  # e.g. "...Extended Diploma in X (1080 GLH)"
        if anchor:
            window = text[max(0, anchor.start() - 160):anchor.start()]
            found = re.findall(r"((?:\d+\s+)?(?:Extended\s+)?(?:Diploma|Certificate))", window)
            if found:
                name = re.sub(r"\s+", " ", found[-1]).strip()
                # Two-column pages can split "540" and "Diploma" onto different lines.
                if not re.match(r"\d", name) and re.search(rf"\b{glh}\s*$", window[:window.rfind(found[-1])], re.MULTILINE):
                    name = f"{glh} {name}"
        sizes.append({"name": name or f"{glh} GLH", "glh": glh, "modules": list(included), "units": unit_codes})
    return sizes


# ----------------------------------------------------------------------------- main entry
def read(text: str, pdf_path: str | None = None) -> dict | None:
    if not looks_like(text):
        return None
    units, order, objectives = _read_units(text)
    if not order:
        return None

    problems, notes = [], []
    grids = read_band_grids(pdf_path) if pdf_path else []
    grid = grids[0] if grids else {}
    if len(grids) >= 2:
        # Compare words only: the spec's two printed copies differ in punctuation in places
        # (AC3 U: "evidence, work" vs "evidence. Work") - that is Pearson's text, not a misread.
        def words_only(entry):
            return {b: re.sub(r"[^a-z0-9]+", " ", (entry or {}).get("bands", {}).get(b, "").lower()).strip() for b in BANDS}
        same = all(words_only(grids[0].get(k)) == words_only(grids[1].get(k)) for k in grids[0])
        if same:
            notes.append("Grade grid read from the page layout and confirmed against the spec's second copy of it.")
        else:
            problems.append("The spec prints the grade grid twice and the two copies did not read the same - check the band wording.")
    elif grid:
        notes.append("Grade grid read from the page layout (only one copy found to cross-check).")
    elif pdf_path:
        problems.append("The grade grid (U / Pass / Merit / Distinction) could not be read from this PDF - criteria have titles but no band wording.")
    else:
        problems.append("No original PDF is stored for this spec, so the grade grid (U / Pass / Merit / Distinction) could not be split. "
                        "Upload the PDF again to get the band wording.")

    # AC titles: from the grid when we have it, otherwise from the AC label lines in the text.
    ac_titles = {code: entry["title"] for code, entry in grid.items()}
    if not ac_titles:
        for m in re.finditer(r"AC(\d):\s*([A-Z][a-z]+(?: [a-z]+)?)", text):
            ac_titles.setdefault(f"AC{m.group(1)}", m.group(2))
    area_names = {_area_key(t): t for t in ac_titles.values()}

    module_order = []
    for code in order:
        if units[code]["module"] not in module_order:
            module_order.append(units[code]["module"])
    module_titles = {}
    for m in re.finditer(r"^Module ([A-Z]):\s+(.+?)(?:\s+\d+)?\s*$", text, re.MULTILINE):
        module_titles.setdefault(m.group(1), m.group(2).strip())

    content = _read_content_areas(text, module_order, area_names)
    for module in module_order:
        missing = [t for t in ac_titles.values() if t not in content.get(module, {})]
        if missing:
            problems.append(f"Module {module}: teaching content not found for {', '.join(missing)}.")

    out_units = []
    for code in order:
        u = units[code]
        los = []
        for n in range(1, 10):
            title = objectives.get((code, n))
            if not title:
                continue
            ac = f"AC{n}"
            entry = grid.get(ac)
            ac_title = ac_titles.get(ac, ac)
            if entry:
                band_text = " | ".join(f"{b}: {entry['bands'][b]}" for b in BANDS if entry["bands"].get(b))
                description = f"{ac_title} - {band_text}"
            else:
                description = ac_title
            criterion = {"code": f"{code}.{ac}", "description": description}
            if entry:
                criterion["bands"] = entry["bands"]
            los.append({
                "code": f"{code}.{n}", "title": title,
                "content_area": ac_title if ac_title in content.get(u["module"], {}) else None,
                "assessed_by": ac,
                "criteria": [criterion],
            })
        if not los:
            problems.append(f"Assessment unit {code}: no objectives found.")
        out_units.append({
            "name": f"Assessment unit {code}: {u['title']}",
            "code": code, "module": u["module"], "glh": u["glh"],
            "learning_outcomes": los,
        })

    rule = PASS_RULE_RE.search(text)
    sizes = _qualification_sizes(text, units, order)
    extras = {
        "spec_style": "btec_practice",
        "qualification": {
            "sizes": sizes,
            "modules": [{"code": m, "title": module_titles.get(m, f"Module {m}"),
                         "units": [c for c in order if units[c]["module"] == m]} for m in module_order],
            "pass_rule": rule.group(0).strip() if rule else None,
        },
        "grading": {
            "type": "band_grid",
            "bands": BANDS,
            "criteria": grid,
            "points": _read_points(text, order),
        },
        "content_areas": content,
    }
    notes.insert(0, f"Read as BTEC 'Practice' style: {len(out_units)} assessment unit(s), "
                    f"{sum(len(u['learning_outcomes']) for u in out_units)} objective(s), "
                    f"{len(ac_titles)} shared grade criteria, teaching content for {len(content)} module(s).")
    if sizes:
        notes.append("Qualification sizes: " + "; ".join(f"{s['name']} = units {', '.join(s['units'])}" for s in sizes) + ".")
    return {
        "units": out_units,
        "unmatched": [],
        "extras": extras,
        "ruleset": "BTEC 'Practice' style (modules / assessment units / shared grade grid)",
        "problems": problems,
        "notes": notes,
    }
