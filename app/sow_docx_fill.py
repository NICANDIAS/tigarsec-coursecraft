"""
Fills the ACTUAL uploaded scheme-of-work template document, in place - the same standing
principle as rubric_fill.py, applied to a college's SOW template instead of a marking
rubric.

WHAT THIS DOES vs WHAT IT DELIBERATELY DOES NOT DO:

- Finds the template's main week-by-week grid table and, for each row, decides whether it's
  a PROTECTED row (the college has already put real content in it - "Half Term", "Christmas
  Break", a bank holiday, a named conference/exam date, an assessment window - detected as
  any row where a column OTHER than the week/date column already has text) or an AVAILABLE
  row (every other column is blank - a genuine open teaching week). Only AVAILABLE rows
  receive generated content; PROTECTED rows are never touched. This is the core "know where
  to add details, know where not to" behaviour that was asked for.

- Maps the already-generated weeks_json onto AVAILABLE rows IN ORDER (week 1 -> first
  available row, week 2 -> second, etc.). If there are more generated weeks than available
  rows, the overflow is reported as a warning rather than silently dropped or forced in
  somewhere. If there are fewer, the remaining available rows are simply left blank.

- If asked to (via header_updates), updates ONLY the Start Date / End Date / Tutor(s)
  fields in the template's header info table, matching whatever date separator/format the
  template's own cell already uses rather than imposing a new one. Nothing else in the
  header table (e.g. Course Title) is touched.

- Deliberately does NOT attempt to add or remove rows in the grid, shift the whole calendar,
  or resolve a mismatch between a requested end date and the template's existing week rows -
  those rows may encode real fixed institutional dates (half terms, bank holidays,
  conferences) that this tool has no way to know are safe to move. A mismatch is reported as
  a warning for the teacher to resolve manually, rather than guessed at.

Uses the same paraId-anchored, run-formatting-preserving, read-back-verified low-level
approach as rubric_fill.py (see docx_xml_edit.py).
"""
import re

from docx import Document

from .docx_xml_edit import (
    qn, cell_text, all_existing_para_ids, ensure_para_id, clone_rpr_from_paragraph,
    build_run, build_mixed_runs, replace_paragraph_text, unzip_docx, rezip_docx, parse_document_xml,
    write_document_xml, assert_paragraph_count_unchanged,
)

WEEK_HEADER_HINTS = ("week", "date", "commencing")
DATE_LABEL_PATTERNS = {
    "start_date": re.compile(r'\bstart\s*date\b', re.IGNORECASE),
    "end_date": re.compile(r'\bend\s*date\b', re.IGNORECASE),
}
TUTOR_LABEL_PATTERN = re.compile(r'\btutor', re.IGNORECASE)
COURSE_TITLE_LABEL_PATTERN = re.compile(r'\bcourse\s*title\b', re.IGNORECASE)
UNIT_TITLE_LABEL_PATTERN = re.compile(r'\bunit\s*/?\s*project\s*title\b', re.IGNORECASE)
UNIT_OUTCOMES_LABEL_PATTERN = re.compile(r'\bunit\s*outcomes?\b', re.IGNORECASE)
CONTEXT_LABEL_PATTERN = re.compile(r'\bcontext\s+for\s+the\s+unit', re.IGNORECASE)
IMPACT_LABEL_PATTERN = re.compile(r'\bintended\s+impact\b', re.IGNORECASE)
DATE_VALUE_PATTERN = re.compile(r'^\s*(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\s*$')


def _format_date_like(d, template_value: str) -> str:
    """Formats a date to match the separator style already used in the template's own
    cell (e.g. '7/9/2026' -> keep '/', '7-9-2026' -> keep '-'), day-first as this
    template's own values already are, with no leading zeros (matching '7/9/2026' rather
    than '07/09/2026') - never a new/foreign date style imposed on the document."""
    sep = "/"
    m = DATE_VALUE_PATTERN.match(template_value or "")
    if m:
        # infer separator actually present
        sep = "/" if "/" in template_value else "-"
    return f"{d.day}{sep}{d.month}{sep}{d.year}"


def _find_grid_table(root):
    """The main week-by-week grid: the table whose header row's first cell mentions
    'week' (or 'date'/'commencing'), preferring the one with the most rows if more than
    one table matches - a small header/info table incidentally containing the word
    'week' shouldn't be mistaken for the real grid."""
    best = None
    best_row_count = -1
    for table in root.iter(qn("w:tbl")):
        rows = table.findall(qn("w:tr"))
        if not rows:
            continue
        first_row_cells = rows[0].findall(qn("w:tc"))
        if not first_row_cells:
            continue
        header_text = cell_text(first_row_cells[0]).strip().lower()
        if any(hint in header_text for hint in WEEK_HEADER_HINTS):
            if len(rows) > best_row_count:
                best = table
                best_row_count = len(rows)
    return best


def _find_header_fields(root) -> dict:
    """
    Locates the Start Date / End Date value cells (the cell immediately AFTER a cell
    whose text is the label, in the same row), the Tutor(s) / Course Title / Unit-Project
    Title value paragraphs (the paragraph immediately AFTER a label-style paragraph,
    within the SAME cell - this template keeps several label/value pairs as two separate
    paragraphs in one cell, unlike the date fields which use two separate cells), and the
    Context/Intended-impact value cell (this template stacks BOTH labels together in one
    cell with no per-label answer space of their own - the actual answer area is a single
    shared empty cell in the row directly below, same column - so both get written into
    that one cell, clearly sub-labelled, rather than the document being restructured to
    give them separate spaces it was never built with).
    """
    fields = {
        "start_date_cell": None, "end_date_cell": None,
        "tutor_paragraph": None, "tutor_label_paragraph": None,
        "course_title_paragraph": None, "course_title_label_paragraph": None,
        "unit_title_paragraph": None, "unit_title_label_paragraph": None,
        "unit_outcomes_paragraph": None, "unit_outcomes_label_paragraph": None,
        "context_impact_value_cell": None,
    }
    for table in root.iter(qn("w:tbl")):
        rows = table.findall(qn("w:tr"))
        for row_idx, row in enumerate(rows):
            cells = row.findall(qn("w:tc"))
            for i, cell in enumerate(cells):
                text = cell_text(cell).strip()
                if DATE_LABEL_PATTERNS["start_date"].search(text) and i + 1 < len(cells):
                    fields["start_date_cell"] = cells[i + 1]
                if DATE_LABEL_PATTERNS["end_date"].search(text) and i + 1 < len(cells):
                    fields["end_date_cell"] = cells[i + 1]

                # Label + value as two paragraphs within the SAME cell
                paras = cell.findall(qn("w:p"))
                for pi, p in enumerate(paras):
                    p_text = "".join(t.text or "" for t in p.iter(qn("w:t"))).strip()
                    if TUTOR_LABEL_PATTERN.search(p_text) and pi + 1 < len(paras):
                        fields["tutor_label_paragraph"] = p
                        fields["tutor_paragraph"] = paras[pi + 1]
                    if COURSE_TITLE_LABEL_PATTERN.search(p_text) and pi + 1 < len(paras):
                        fields["course_title_label_paragraph"] = p
                        fields["course_title_paragraph"] = paras[pi + 1]
                    if UNIT_TITLE_LABEL_PATTERN.search(p_text) and pi + 1 < len(paras):
                        fields["unit_title_label_paragraph"] = p
                        fields["unit_title_paragraph"] = paras[pi + 1]
                    if UNIT_OUTCOMES_LABEL_PATTERN.search(p_text) and pi + 1 < len(paras):
                        fields["unit_outcomes_label_paragraph"] = p
                        fields["unit_outcomes_paragraph"] = paras[pi + 1]

                # Context/Intended-impact: both labels live together in one cell with no
                # per-label answer space - the real answer area is the equivalent cell in
                # the NEXT row of the same table, same column index.
                if CONTEXT_LABEL_PATTERN.search(text) or IMPACT_LABEL_PATTERN.search(text):
                    if row_idx + 1 < len(rows):
                        next_row_cells = rows[row_idx + 1].findall(qn("w:tc"))
                        if i < len(next_row_cells):
                            fields["context_impact_value_cell"] = next_row_cells[i]
    return fields


def _has_vmerge_continuation(cell) -> bool:
    """
    True if this cell is a continuation of a vertically-merged cell from the row above
    (a <w:vMerge/> with no val="restart") - it has no real content or visible area of its
    own, so it must never be treated as blank/available, even though cell_text() on it
    returns empty (that's exactly the trap: a vMerge continuation cell always looks blank
    in raw XML, but it's actually still part of whatever's merged above it, e.g. the
    second row of a two-row "Christmas Break" block).
    """
    tcpr = cell.find(qn("w:tcPr"))
    if tcpr is None:
        return False
    vmerge = tcpr.find(qn("w:vMerge"))
    if vmerge is None:
        return False
    return vmerge.get(qn("w:val")) != "restart"


def _parse_append_labels(text: str | None) -> list[str]:
    """Parses a comma-separated list of labels the teacher has said are still real
    teaching weeks even though the template already has a short label in them (e.g.
    'Discover, Understand, Develop, Look Ahead, Engage, One to one week') - deliberately
    NOT auto-detected from the text itself. Distinguishing a week that's genuinely a
    closure (Half Term, Bank Holiday) from one that's a real teaching week with an
    induction/pastoral theme already in it isn't something that can be safely guessed
    for an arbitrary template - only the teacher reliably knows which of their own
    calendar's labels mean "still teach something this week"."""
    if not text:
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


def _row_fill_mode(row, append_eligible_labels: list[str]) -> str:
    """
    Returns one of:
    - "blank": every column other than the week/date column is genuinely empty - filled
      normally, exactly as before.
    - "append": the row already has text, but it matches one of the teacher's own
      append_eligible_labels (e.g. "Discover") - content is still written in, appended
      UNDER the existing text rather than replacing it, so both are visible together.
    - "protected": has existing text that does NOT match an append-eligible label (or is
      a vertical-merge continuation of a protected block above) - left completely alone,
      the same as before. This is still the default for anything not explicitly named as
      append-eligible, so nothing gets written into a genuine closure/assessment week by
      mistake.
    """
    cells = row.findall(qn("w:tc"))
    if len(cells) < 2:
        return "protected"
    if any(_has_vmerge_continuation(c) for c in cells[1:]):
        return "protected"
    other_text = "".join(cell_text(c) for c in cells[1:]).strip()
    if not other_text:
        return "blank"
    if append_eligible_labels and any(label.lower() in other_text.lower() for label in append_eligible_labels):
        return "append"
    return "protected"


def _is_row_available(row, append_eligible_labels: list[str] | None = None) -> bool:
    """Backward-compatible boolean wrapper: True for both 'blank' and 'append' rows -
    i.e. any row that's a genuine fill target, regardless of whether it's empty or
    already has an append-eligible label in it."""
    return _row_fill_mode(row, append_eligible_labels or []) in ("blank", "append")


def _split_title_phase_and_topic(raw_title: str) -> list[str]:
    """
    The model has its own habit of writing "title" as a short phase-word (Discover,
    Understand, Develop, Look Ahead, Engage) followed by the actual lesson topic, both
    in one string with a single embedded newline - not something we prompt for, just
    an emergent stylistic pattern. Splitting it into two properly blank-line-separated
    lines (rather than the single line-break the model itself put there) reads as two
    distinct things - a phase label and a topic - the same reasoning already applied to
    separating the learning outcome statement below it.

    Extracted as its own function, used by both the writer (_week_fill_lines) and the
    read-back verification below, so the two can never drift into checking for a
    different shape of text than what actually gets written.
    """
    if "\n" in raw_title:
        phase_label, topic = raw_title.split("\n", 1)
        return [phase_label.strip(), "", topic.strip()]
    return [raw_title]


def _week_fill_lines(week: dict) -> tuple[list, list, list]:
    """Builds the three content columns' text from one week of the already-generated
    weeks_json - the exact same fields the in-app view and the general-shape export
    already use, just aimed at different cells."""
    topic_lines = _split_title_phase_and_topic(week.get("title", ""))
    # The actual learning outcome SENTENCE (Bloom's-verb-driven, e.g. "Explain the
    # purpose of...") - this is what a reader of the "Learning outcome and lesson topic"
    # column actually needs to see; the bare LO code(s) below it stay too, since
    # coverage-tracking matches against those specifically and must keep working. A
    # blank line separates it from the topic title above - without it, the two run
    # straight into each other and read as one sentence rather than two distinct things
    # (a lesson topic and a learning outcome), confirmed as a real readability problem.
    if week.get("learning_outcome_statement"):
        topic_lines.append("")
        topic_lines.append(week["learning_outcome_statement"])
    los = week.get("learning_outcomes") or []
    if los:
        topic_lines.append("(" + ", ".join(los) + ")")

    content_lines = [f"- {a}" for a in (week.get("activities") or [])]
    if week.get("practical"):
        # A blank line before this, when there's preceding content to separate it
        # from - without it, "Practical:" ran straight on from the last activity
        # bullet with no visual break, exactly the readability problem already fixed
        # for the topic/outcome split above, just not yet applied here too.
        if content_lines:
            content_lines.append("")
        # Bold label, no leading "- " - a labelled sub-section reads distinctly from a
        # plain bullet; with the dash it was visually indistinguishable from the
        # activities above it despite meaning something different.
        content_lines.append(("Practical: ", week["practical"]))
    if week.get("notes"):
        if content_lines:
            content_lines.append("")
        # Same fix, same reasoning - Notes had the identical problem (dash-prefixed,
        # reading as just another bullet item) and needed the same treatment, not just
        # the one example that happened to be pointed out.
        content_lines.append(("Notes: ", week["notes"]))
    if not content_lines:
        content_lines = [""]

    assignment = week.get("assignment")
    assessment_lines = []
    if assignment:
        assessment_lines.append(assignment.get("title", ""))
        if assignment.get("brief"):
            assessment_lines.append(assignment["brief"])
        covered = assignment.get("criteria_covered") or []
        if covered:
            assessment_lines.append(f"Covers: {', '.join(covered)}")
    else:
        assessment_lines = [""]

    return topic_lines, content_lines, assessment_lines


def _map_logical_columns_to_cells(cells) -> dict:
    """
    Maps logical 0-indexed grid columns (0=week/date, 1=topic, 2=content, 3=assessment) to
    the actual <w:tc> element that covers each one, accounting for gridSpan. Without this,
    a row where the last three columns are merged into one physical cell (confirmed as a
    real case in a real template - a genuinely blank week whose merged cell spans columns
    2-4) would only ever get its FIRST logical column written (topic), silently dropping
    the generated content/assessment text for that week entirely rather than combining it
    into the one cell that actually exists.
    """
    mapping = {}
    col = 0
    for cell in cells:
        tcpr = cell.find(qn("w:tcPr"))
        span_el = tcpr.find(qn("w:gridSpan")) if tcpr is not None else None
        span = int(span_el.get(qn("w:val"))) if span_el is not None else 1
        for c in range(col, col + span):
            mapping[c] = cell
        col += span
    return mapping


def _parse_grid_date(text: str):
    """Parses this kind of template's own week-commencing date format (e.g. '7-9-2026' or
    '7/9/2026', day-first) - returns None for non-date labels like 'Induction Week', which
    is fine, since those rows are never available/countable anyway."""
    m = re.match(r'^\s*(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\s*$', text or "")
    if not m:
        return None
    day, month, year = (int(x) for x in m.groups())
    try:
        import datetime
        return datetime.date(year, month, day)
    except ValueError:
        return None


def count_available_weeks_in_range(template_path: str, start_date, end_date, append_eligible_labels: list[str] | None = None) -> int | None:
    """
    Standing fix: a raw day-count between two dates assumes every calendar week in
    between is a free teaching week - not true once a real template is attached
    (induction weeks, conferences, half terms, Christmas all eat into it). If a .docx
    template was uploaded, count its ACTUAL available weeks in this date range instead -
    both "blank" rows and rows the teacher has explicitly marked as append_eligible (see
    _row_fill_mode) count as real teaching capacity; anything else does not. Uses the
    exact same per-row classification as the actual fill, so the count and the fill can
    never disagree with each other.

    Returns None if the template can't be read or has no recognisable grid, so the
    caller can fall back to the simpler day-count estimate rather than failing outright.
    """
    append_eligible_labels = append_eligible_labels or []
    try:
        work_dir, unpacked = unzip_docx(template_path)
        try:
            tree, _ = parse_document_xml(unpacked)
            root = tree.getroot()
            grid_table = _find_grid_table(root)
            if grid_table is None:
                return None
            rows = grid_table.findall(qn("w:tr"))
            count = 0
            for row in rows[1:]:
                if _row_fill_mode(row, append_eligible_labels) not in ("blank", "append"):
                    continue
                cells = row.findall(qn("w:tc"))
                if not cells:
                    continue
                row_date = _parse_grid_date(cell_text(cells[0]).strip())
                if row_date is None:
                    continue
                if start_date <= row_date <= end_date:
                    count += 1
            return count
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        return None


def fill_sow_template_docx(
    template_path: str,
    weeks_json: list[dict],
    header_updates: dict,
    output_path: str,
    append_eligible_labels: list[str] | None = None,
) -> tuple[dict, list[str]]:
    """
    header_updates: {"start_date": date|None, "end_date": date|None, "tutor_names": str|None,
                      "course_title": str|None, "unit_title": str|None,
                      "context_rationale": str|None, "intended_impact": str|None}
    - each only applied if not None/empty; anything else in the template is left alone.
    course_title/unit_title are deterministic corrections from the app's own known data
    (the course/unit this scheme was actually generated for), not AI-guessed.

    Returns (report, warnings). report = {
        "weeks_filled": int, "available_rows": int, "weeks_overflow": int,
        "header_fields_updated": [str, ...],
    }
    """
    warnings: list[str] = []
    work_dir, unpacked = unzip_docx(template_path)
    try:
        tree, document_xml_path = parse_document_xml(unpacked)
        root = tree.getroot()
        original_paragraph_count = len(list(root.iter(qn("w:p"))))
        existing_para_ids = all_existing_para_ids(root)

        grid_table = _find_grid_table(root)
        if grid_table is None:
            raise RuntimeError(
                "Could not find a week-by-week grid table in this template (looked for a "
                "table whose first column header mentions 'week', 'date', or 'commencing'). "
                "No content was written - the template file is unchanged."
            )

        rows = grid_table.findall(qn("w:tr"))
        append_eligible_labels = append_eligible_labels or []
        available_rows = [r for r in rows[1:] if _row_fill_mode(r, append_eligible_labels) in ("blank", "append")]

        weeks_filled = 0
        for i, week in enumerate(weeks_json):
            if i >= len(available_rows):
                break
            row = available_rows[i]
            cells = row.findall(qn("w:tc"))
            topic_lines, content_lines, assessment_lines = _week_fill_lines(week)

            # Logical columns: 1=topic, 2=content, 3=assessment. Grouped by which ACTUAL
            # cell covers each one (see _map_logical_columns_to_cells) - if two or three of
            # these are merged into one physical cell (a real case: a blank week whose last
            # three columns are merged), all their content is combined into that one cell
            # instead of the later logical columns being silently dropped.
            col_map = _map_logical_columns_to_cells(cells)
            sections = [(1, topic_lines), (2, content_lines)]
            if assessment_lines != [""]:
                sections.append((3, assessment_lines))

            cell_content: dict = {}  # id(cell) -> (cell, [line, line, ...])
            for logical_col, lines in sections:
                target_cell = col_map.get(logical_col)
                if target_cell is None:
                    continue
                key = id(target_cell)
                if key not in cell_content:
                    cell_content[key] = (target_cell, [])
                else:
                    cell_content[key][1].append("")  # blank-line separation between merged sections
                cell_content[key][1].extend(lines)

            for target_cell, lines in cell_content.values():
                paras = target_cell.findall(qn("w:p"))
                target_p = paras[-1] if paras else None
                if target_p is not None:
                    # If this cell already has real content (an append-eligible row, e.g.
                    # "Discover"), keep that text and add the new lesson content as a
                    # clearly separated block underneath it, rather than replacing it -
                    # the teacher explicitly wants both visible together.
                    if cell_text(target_cell).strip():
                        lines = ["", ""] + lines
                    ensure_para_id(target_p, existing_para_ids, warnings, f"Week {week.get('week', '?')} cell")
                    rpr = clone_rpr_from_paragraph(target_p)

                    # Normalize paragraph-level formatting before writing - confirmed as
                    # a real, standing cause of inconsistent-looking output: this
                    # inherits whatever pStyle/indent the TEMPLATE's specific blank row
                    # already happened to have (some rows carry a leftover "ListParagraph"
                    # style with its own left indent from earlier manual editing, others
                    # don't), so two weeks filled by identical code could render with
                    # different left margins purely because of which template row they
                    # landed on. Stripping these makes every week consistent regardless.
                    ppr = target_p.find(qn("w:pPr"))
                    if ppr is not None:
                        for tag in ("w:pStyle", "w:ind"):
                            el = ppr.find(qn(tag))
                            if el is not None:
                                ppr.remove(el)

                    for run in build_mixed_runs(lines, rpr):
                        target_p.append(run)

            weeks_filled += 1

        weeks_overflow = max(0, len(weeks_json) - len(available_rows))
        if weeks_overflow:
            warnings.append(
                f"{weeks_overflow} generated week(s) did not fit - this template has "
                f"{len(available_rows)} available (blank) teaching week row(s), but "
                f"{len(weeks_json)} weeks were generated. The last {weeks_overflow} week(s) "
                f"were not written into the document. Consider requesting fewer weeks, or "
                f"check whether the template's calendar actually covers your intended course "
                f"length."
            )
        elif len(available_rows) > len(weeks_json):
            leftover = len(available_rows) - len(weeks_json)
            warnings.append(
                f"{leftover} available row(s) in the template's calendar were left blank - "
                f"you generated fewer weeks than the template has room for."
            )

        # Header fields - only touched if a real value was given.
        header_fields_updated = []
        header_fields = _find_header_fields(root)
        start_date = header_updates.get("start_date")
        end_date = header_updates.get("end_date")
        tutor_names = header_updates.get("tutor_names")

        if start_date and header_fields["start_date_cell"] is not None:
            cell = header_fields["start_date_cell"]
            existing_value = cell_text(cell)
            paras = cell.findall(qn("w:p"))
            target_p = paras[-1] if paras else None
            if target_p is not None:
                ensure_para_id(target_p, existing_para_ids, warnings, "Start Date field")
                replace_paragraph_text(target_p, [_format_date_like(start_date, existing_value)])
                header_fields_updated.append("Start Date")
        if end_date and header_fields["end_date_cell"] is not None:
            cell = header_fields["end_date_cell"]
            existing_value = cell_text(cell)
            paras = cell.findall(qn("w:p"))
            target_p = paras[-1] if paras else None
            if target_p is not None:
                ensure_para_id(target_p, existing_para_ids, warnings, "End Date field")
                replace_paragraph_text(target_p, [_format_date_like(end_date, existing_value)])
                header_fields_updated.append("End Date")
        if tutor_names and header_fields["tutor_paragraph"] is not None:
            ensure_para_id(header_fields["tutor_paragraph"], existing_para_ids, warnings, "Tutor(s) field")
            replace_paragraph_text(header_fields["tutor_paragraph"], [tutor_names])
            header_fields_updated.append("Tutor(s)")

        # Course Title / Unit-Project Title - deterministic, not AI-guessed: the app
        # already knows the real course name and unit/content-area title from its own
        # data, so these are corrected directly from that rather than asked of the AI,
        # regardless of whatever placeholder text the template started with (confirmed
        # as a real, standing problem: a template arrived with a stale/wrong course
        # title pre-filled in it).
        course_title = header_updates.get("course_title")
        unit_title = header_updates.get("unit_title")
        unit_outcomes = header_updates.get("unit_outcomes")
        if course_title and header_fields["course_title_paragraph"] is not None:
            ensure_para_id(header_fields["course_title_paragraph"], existing_para_ids, warnings, "Course Title field")
            replace_paragraph_text(header_fields["course_title_paragraph"], [course_title])
            header_fields_updated.append("Course Title")
        if unit_title and header_fields["unit_title_paragraph"] is not None:
            ensure_para_id(header_fields["unit_title_paragraph"], existing_para_ids, warnings, "Unit/Project Title field")
            replace_paragraph_text(header_fields["unit_title_paragraph"], [unit_title])
            header_fields_updated.append("Unit/Project Title")
        if unit_outcomes and header_fields["unit_outcomes_paragraph"] is not None:
            ensure_para_id(header_fields["unit_outcomes_paragraph"], existing_para_ids, warnings, "Unit outcomes field")
            lines = unit_outcomes if isinstance(unit_outcomes, list) else [unit_outcomes]
            replace_paragraph_text(header_fields["unit_outcomes_paragraph"], lines)
            header_fields_updated.append("Unit outcomes")

        # Context/rationale + intended impact - the template stacks both labels in one
        # cell with no per-label answer space, so both are written into the single
        # shared value cell below them, clearly sub-labelled, rather than the document
        # being restructured to give them separate spaces it was never built with.
        context_rationale = header_updates.get("context_rationale")
        intended_impact = header_updates.get("intended_impact")
        value_cell = header_fields["context_impact_value_cell"]
        if (context_rationale or intended_impact) and value_cell is not None:
            paras = value_cell.findall(qn("w:p"))
            target_p = paras[-1] if paras else None
            if target_p is not None:
                ensure_para_id(target_p, existing_para_ids, warnings, "Context/Intended impact field")
                lines = []
                if context_rationale:
                    lines.append(f"Context and rationale: {context_rationale}")
                if context_rationale and intended_impact:
                    lines.append("")
                if intended_impact:
                    lines.append(f"Intended impact: {intended_impact}")
                replace_paragraph_text(target_p, lines)
                header_fields_updated.append("Context/Intended impact")

        # Warn (don't guess) if a requested date range implies the template's own
        # calendar rows may no longer match - grid rows are never added/removed here.
        if end_date and grid_table is not None:
            last_row_cells = rows[-1].findall(qn("w:tc"))
            if last_row_cells:
                last_row_label = cell_text(last_row_cells[0]).strip()
                warnings.append(
                    f"The template's calendar grid still runs through its own last row "
                    f"(\"{last_row_label}\") regardless of the End Date given - rows are "
                    f"never added or removed automatically, since they may encode real "
                    f"fixed institutional dates (half terms, bank holidays, exam windows). "
                    f"If your course genuinely ends earlier or later than this template's "
                    f"calendar, adjust the grid's rows manually."
                )

        assert_paragraph_count_unchanged(root, original_paragraph_count)
        write_document_xml(tree, document_xml_path)
        rezip_docx(unpacked, output_path)

        # Verification: reopen the saved file and confirm the topic text for each filled
        # week actually landed in the right row, rather than trusting no exception = success.
        verified_filled = 0
        try:
            verify_doc = Document(output_path)
            verify_grid = None
            for t in verify_doc.tables:
                if len(t.rows) and any(h in t.rows[0].cells[0].text.lower() for h in WEEK_HEADER_HINTS):
                    if verify_grid is None or len(t.rows) > len(verify_grid.rows):
                        verify_grid = t
            if verify_grid is not None:
                verify_available = [r for r in verify_grid.rows[1:] if not "".join(c.text for c in r.cells[1:]).strip() == "" or True]
                # Re-check against the same available-row list computed before editing,
                # by position, since row identity is stable (no rows added/removed).
                for i in range(min(weeks_filled, len(weeks_json))):
                    week = weeks_json[i]
                    expected_parts = [p for p in _split_title_phase_and_topic(week.get("title", "")) if p]
                    if i < len(available_rows):
                        row_index = rows.index(available_rows[i])
                        actual_text = verify_grid.rows[row_index].cells[1].text
                        if expected_parts and all(part in actual_text for part in expected_parts):
                            verified_filled += 1
                        else:
                            warnings.append(f"Week {week.get('week', i+1)}: wrote successfully but read-back verification didn't find the expected title in the target row - check it manually.")
        except Exception as e:
            warnings.append(f"Could not run read-back verification: {e}")

        report = {
            "weeks_filled": verified_filled,
            "available_rows": len(available_rows),
            "weeks_overflow": weeks_overflow,
            "header_fields_updated": header_fields_updated,
        }
        return report, warnings
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)



def template_calendar(template_path: str, append_eligible_labels: list[str] | None = None) -> list | None:
    """
    The college calendar inside a scheme-of-work template, row by row:
    [{"date": week-commencing date or None, "available": True if the fill writes a scheme week
    into this row, "label": the row's own text for rows it leaves alone, e.g. "Half Term"}].
    Uses the exact same row choice as fill_sow_template_docx, so the scheme's 1st week is the
    1st available row, its 2nd week the 2nd, and so on - induction weeks, half terms and
    closures are the rows in between. Returns None if the template can't be read.
    """
    append_eligible_labels = append_eligible_labels or []
    try:
        work_dir, unpacked = unzip_docx(template_path)
        try:
            tree, _ = parse_document_xml(unpacked)
            grid_table = _find_grid_table(tree.getroot())
            if grid_table is None:
                return None
            out = []
            for row in grid_table.findall(qn("w:tr"))[1:]:
                cells = row.findall(qn("w:tc"))
                available = _row_fill_mode(row, append_eligible_labels) in ("blank", "append")
                label = "" if available else " ".join(" ".join(cell_text(c) for c in cells[1:]).split())[:80]
                out.append({"date": _parse_grid_date(cell_text(cells[0]).strip()) if cells else None,
                            "available": available, "label": label})
            return out
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        return None


def available_week_dates(template_path: str, append_eligible_labels: list[str] | None = None) -> list | None:
    """The week-commencing date of each row the fill writes into, in order (None = no readable date)."""
    rows = template_calendar(template_path, append_eligible_labels)
    return None if rows is None else [r["date"] for r in rows if r["available"]]
