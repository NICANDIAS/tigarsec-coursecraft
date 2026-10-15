"""
Reader for "Content | Amplification" specs - the two-column subject content tables used by
WJEC / Eduqas for their A level and GCSE specifications.

Tested on: WJEC Eduqas GCE A level Business (601/4868/8), teaching from 2015, version 2.

Layout this reader expects (all confirmed in the real Eduqas spec):
  Business Opportunities and Functions          <- component title, on its own intro page
  Written examination: 2 hours 15 minutes
  33 1/3% of qualification
  ...
  | Content    | Amplification                                         |   <- table header
  | Enterprise | Explain the meaning of enterprise and SMEs            |
  |            | Explain how satisfying needs and wants can give       |   <- one statement over
  |            | opportunities to entrepreneurs                        |      two lines
A table row's left cell is a topic; each statement in its right cell is one thing learners
must be able to do. CourseCraft stores a component as a unit, a topic as a learning outcome
and a statement as a criterion (numbered 1.1, 1.2 ... because the spec has no codes).

Why the PDF layout and not the text: in the text the two columns are mixed together
("Business Explain the sources of finance..." / "finance Analyse ..."), so a topic name is
glued to the first statement and split across lines. The page layout says exactly which
words are in which column and where each statement starts (a blank gap above it).

Traps handled: a table that carries on onto the next page with an empty topic cell (that is
the same topic continued); the topic list on each component's intro page (not a table);
pages before the first component (introduction) and after the content (assessment).
Also read, for the spec page: each component's exam (duration, weighting), the assessment
objectives AO1-AO4 with their weightings, and Appendix A (quantitative skills) as a unit.
"""
import re

HEADER_LEFT, HEADER_RIGHT = "Content", "Amplification"
EXAM_RE = re.compile(r"^(Written examination|Non-exam assessment)\b", re.IGNORECASE)
WEIGHT_RE = re.compile(r"^(\d+(?:\s*⅓|\s*⅔|\.\d+)?)\s*%\s+of\s+qualification", re.IGNORECASE)
AO_RE = re.compile(r"^AO(\d)$")


def looks_like(text: str) -> bool:
    return len(re.findall(r"^\s*Content\s+Amplification\s*$", text or "", re.MULTILINE)) >= 3


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _lines(words, tolerance=2.5):
    """Groups words into text lines (top to bottom); each line = (top, [words left to right])."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0] - w["top"]) <= tolerance:
            lines[-1][1].append(w)
        else:
            lines.append([w["top"], [w]])
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in lines]


def _join(a: str, b: str) -> str:
    """Joins a wrapped line onto the one above ("decision-" + "making" -> "decision-making")."""
    return f"{a}{b}" if a.endswith("-") else f"{a} {b}"


def _blocks(words, rules=()):
    """Splits words into blocks: a wrapped line (normal line spacing) belongs to the block
    above; a blank gap, or a ruled line between two lines (the next table row), starts a new
    block. Returns [(top_of_block, text)].
    Measured in the real spec (11pt text): a wrapped line sits 12.6-14.6pt below the one above,
    the next statement ~25pt below; the next table row only ~16pt below, but with a rule."""
    out, last_top, size = [], None, 11.0
    for top, ws in _lines(words):
        size = ws[0].get("size") or size
        text = " ".join(w["text"] for w in ws)
        ruled = last_top is not None and any(last_top < y < top for y in rules)
        if last_top is not None and top - last_top <= size * 1.6 and not ruled and out:
            out[-1] = (out[-1][0], _join(out[-1][1], text))
        else:
            out.append((top, text))
        last_top = top
    return [(top, _norm(t)) for top, t in out if _norm(t)]


def _page_lines(page):
    return [" ".join(w["text"] for w in ws) for _, ws in _lines(page.extract_words(extra_attrs=["size"]))]


def _content_rows(page):
    """Returns [(topic_or_empty, [statements])] for the Content / Amplification table on this
    page, or None if the page has no such table. Uses word positions, not the table's ruled
    lines: on some pages several topics sit in one ruled box. A topic starts where a name
    appears in the left column; each statement goes to the topic level with or above it.
    Statements above the first name on a page continue the previous page's topic."""
    words = page.extract_words(extra_attrs=["size"])
    left = [w for w in words if w["text"] == HEADER_LEFT]
    right = [w for w in words if w["text"] == HEADER_RIGHT]
    header = next(((l, r) for l in left for r in right if abs(l["top"] - r["top"]) < 3 and r["x0"] > l["x0"]), None)
    if not header:
        return None
    split_x = header[1]["x0"] - 4
    left_x = header[0]["x0"] - 6
    footer = min([w["top"] for w in words if w["text"].startswith("©") and w["top"] > header[0]["bottom"]] or [page.height])
    body = [w for w in words if header[0]["bottom"] + 1 < w["top"] < footer - 1 and w["x0"] >= left_x]
    rules = sorted({e["top"] for e in page.edges if e["orientation"] == "h" and e["x1"] - e["x0"] > 50
                    and header[0]["bottom"] + 1 < e["top"] < footer})
    topics = _blocks([w for w in body if w["x1"] <= split_x + 2], rules)
    statements = _blocks([w for w in body if w["x0"] >= split_x - 2], rules)
    rows = [("", [])] if not topics or (statements and statements[0][0] < topics[0][0] - 3) else []
    rows += [(name, []) for _, name in topics]
    starts = [t for t, _ in topics]
    for top, text in statements:
        index = sum(1 for t in starts if t <= top + 3) - 1 + (1 if rows and rows[0][0] == "" else 0)
        rows[max(index, 0)][1].append(text)
    return [r for r in rows if r[0] or r[1]]


def _component_heading(lines):
    """On a component's intro page: the title is the line just above 'Written examination...'.
    Returns (title, exam, weighting) or None."""
    for n, line in enumerate(lines):
        if EXAM_RE.match(line) and n > 0:
            title = _norm(lines[n - 1])
            weighting = next((_norm(m.group(1)).replace(" ⅓", "⅓").replace(" ⅔", "⅔") + "%"
                              for l in lines[n:n + 3] if (m := WEIGHT_RE.match(l))), "")
            if title and not title.upper().startswith("A LEVEL") and len(title) < 90:
                return title, _norm(line), weighting
    return None


def _assessment_objectives(lines):
    """AO1..AO4: 'AO1' on its own line, its wording on the lines after it."""
    aos, current = [], None
    for line in lines:
        m = AO_RE.match(line.strip())
        if m:
            current = {"code": f"AO{m.group(1)}", "description": ""}
            aos.append(current)
        elif current is not None:
            if line.startswith("The table below") or line.startswith("©"):
                current = None
            else:
                current["description"] = _norm(f"{current['description']} {line}")
    return aos


def _ao_weightings(lines, codes):
    rows = []
    for line in lines:
        m = re.match(r"^(Component \d|Overall weighting)\s+(.*)$", line)
        if m:
            cells = re.findall(r"\d+(?:\s*-\s*\d+)?%", m.group(2))
            if len(cells) == len(codes):
                rows.append({"name": m.group(1), **{c: v.replace(" ", "") for c, v in zip(codes, cells)}})
    return rows


def _appendix_skills(lines):
    """Appendix A: the bulleted quantitative skills (bullets come through as \\uf0b7)."""
    skills, started = [], False
    for line in lines:
        if "including being able to" in line:
            started = True
            continue
        if not started:
            continue
        if line.startswith("The assessment of") or line.startswith("©"):
            break
        text = line.replace("", "").strip()
        if not text:
            continue
        if line.lstrip().startswith("") or not skills:
            skills.append(text)
        else:
            skills[-1] = f"{skills[-1]} {text}"
    return [_norm(s) for s in skills]


def read(text: str, pdf_path: str | None = None) -> dict | None:
    if not looks_like(text) or not pdf_path:
        return None
    try:
        import pdfplumber
        pdf = pdfplumber.open(pdf_path)
    except Exception:
        return None

    units, problems, notes = [], [], []
    extras = {}
    current_unit, current_lo = None, None
    aos, weightings, skills = [], [], []
    with pdf:
        for page in pdf.pages:
            lines = _page_lines(page)
            heading = _component_heading(lines)
            if heading:
                title, exam, weighting = heading
                current_unit = {"name": f"Component {len(units) + 1}: {title}", "code": f"Component {len(units) + 1}",
                                "assessment": ", ".join(x for x in (exam, f"{weighting} of qualification" if weighting else "") if x),
                                "learning_outcomes": []}
                units.append(current_unit)
                current_lo = None
                continue
            rows = _content_rows(page) if current_unit else None
            if rows:
                # A topic with no statements of its own, followed by more topics on the page, is
                # a heading over them ("PEST factors" over Political, Economic ...).
                parent = ""
                for k, (topic, statements) in enumerate(rows):
                    if topic and not statements and any(t for t, _ in rows[k + 1:]):
                        parent = topic
                        continue
                    if topic and parent:
                        topic = f"{parent} - {topic}"
                    if topic or current_lo is None:
                        current_lo = {"code": f"LO{len(current_unit['learning_outcomes']) + 1}",
                                      "title": topic or "(continued)", "criteria": []}
                        current_unit["learning_outcomes"].append(current_lo)
                    for s in statements:
                        n = current_unit["learning_outcomes"].index(current_lo) + 1
                        current_lo["criteria"].append({"code": f"{n}.{len(current_lo['criteria']) + 1}", "description": s})
                continue
            if any(AO_RE.match(l.strip()) for l in lines) and not aos:
                aos = _assessment_objectives(lines)
                weightings = _ao_weightings(lines, [a["code"] for a in aos])
                current_unit = None  # the subject content has ended
            if any("quantitative skills" in l.lower() for l in lines) and any("including being able to" in l for l in lines):
                skills = _appendix_skills(lines)

    units = [u for u in units if u["learning_outcomes"]]
    if not units:
        return None
    if skills:
        units.append({"name": "Appendix A: Quantitative skills", "code": "Appendix A",
                      "learning_outcomes": [{"code": "LO1", "title": "Quantitative skills in business",
                                             "criteria": [{"code": f"1.{k}", "description": s} for k, s in enumerate(skills, 1)]}]})
        notes.append("Appendix A (quantitative skills) was added as a unit - the spec says these are assessed across the exams.")
    if aos:
        extras["assessment"] = {"objectives": aos, "weightings": weightings,
                                "components": [{"name": u["name"], "assessment": u.get("assessment", "")}
                                               for u in units if u.get("assessment")]}
    for u in units:
        for lo in u["learning_outcomes"]:
            if not lo["criteria"]:
                problems.append(f"'{u['name']}' / {lo['title']}: no statements were read for this topic.")
    notes.append("Read from the spec's Content / Amplification tables using the page layout (no AI). "
                 "The spec has no codes, so topics are numbered LO1, LO2 ... and statements 1.1, 1.2 ...")
    return {"units": units, "unmatched": [], "ruleset": "Content / Amplification tables (WJEC / Eduqas A level, GCSE)",
            "extras": extras, "problems": problems, "notes": notes}
