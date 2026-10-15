"""
Document annotation service.

CORE RULE (see architecture doc section 6): never regenerate the document body.
This module only ever ADDS Word comments to the ORIGINAL file - paragraph text,
formatting, justification, images, tables are never touched.

v1 anchoring is simplified: a comment is anchored to the first run of whichever
paragraph contains the AI's quoted evidence text. This is a known limitation -
if evidence spans multiple paragraphs or the exact substring doesn't match
(e.g. due to smart quotes), the comment falls back into a "general feedback"
note at the end of the document rather than being silently dropped.

TEST THIS on real, messy submissions (tables, tracked changes already present,
non-standard formatting) before trusting it on a full class set - flagged in
the architecture doc as the highest-risk component.
"""
from docx import Document
from pypdf import PdfReader


def extract_text_from_docx(file_path: str) -> str:
    """
    Extracts BOTH paragraphs and tables, in their true document order - a plain
    paragraph-only extraction (the previous version) completely misses tables, which is
    exactly where many real college templates put their actual week-by-week structure
    (confirmed on a real uploaded SOW template: a 42-row week/date/content/assessment grid
    that was entirely invisible to the AI before this fix, despite being the whole point
    of the document).
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(file_path)
    parts = []

    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            if para.text.strip():
                parts.append(para.text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            table_lines = []
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    table_lines.append(" | ".join(cells))
            if table_lines:
                parts.append("[TABLE]\n" + "\n".join(table_lines) + "\n[/TABLE]")

    return "\n".join(parts)


def _format_table_markdown(table: list) -> str:
    """Formats a pdfplumber-extracted table (list of row lists) as a clean
    pipe-separated table, so row/column associations are unambiguous regardless of how
    the source PDF happened to draw the cells internally."""
    if not table:
        return ""
    lines = []
    for row in table:
        cells = [(c or "").strip().replace("\n", " ") for c in row]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _get_vector_checkboxes_by_page(pdf_path: str) -> dict:
    """
    Fallback checkbox detection for documents with NO AcroForm fields at all - confirmed
    as a real, non-hypothetical case: a document with visibly checked/unchecked boxes
    that turned out to have zero form fields and zero annotations on inspection. The
    boxes there are drawn entirely as vector graphics: a small rounded-square rect
    (present for every box regardless of state) plus one extra curve drawn inside ONLY
    when checked (the tick mark). Verified against that real document's known states -
    16 for 16 correct across both pages, including the page where every box was
    unchecked.

    Deliberately conservative to avoid false positives on unrelated small square
    graphics elsewhere in a totally different document (a bullet point, a decorative
    element): only activates when there are at least 4 same-sized, roughly-square rects
    on a page - a real checkbox list/table has several in a grid; a one-off decorative
    square does not. This is a heuristic tuned to one confirmed real rendering style,
    not a universal solution - a checkbox drawn as an image, or in a font glyph, or
    with a different vector pattern, would need separate handling if it turns up.
    """
    result = {}
    try:
        import pdfplumber
    except Exception:
        return result

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            small_square_rects = [
                r for r in page.rects
                if 4 <= (r["x1"] - r["x0"]) <= 20
                and abs((r["x1"] - r["x0"]) - (r["bottom"] - r["top"])) < 2  # roughly square
            ]
            if len(small_square_rects) < 4:
                continue  # not enough to plausibly be a checkbox grid - avoid false positives

            checkboxes = []
            for r in small_square_rects:
                nearby_curves = [
                    c for c in page.curves
                    if abs(c["x0"] - r["x0"]) < 15 and abs(c["top"] - r["top"]) < 15
                ]
                is_checked = len(nearby_curves) >= 2
                # Convert pdfplumber's top-down (top/bottom) rect into the same PDF
                # bottom-up (x0, y0, x1, y1) convention _nearest_checkbox_label expects,
                # so that function can be reused unchanged regardless of which detection
                # method found the checkbox.
                rect_pdf_coords = (r["x0"], page.height - r["bottom"], r["x1"], page.height - r["top"])
                checkboxes.append((rect_pdf_coords, is_checked))
            if checkboxes:
                result[page_idx] = checkboxes
    return result


def _get_checkbox_fields_by_page(pdf_path: str) -> dict:
    """
    Returns {page_index: [(rect, is_checked), ...]} for every checkbox WIDGET (form field)
    on the page. Standing fix: checkbox state lives in AcroForm field data (a /V value),
    completely separate from the page's visible text content - confirmed by direct
    testing that neither pypdf's nor pdfplumber's text extraction ever sees it. A
    checked/unchecked box that was never extracted at all can make it look like a
    student selected nothing when they genuinely selected several things.
    """
    reader = PdfReader(pdf_path)
    by_page = {}
    for page_idx, page in enumerate(reader.pages):
        annots = page.get("/Annots")
        if not annots:
            continue
        checkboxes = []
        for annot_ref in annots:
            annot = annot_ref.get_object()
            if annot.get("/FT") != "/Btn":
                continue
            rect = annot.get("/Rect")
            if not rect:
                continue
            state = annot.get("/V") or annot.get("/AS")
            is_checked = state is not None and str(state) not in ("/Off", "Off")
            checkboxes.append((tuple(float(x) for x in rect), is_checked))
        if checkboxes:
            by_page[page_idx] = checkboxes
    return by_page


def _nearest_checkbox_label(checkbox_rect, words, page_height, other_checkbox_x_positions) -> str:
    """
    Finds the label for one checkbox: words to its right, roughly the same row, gathered
    across up to two lines (a label can wrap - e.g. "Maintenance staff" splitting onto two
    lines, confirmed as a real case worth handling, not a hypothetical one). Stops at a
    horizontal gap or the x-position of the NEXT checkbox in the row, so one label never
    bleeds into the next column's.
    """
    x0, y0, x1, y1 = checkbox_rect
    cb_top = page_height - y1  # PDF y-axis is bottom-up; pdfplumber word positions are top-down
    cb_bottom = page_height - y0
    row_height = cb_bottom - cb_top

    same_row_x = [cx for cx in other_checkbox_x_positions if cx > x1 + 2]
    right_boundary = min(same_row_x) if same_row_x else float("inf")

    candidates = []
    for w in words:
        if w["x0"] < x1 - 2 or w["x0"] >= right_boundary:
            continue
        if w["top"] < cb_top - 2 or w["top"] > cb_bottom + row_height * 2.2:
            continue
        candidates.append(w)

    if not candidates:
        return ""
    candidates.sort(key=lambda w: (round(w["top"]), w["x0"]))
    return " ".join(w["text"] for w in candidates)


def _extract_checkbox_summary(page, checkboxes: list) -> str:
    """One page's checkbox states as a clear "☑ Label | ☐ Label" summary, using the same
    label-association logic tested against both simple and wrapped-label layouts."""
    if not checkboxes:
        return ""
    words = page.extract_words()
    x_positions = [rect[0] for rect, _ in checkboxes]
    entries = []
    for rect, is_checked in checkboxes:
        label = _nearest_checkbox_label(rect, words, page.height, x_positions)
        symbol = "☑" if is_checked else "☐"
        entries.append(f"{symbol} {label}".strip())
    return " | ".join(entries)


def _extract_pdf_page_text(page, checkboxes: list | None = None, include_table_copies: bool = True) -> str:
    """
    One page's text via pdfplumber - genuinely more robust than pypdf's plain
    extract_text() for anything with unusual internal text positioning. Confirmed by
    direct testing against real failure modes, not assumed:
    - A styled/kerned heading with individually-positioned words came out scrambled
      one-word-per-line via pypdf; pdfplumber reconstructs it as one correct line,
      because it groups text by actual Y/X position, not PDF drawing order.
    - A hazard/risk/control-measure table drawn column-by-column internally (a real
      pattern, not just theoretical) had pypdf list all hazards, then all risks, then
      all controls, completely destroying which risk belongs to which hazard.
      pdfplumber's extract_text() alone already reconstructs it row-by-row correctly,
      for the same reason (position-based, not draw-order-based).
    Any DETECTED TABLE (via ruling lines - matches real bordered risk-assessment-style
    tables) is additionally reconstructed as a clean pipe-separated block and appended,
    clearly labelled - a belt-and-braces supplement for edge cases like wrapped cell
    text or very tight column spacing, not a replacement for the text above it.
    Any CHECKBOX form fields on the page (a real, confirmed case: a "Persons at risk"
    style checklist where the checked/unchecked state lives in AcroForm field data, not
    in the page's text content at all - neither extraction method above would ever see
    it otherwise) get a clear "☑ Label | ☐ Label" summary appended too.
    """
    text = (page.extract_text() or "").strip()
    # include_table_copies=False is used ONLY for spec uploads: a spec parser reads every
    # line, so a second copy of each table made it read every criterion twice (confirmed:
    # T Level DSS came out with 966 "criteria" instead of ~490). Marking, IV and SOW
    # templates keep the copies (default True) - unchanged behaviour for them.
    try:
        tables = page.extract_tables() if include_table_copies else []
    except Exception:
        tables = []
    table_blocks = []
    for i, table in enumerate(tables, start=1):
        formatted = _format_table_markdown(table)
        if formatted:
            label = f"[Table {i} on this page, reconstructed from the document's actual grid for accuracy]"
            table_blocks.append(f"{label}\n{formatted}")
    if table_blocks:
        text = (text + "\n\n" + "\n\n".join(table_blocks)).strip()

    if checkboxes:
        try:
            summary = _extract_checkbox_summary(page, checkboxes)
        except Exception:
            summary = ""
        if summary:
            text = (text + "\n\n[Checkbox selections detected on this page - ☑ = checked, ☐ = unchecked]\n" + summary).strip()

    return text


def extract_text_from_pdf(file_path: str, include_table_copies: bool = True) -> str:
    try:
        import pdfplumber
        checkboxes_by_page = _get_checkbox_fields_by_page(file_path)
        if not checkboxes_by_page:
            # No AcroForm checkboxes anywhere in the file - try the vector-drawn
            # fallback before concluding there simply aren't any (confirmed as a real,
            # common case: a document with visible checkboxes but zero form fields).
            checkboxes_by_page = _get_vector_checkboxes_by_page(file_path)
        with pdfplumber.open(file_path) as pdf:
            return "\n".join(
                _extract_pdf_page_text(page, checkboxes_by_page.get(i), include_table_copies=include_table_copies)
                for i, page in enumerate(pdf.pages)
            )
    except Exception:
        # Defensive fallback - pdfplumber (pdfminer.six underneath) is generally robust,
        # but a small number of malformed/unusual PDFs might trip it up where pypdf's
        # simpler parser still succeeds. An extraction-quality improvement must never
        # become a NEW way for extraction to fail outright on a file that used to work.
        reader = PdfReader(file_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)


def pdf_page_count(file_path: str) -> int:
    try:
        return len(PdfReader(file_path).pages)
    except Exception:
        return 0


def extract_text_from_pdf_pages(file_path: str, first: int, last: int) -> str:
    """Text of pages first..last (1-based, inclusive) - used by the spec "read these pages"
    fix tool. Same extraction as a spec upload (no duplicate table copies)."""
    import pdfplumber
    with pdfplumber.open(file_path) as pdf:
        first = max(1, first)
        last = min(len(pdf.pages), last)
        return "\n".join(_extract_pdf_page_text(pdf.pages[i], None, include_table_copies=False) for i in range(first - 1, last))


def extract_text(file_path: str, filename: str, include_table_copies: bool = True) -> str:
    """include_table_copies only affects PDFs - see _extract_pdf_page_text. Spec uploads
    pass False; every other caller keeps the default (unchanged behaviour)."""
    lower = filename.lower()
    if lower.endswith(".docx"):
        return extract_text_from_docx(file_path)
    elif lower.endswith(".pdf"):
        return extract_text_from_pdf(file_path, include_table_copies=include_table_copies)
    elif lower.endswith(".txt"):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    else:
        raise ValueError(f"Unsupported file type for extraction: {filename}")


def annotate_submission(
    file_path: str,
    marks: list[dict],
    output_path: str,
    author: str = "TIGARSEC CourseCraft (AI)",
) -> list[str]:
    """
    marks: list of dicts with keys "evidence_text" and "comment".
    Only works on .docx (annotation requires an OOXML document - PDFs would need
    a different, image-overlay-based approach; not built for v1).

    Returns a list of comments that could NOT be anchored (for logging/QA).
    """
    doc = Document(file_path)
    unmatched = []

    for mark in marks:
        evidence = (mark.get("evidence_text") or "").strip()
        comment_text = mark.get("comment") or ""
        if not comment_text:
            continue

        anchored = False
        if evidence:
            needle = evidence[:80]  # short prefix improves match tolerance
            for paragraph in doc.paragraphs:
                if needle and needle in paragraph.text and paragraph.runs:
                    try:
                        doc.add_comment(
                            runs=[paragraph.runs[0]],
                            text=comment_text,
                            author=author,
                            initials="AI",
                        )
                        anchored = True
                    except Exception:
                        anchored = False
                    break

        if not anchored:
            unmatched.append(comment_text)

    if unmatched and doc.paragraphs:
        summary = "General AI feedback (could not anchor to a specific line):\n" + "\n".join(
            f"- {c}" for c in unmatched
        )
        last_para = doc.paragraphs[-1]
        if last_para.runs:
            doc.add_comment(runs=[last_para.runs[0]], text=summary, author=author, initials="AI")

    doc.save(output_path)
    return unmatched


# ---------------------------------------------------------------------------
# Location-tagged extraction for multi-file submission marking. Kept SEPARATE
# from extract_text() above (used by spec parsing and SOW format samples) -
# inserting "[Slide N]"/"[Page N]" style tags into spec text would risk
# reintroducing the exact kind of text contamination that was just fixed for
# spec parsing. This is only used for student submissions, where knowing WHICH
# file and WHERE evidence came from is the actual point.
# ---------------------------------------------------------------------------

def extract_text_from_pdf_with_pages(file_path: str) -> str:
    """Same as extract_text_from_pdf but tags each page, so marking can say
    which page evidence came from (useful when a PDF has one question per page)."""
    try:
        import pdfplumber
        checkboxes_by_page = _get_checkbox_fields_by_page(file_path)
        if not checkboxes_by_page:
            checkboxes_by_page = _get_vector_checkboxes_by_page(file_path)
        parts = []
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                page_text = _extract_pdf_page_text(page, checkboxes_by_page.get(i))
                if page_text:
                    parts.append(f"[Page {i + 1}]\n{page_text}")
        return "\n\n".join(parts)
    except Exception:
        reader = PdfReader(file_path)
        parts = []
        for i, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                parts.append(f"[Page {i}]\n{page_text}")
        return "\n\n".join(parts)


def extract_text_from_pptx(file_path: str) -> str:
    """Extracts text per slide, tagged with slide number - lets marking say
    'evidence found on Slide 2' rather than just 'somewhere in the presentation'."""
    from pptx import Presentation
    prs = Presentation(file_path)
    parts = []
    for i, slide in enumerate(prs.slides, start=1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            if shape.has_table:
                for row in shape.table.rows:
                    texts.append(" | ".join(cell.text.strip() for cell in row.cells))
        if texts:
            parts.append(f"[Slide {i}]\n" + "\n".join(texts))
    return "\n\n".join(parts)


def extract_text_from_xlsx(file_path: str) -> str:
    """Extracts non-empty cell values per sheet/row, tagged with sheet name and
    row number - lets marking say 'evidence found in Sheet1, Row 12' for
    calculations/graphs/data submitted as a spreadsheet."""
    import openpyxl
    wb = openpyxl.load_workbook(file_path, data_only=True)
    parts = []
    for sheet in wb.worksheets:
        for row_idx, row in enumerate(sheet.iter_rows(), start=1):
            values = [str(cell.value).strip() for cell in row if cell.value is not None and str(cell.value).strip()]
            if values:
                parts.append(f"[Sheet: {sheet.title}, Row {row_idx}] " + " | ".join(values))
    return "\n".join(parts)


def extract_text_with_locations(file_path: str, filename: str) -> str:
    """
    The main entry point for multi-file submission marking. Returns text tagged
    with page/slide/sheet-row markers where the format supports it, so the AI
    can cite exactly where in a file it found evidence for a criterion.
    """
    lower = filename.lower()
    if lower.endswith(".docx"):
        return extract_text_from_docx(file_path)  # no reliable location markers available for docx
    elif lower.endswith(".pdf"):
        return extract_text_from_pdf_with_pages(file_path)
    elif lower.endswith(".pptx"):
        return extract_text_from_pptx(file_path)
    elif lower.endswith(".xlsx"):
        return extract_text_from_xlsx(file_path)
    elif lower.endswith(".txt"):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    else:
        raise ValueError(f"Unsupported file type for extraction: {filename}")
