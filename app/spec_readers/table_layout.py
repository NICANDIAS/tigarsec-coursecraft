"""
Stage 5 - the layout learner: reads ANY spec whose subject content is printed in tables,
driven by a small LAYOUT DESCRIPTION instead of code written for one exam board.

Why: AI is good at understanding a layout ("left column = topic, right column = statements")
but bad at copying hundreds of lines (its answer gets cut off, lines get dropped - seen on the
A level spec). So the work is split:
  1. describe  - work out the layout once: discover_layout() does it from the page layout with
                 no AI; ai_describe_layout() asks AI only for a SHORT description when that fails.
  2. read      - read_with_layout() copies every line itself, using word positions, spacing and
                 ruled lines (the same method as the A level reader), so nothing is skipped.
  3. remember  - the description is saved (DiscoveredSpecFormat.layout_json), so the next spec
                 with the same layout is read with no AI and no discovery at all.

A layout description (JSON, small enough to read and to save):
  {
    "kind": "table",
    "label": "Code | Subject content | What students need to learn",
    "header": ["Code", "Subject content", "What students need to learn"],  # header cells, left to right
    "roles": ["code", "topic", "statement"],      # topic | statement | code | guidance | ignore
    "unit_rule": {"type": "heading", "regex": "^Topic\\s+(\\d+...)..."}   # or "line_before" / "single"
    "topic_rule": {"regex": "..."} or null,       # topic headings ABOVE tables (when no topic column)
  }
Each unit heading becomes a unit, each topic a learning outcome, each statement a criterion.
Codes come from a code column / numbered heading when the spec has them, otherwise LO1, 1.1 ...
"""
import re
from collections import Counter

from .content_amplification import _join, _lines, _norm

ROLES = ("topic", "statement", "code", "guidance", "ignore")
CODE_RE = re.compile(r"^(?:[A-Z]{0,3}\d{1,3}(?:\.\d{1,3}){0,3}[a-z]?|[a-z]\)|\(?[ivx]{1,4}\))$")
DOTTED_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){0,3}$")
KEYWORDS = ("Component", "Unit", "Topic", "Theme", "Paper", "Module", "Section", "Area of study", "Content area")
KEYWORD_HEADING_RE = re.compile(r"^(" + "|".join(KEYWORDS) + r")\s+(\d{1,2}[A-Z]?)\s*[:\-–.]?\s+(\S.{1,90})$", re.IGNORECASE)
NUMBERED_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,3})\.?\s+([A-Z][^.]{2,90})$")
EXAM_LINE = r"^(Written examination|Non-exam assessment|Examination)\b"
GUIDANCE_HEADER_RE = re.compile(r"guidance|additional information|notes|comment|exemplification|examples|teaching", re.IGNORECASE)
TOC_TAIL_RE = re.compile(r"\s\.*\s*\d{1,3}$")  # "Topic 1: Running a business 4" - a contents line
MIN_CRITERIA = 5


# ----------------------------------------------------------------------------------- page helpers

def _all_words(pdf):
    """Every page's words, worked out once per open PDF (discovery reads the same pages several times)."""
    cached = getattr(pdf, "_coursecraft_words", None)
    if cached is None:
        cached = [_page_words(p) for p in pdf.pages]
        pdf._coursecraft_words = cached
    return cached


def _page_words(page):
    # size only: asking for the font too would split a word wherever the font changes
    # (a curly apostrophe printed in another font became "shareholders ’").
    words = page.extract_words(extra_attrs=["size"])
    bold_tops = {}
    for ch in page.chars:
        if ch["text"].strip():
            key = round(ch["top"])
            b, n = bold_tops.get(key, (0, 0))
            bold_tops[key] = (b + ("bold" in (ch.get("fontname") or "").lower()), n + 1)
    for w in words:
        b, n = bold_tops.get(round(w["top"]), (0, 1))
        w["bold"] = n > 0 and b / n >= 0.8
    return words


def _cells(words, gap=8.0):
    """Clusters the words of ONE line into cells: a gap wider than `gap` points starts a new cell."""
    cells = []
    for w in sorted(words, key=lambda w: w["x0"]):
        if cells and w["x0"] - cells[-1]["x1"] <= gap:
            cells[-1]["text"] += " " + w["text"]
            cells[-1]["x1"] = w["x1"]
        else:
            cells.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"]})
    return cells


def _is_heading(ws, body_size):
    bold = all(w.get("bold") for w in ws)
    big = (ws[0].get("size") or 0) >= body_size * 1.12
    return bold or big


def _body_size(pages_words):
    sizes = Counter(round(w.get("size") or 0, 1) for words in pages_words for w in words)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _footer_key(text):
    return re.sub(r"\d+", "#", _norm(text).lower())


def _footer_keys(pdf_words, heights):
    """Running footers: text in the bottom 12% of a page that repeats (numbers ignored) on at
    least a third of the pages, e.g. "(c) WJEC CBAC Ltd." or "Issue 1 - page 7"."""
    seen = Counter()
    for words, height in zip(pdf_words, heights):
        keys = {_footer_key(" ".join(w["text"] for w in ws)) for top, ws in _lines(words) if top > height * 0.88}
        seen.update(keys)
    return {k for k, n in seen.items() if n >= max(2, len(heights) // 3)}


def _footer_top(words, page_height, keys=frozenset()):
    """Top of the running footer on this page (the page bottom if there is none)."""
    tops = [top for top, ws in _lines(words) if top > page_height * 0.88 and _footer_key(" ".join(w["text"] for w in ws)) in keys]
    return min(tops) if tops else page_height


def _rules(page, top, bottom):
    return sorted({e["top"] for e in page.edges if e["orientation"] == "h" and e["x1"] - e["x0"] > 40 and top < e["top"] < bottom})


def _blocks(words, rules, size_hint):
    """A wrapped line (normal spacing, no rule in between) joins the block above; anything else
    starts a new block. Same rule as the A level reader."""
    out, last_top = [], None
    for top, ws in _lines(words):
        size = ws[0].get("size") or size_hint
        text = " ".join(w["text"] for w in ws)
        ruled = last_top is not None and any(last_top < y < top for y in rules)
        if out and last_top is not None and top - last_top <= size * 1.6 and not ruled:
            out[-1] = [out[-1][0], _join(out[-1][1], text)]
        else:
            out.append([top, text])
        last_top = top
    return [(t, _norm(x)) for t, x in out if _norm(x)]


def _header_key(texts):
    return "|".join(_norm(t).lower() for t in texts)


def _find_headers(lines, header):
    """Header lines on a page matching the layout's header cells. Returns [(top, bottom, [x0 per column])]."""
    key = _header_key(header)
    found = []
    for top, ws in lines:
        cells = _cells(ws)
        if len(cells) == len(header) and _header_key(c["text"] for c in cells) == key:
            found.append((top, max(w["bottom"] for w in ws), [c["x0"] for c in cells]))
    return found


# ----------------------------------------------------------------------------------- reading

def _segments(pdf, layout):
    """Walks the document top to bottom and yields events:
       ("heading", line_text, is_heading_style, page_no)       - a line outside any table
       ("table", {"col": role -> [(top, text)]}, page_no)    - one table (or the part on one page)"""
    header = layout["header"]
    roles = layout["roles"]
    all_words = _all_words(pdf)
    body_size = _body_size(all_words)
    keys = _footer_keys(all_words, [p.height for p in pdf.pages])
    for page_no, (page, words) in enumerate(zip(pdf.pages, all_words), start=1):
        lines = _lines(words)
        footer = _footer_top(words, page.height, keys)
        headers = _find_headers(lines, header)
        regions = []
        for k, (top, bottom, xs) in enumerate(headers):
            end = headers[k + 1][0] - 1 if k + 1 < len(headers) else footer - 1
            tables = [t for t in page.find_tables() if t.bbox[1] - 3 <= top <= t.bbox[3]]
            if tables:
                end = min(end, tables[0].bbox[3] + 1)
            regions.append((top, bottom, end, xs))
        # lines outside tables, in order, interleaved with the tables
        events = []
        for top, ws in lines:
            if top >= footer - 1 or any(r[0] - 1 <= top <= r[2] for r in regions):
                continue
            events.append((top, ("heading", _norm(" ".join(w["text"] for w in ws)), _is_heading(ws, body_size), page_no)))
        for top, bottom, end, xs in regions:
            bounds = [x - 4 for x in xs] + [page.width + 1]
            cols = {}
            inside = [w for w in words if bottom + 1 < w["top"] < end and w["x0"] >= bounds[0] - 2]
            rules = _rules(page, bottom, end)
            for c, role in enumerate(roles):
                if role == "ignore":
                    continue
                col_words = [w for w in inside if bounds[c] - 2 <= w["x0"] < bounds[c + 1] - 2]
                cols[role] = _blocks(col_words, rules, body_size)
            events.append((top, ("table", cols, page_no)))
        for _, event in sorted(events, key=lambda e: e[0]):
            yield event


def _unit_name(rule, line, number):
    if rule["type"] == "line_before":
        return f"Component {number}: {line}", f"Component {number}"
    m = re.match(rule["regex"], line, re.IGNORECASE)
    code = m.group(1) if m and m.groups() else str(number)
    return line, code


def read_with_layout(pdf_path, layout, pdf=None):
    """Reads the spec at pdf_path using a layout description. Returns
    {"units", "problems", "notes", "coverage"} or None if the layout does not fit.
    `pdf` - an already open pdfplumber PDF (discovery passes its own, so pages are read once)."""
    own = pdf is None
    if own:
        try:
            import pdfplumber
            pdf = pdfplumber.open(pdf_path)
        except Exception:
            return None
    if len(layout.get("header") or []) != len(layout.get("roles") or []) or "statement" not in layout["roles"]:
        return None
    unit_rule = layout.get("unit_rule") or {"type": "single"}
    topic_rule = layout.get("topic_rule") or None
    try:
        unit_re = re.compile(unit_rule["regex"], re.IGNORECASE) if unit_rule.get("type") in ("heading", "line_before") else None
        topic_re = re.compile(topic_rule["regex"], re.IGNORECASE) if topic_rule else None
    except (re.error, KeyError, TypeError):
        return None

    units, problems = [], []
    unit = lo = last_crit = None
    previous_line = ""
    words_in, words_out = 0, 0

    def new_unit(name, code):
        nonlocal unit, lo, last_crit
        unit = {"name": name, "code": code, "learning_outcomes": []}
        units.append(unit)
        lo = last_crit = None

    def new_lo(title, code=None):
        nonlocal lo, last_crit
        n = len(unit["learning_outcomes"]) + 1
        lo = {"code": code or f"LO{n}", "title": title or "Subject content", "criteria": []}
        unit["learning_outcomes"].append(lo)
        last_crit = None

    if unit_rule["type"] == "single":
        new_unit(layout.get("unit_name") or "Subject content", "1")

    try:
        events = list(_segments(pdf, layout))
    finally:
        if own:
            pdf.close()
    if True:
        for event in events:
            if event[0] == "heading":
                _, line, is_heading, _page = event
                if TOC_TAIL_RE.search(line) and not unit_re:
                    previous_line = line
                    continue
                if unit_rule["type"] == "line_before" and unit_re.match(line):
                    if previous_line and len(previous_line) < 90:
                        new_unit(*_unit_name(unit_rule, previous_line, len(units) + 1))
                        unit["assessment"] = line
                elif unit_rule["type"] == "heading" and is_heading and unit_re.match(line) and not TOC_TAIL_RE.search(line):
                    new_unit(*_unit_name(unit_rule, line, len(units) + 1))
                elif topic_re and unit and is_heading and topic_re.match(line) and not TOC_TAIL_RE.search(line):
                    m = topic_re.match(line)
                    code = m.group(1) if m.groups() else None
                    title = m.group(2) if m.groups() and len(m.groups()) > 1 else line
                    new_lo(_norm(title), code)
                previous_line = line
                continue

            _, cols, _page = event
            if not unit:
                continue  # a table before the first unit heading (introduction, contents)
            topics = cols.get("topic", [])
            statements = cols.get("statement", [])
            codes = cols.get("code", [])
            guidance = cols.get("guidance", [])
            words_in += sum(len(t.split()) for _, t in statements)

            topic_tops = [t for t, _ in topics]
            # a topic with no statements of its own, followed by more topics, is a heading over them
            parent = ""
            topic_events = []
            for k, (top, name) in enumerate(topics):
                nxt = topics[k + 1][0] if k + 1 < len(topics) else float("inf")
                own = [s for s in statements if top - 3 <= s[0] < nxt - 3]
                if not own and k + 1 < len(topics):
                    parent = name
                    continue
                topic_events.append((top, f"{parent} - {name}" if parent else name))

            def code_at(top):
                return next((c for t, c in codes if abs(t - top) <= 3), None)

            stream = sorted([(t, "topic", n) for t, n in topic_events] + [(t, "statement", s) for t, s in statements],
                            # a topic and its first statement share a line, a fraction of a point apart
                            key=lambda e: e[0] - (3 if e[1] == "topic" else 0))
            first = True
            crit_at = {}
            for top, kind, text in stream:
                if kind == "topic":
                    code = code_at(top) if "topic" in cols else None
                    new_lo(text, code if code and CODE_RE.match(code) else None)
                    first = False
                    continue
                # a statement carried over from the previous page (starts in lower case, no new topic)
                if first and last_crit is not None and text[:1].islower():
                    last_crit["description"] = _join(last_crit["description"], text)
                    first = False
                    words_out += len(text.split())
                    continue
                first = False
                if lo is None:
                    new_lo(unit["name"].split(": ", 1)[-1] if topic_re is None and "topic" not in cols else "")
                code = code_at(top) if "topic" not in cols else None
                if not code:
                    base = lo["code"] if DOTTED_RE.match(lo["code"] or "") else str(unit["learning_outcomes"].index(lo) + 1)
                    code = f"{base}.{len(lo['criteria']) + 1}"
                last_crit = {"code": code, "description": text}
                lo["criteria"].append(last_crit)
                crit_at[top] = last_crit
                words_out += len(text.split())
            for top, text in guidance:
                # guidance belongs to the statement level with (or just above) it
                owners = [t for t in crit_at if t <= top + 3]
                if owners:
                    crit = crit_at[max(owners)]
                    crit["guidance"] = _norm(f"{crit.get('guidance', '')} {text}")

    units = [u for u in units if u["learning_outcomes"] and any(l["criteria"] for l in u["learning_outcomes"])]
    for u in units:
        u["learning_outcomes"] = [l for l in u["learning_outcomes"] if l["criteria"]]
    criteria = sum(len(l["criteria"]) for u in units for l in u["learning_outcomes"])
    if not units or criteria < MIN_CRITERIA:
        return None
    coverage = words_out / words_in if words_in else 0
    return {"units": units, "problems": problems, "coverage": coverage,
            "notes": [f"Read from the spec's tables ({' | '.join(layout['header'])}) using the page layout (no AI)."]}


# ----------------------------------------------------------------------------------- discovery

def _header_candidates(pdf):
    """Repeated short BOLD lines split into 2-4 cells, each starting with a capital letter:
    table headers are printed in bold at the top of every table (or every page of it).
    Measured on 7 specs: real headers ("Content | Amplification", "Code | Subject content |
    What students need to learn") pass; bullet lists ("• | sequence") and wrapped prose
    ("sector or regulatory | Improvement Service (LSIS)") fail. Most common first."""
    seen = {}
    for page_no, words in enumerate(_all_words(pdf), start=1):
        for top, ws in _lines(words):
            cells = _cells(ws)
            text = " ".join(c["text"] for c in cells)
            if not 2 <= len(cells) <= 4 or len(text) > 80 or text.endswith(".") or not all(w.get("bold") for w in ws):
                continue
            if any(len(c["text"].split()) > 6 or re.search(r"\d", c["text"]) or not re.match(r"[A-Z]", c["text"]) for c in cells):
                continue
            key = _header_key(c["text"] for c in cells)
            entry = seen.setdefault(key, {"count": 0, "pages": set(), "cells": [c["text"] for c in cells]})
            entry["count"] += 1
            entry["pages"].add(page_no)
    out = [(e["count"], len(e["pages"]), e["cells"]) for e in seen.values() if e["count"] >= 2 and len(e["pages"]) >= 2]
    return sorted(out, key=lambda e: (-e[0], -e[1]))


MIN_SHARE = 0.25


def looks_like_content(result, document_words: int = 0) -> bool:
    """A layout is only accepted if what it reads looks like the spec's teaching content:
      * at least 10 statements, most of them sentences (4+ words, mostly letters), not numbers;
      * a real share of the whole document. Measured: content tables are 67-82% of the words
        (A level, test layouts); a glossary or weightings table elsewhere in a spec is ~1%.
        Below MIN_SHARE the layout is not trusted (the spec goes on to the AI fallback)."""
    statements = [c["description"] for u in result["units"] for l in u["learning_outcomes"] for c in l["criteria"]]
    if len(statements) < 10:
        return False
    if document_words and sum(len(s.split()) for s in statements) < MIN_SHARE * document_words:
        return False
    wordy = [s for s in statements if len(s.split()) >= 4 and sum(ch.isalpha() for ch in s) >= 0.6 * len(s)]
    return len(wordy) >= 0.8 * len(statements) and result["coverage"] >= 0.95


def _column_blocks(pdf, header):
    """Each table found with this header, as [blocks of column 0, blocks of column 1, ...]."""
    all_words = _all_words(pdf)
    body_size = _body_size(all_words)
    keys = _footer_keys(all_words, [p.height for p in pdf.pages])
    for page, words in zip(pdf.pages, all_words):
        lines = _lines(words)
        footer = _footer_top(words, page.height, keys)
        headers = _find_headers(lines, header)
        for k, (top, bottom, xs) in enumerate(headers):
            end = headers[k + 1][0] - 1 if k + 1 < len(headers) else footer - 1
            tables = [t for t in page.find_tables() if t.bbox[1] - 3 <= top <= t.bbox[3]]
            if tables:
                end = min(end, tables[0].bbox[3] + 1)
            bounds = [x - 4 for x in xs] + [page.width + 1]
            inside = [w for w in words if bottom + 1 < w["top"] < end and w["x0"] >= bounds[0] - 2]
            rules = _rules(page, bottom, end)
            yield [_blocks([w for w in inside if bounds[c] - 2 <= w["x0"] < bounds[c + 1] - 2], rules, body_size)
                   for c in range(len(header))]


def _heading_rules(pdf, layout):
    """Finds how units (and topics, if there is no topic column) are announced, from the
    heading-style lines (bold or larger) OUTSIDE the tables."""
    keyword, numbered, exam_lines = Counter(), Counter(), 0
    examples = {}
    for event in _segments(pdf, layout):
        if event[0] != "heading":
            continue
        _, line, is_heading, _ = event
        if re.match(EXAM_LINE, line, re.IGNORECASE):
            exam_lines += 1
        if not is_heading or TOC_TAIL_RE.search(line):
            continue
        m = KEYWORD_HEADING_RE.match(line)
        if m:
            keyword[m.group(1).title()] += 1
            examples.setdefault(m.group(1).title(), line)
            continue
        m = NUMBERED_HEADING_RE.match(line)
        if m:
            depth = m.group(1).count(".")
            numbered[depth] += 1
            examples.setdefault(depth, line)
    unit_rule, topic_rule = {"type": "single"}, None
    if keyword:
        word, count = keyword.most_common(1)[0]
        if count >= 1:
            unit_rule = {"type": "heading", "regex": rf"^{word}\s+(\d{{1,2}}[A-Z]?)\s*[:\-–.]?\s+\S", "example": examples[word]}
    depths = sorted(d for d, n in numbered.items() if n >= 1)
    if unit_rule["type"] == "single" and depths:
        d = depths[0]
        unit_rule = {"type": "heading", "regex": rf"^(\d{{1,2}}(?:\.\d{{1,2}}){{{d}}})\.?\s+[A-Z]", "example": examples[d]}
        depths = depths[1:]
    if depths and "topic" not in layout["roles"]:
        d = depths[0]
        topic_rule = {"regex": rf"^(\d{{1,2}}(?:\.\d{{1,2}}){{{d}}})\.?\s+([A-Z].*)$", "example": examples[d]}
    if unit_rule["type"] == "single" and exam_lines:
        unit_rule = {"type": "line_before", "regex": EXAM_LINE}
    return unit_rule, topic_rule


def discover_layout(pdf_path):
    """Works out a table layout from the PDF alone (no AI). Returns (layout, result) - the
    description and what reading with it gives - or (None, None)."""
    try:
        import pdfplumber
        pdf = pdfplumber.open(pdf_path)
    except Exception:
        return None, None
    with pdf:
        document_words = sum(len(w) for w in _all_words(pdf))
        candidates = _header_candidates(pdf)
        for count, pages, cells in candidates[:4]:
            stats = [{"blocks": 0, "words": 0, "codes": 0, "chars": 0} for _ in cells]
            tables = 0
            for table in _column_blocks(pdf, cells):
                tables += 1
                for c, blocks in enumerate(table):
                    for _, text in blocks:
                        stats[c]["blocks"] += 1
                        stats[c]["words"] += len(text.split())
                        stats[c]["chars"] += len(text)
                        stats[c]["codes"] += bool(CODE_RE.match(text))
            if not tables:
                continue
            roles = _roles_from_stats(cells, stats)
            if not roles:
                continue
            layout = {"kind": "table", "label": " | ".join(cells), "header": cells, "roles": roles}
            layout["unit_rule"], layout["topic_rule"] = _heading_rules(pdf, layout)
            result = read_with_layout(pdf_path, layout, pdf=pdf)
            if result and looks_like_content(result, document_words):
                result["share"] = round(sum(len(c["description"].split()) for u in result["units"] for l in u["learning_outcomes"]
                                            for c in l["criteria"]) / max(document_words, 1), 2)
                return layout, result
    return None, None


def _roles_from_stats(cells, stats):
    roles = ["ignore"] * len(cells)
    for c, s in enumerate(stats):
        if s["blocks"] and s["codes"] / s["blocks"] >= 0.7:
            roles[c] = "code"
        elif GUIDANCE_HEADER_RE.search(cells[c]):
            roles[c] = "guidance"
    free = [c for c in range(len(cells)) if roles[c] == "ignore" and stats[c]["blocks"]]
    if not free:
        return None
    statement = max(free, key=lambda c: stats[c]["words"])
    roles[statement] = "statement"
    for c in free:
        if c < statement:
            avg = stats[c]["chars"] / max(stats[c]["blocks"], 1)
            if avg <= 70 and stats[c]["blocks"] <= stats[statement]["blocks"]:
                roles[c] = "topic"
                break
    return roles


def layout_fits(pdf_path, layout, minimum=2):
    """Quick check before using a saved layout: does this PDF print its header at least twice?"""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            seen = 0
            for page in pdf.pages:
                seen += len(_find_headers(_lines(_page_words(page)), layout["header"]))
                if seen >= minimum:
                    return True
    except Exception:
        return False
    return False


def document_words(pdf_path) -> int:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return sum(len(w) for w in _all_words(pdf))
    except Exception:
        return 0


def sample_pages_text(pdf_path, pages=2, limit=7000) -> str:
    """The two pages that look most like a table (most lines split into columns), with the
    layout kept, for the AI to DESCRIBE. Only these few pages are sent, never the whole spec."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            words = _all_words(pdf)
            scored = sorted(range(len(pdf.pages)), key=lambda i: -sum(1 for _, ws in _lines(words[i]) if len(_cells(ws)) >= 2))
            picked = sorted(scored[:pages])
            text = "\n\n".join(f"--- page {i + 1} ---\n" + (pdf.pages[i].extract_text(layout=True) or "") for i in picked)
    except Exception:
        return ""
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]


def _rule_from_example(example, for_topic=False):
    """Turns ONE example heading line into a pattern, so the AI never writes the pattern itself:
    "Topic 2: Money and growth" -> Topic + number; "3.1.2 Customer needs" -> numbers with 2 dots."""
    example = _norm(example or "")
    if not example:
        return None
    m = KEYWORD_HEADING_RE.match(example)
    if m:
        word = re.escape(m.group(1))
        if for_topic:
            return {"regex": rf"^({word}\s+\d{{1,2}}[A-Z]?)\s*[:\-–.]?\s+(\S.*)$", "example": example}
        return {"type": "heading", "regex": rf"^{word}\s+(\d{{1,2}}[A-Z]?)\s*[:\-–.]?\s+\S", "example": example}
    m = NUMBERED_HEADING_RE.match(example)
    if m:
        d = m.group(1).count(".")
        if for_topic:
            return {"regex": rf"^(\d{{1,2}}(?:\.\d{{1,2}}){{{d}}})\.?\s+([A-Z].*)$", "example": example}
        return {"type": "heading", "regex": rf"^(\d{{1,2}}(?:\.\d{{1,2}}){{{d}}})\.?\s+[A-Z]", "example": example}
    if re.match(EXAM_LINE, example, re.IGNORECASE) and not for_topic:
        return {"type": "line_before", "regex": EXAM_LINE}
    return None


def layout_from_description(described) -> dict | None:
    """Checks the AI's short description and builds a layout from it. Anything that does not
    make sense (no header, wrong number of roles, no statement column) is rejected."""
    if not isinstance(described, dict):
        return None
    header = described.get("header")
    roles = described.get("roles")
    if not isinstance(header, list) or not isinstance(roles, list) or not 2 <= len(header) <= 4 or len(header) != len(roles):
        return None
    header = [_norm(str(h)) for h in header]
    roles = [str(r).strip().lower() for r in roles]
    if any(r not in ROLES for r in roles) or roles.count("statement") != 1 or not all(header):
        return None
    layout = {"kind": "table", "label": " | ".join(header), "header": header, "roles": roles, "described_by": "ai"}
    layout["unit_rule"] = _rule_from_example(described.get("unit_heading_example")) or {"type": "single"}
    layout["topic_rule"] = _rule_from_example(described.get("topic_heading_example"), for_topic=True) if "topic" not in roles else None
    return layout
