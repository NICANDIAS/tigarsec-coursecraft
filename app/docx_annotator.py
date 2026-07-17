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


def extract_text_from_pdf(file_path: str) -> str:
    reader = PdfReader(file_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text(file_path: str, filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".docx"):
        return extract_text_from_docx(file_path)
    elif lower.endswith(".pdf"):
        return extract_text_from_pdf(file_path)
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
