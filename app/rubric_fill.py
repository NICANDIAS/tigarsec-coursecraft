"""
Fills in the ACTUAL uploaded rubric document, in place - not a new document that merely
copies its table content. Opens the real .docx you uploaded, finds each row's blank
"Scoring"-style last column, writes the awarded score/comment directly into that existing
cell, and saves the SAME document back out - every other slide, paragraph, heading,
formatting choice, and non-matching row is completely untouched, because nothing is ever
rebuilt from scratch.

Only works for .docx rubrics, since a docx's tables are real structured objects (rows/
cells) that can be read and written to directly. PDF/text-sourced rubrics don't offer that
same reliable in-place editing.
"""
import re
from docx import Document


def _extract_criterion_code(cell_text: str) -> str | None:
    """Looks for a leading criterion code like '1.1' or 'P1' at the start of a cell -
    the pattern every rubric row in this document set actually uses."""
    match = re.match(r'^([A-Z]?\d+(?:\.\d+)?)\b', cell_text.strip())
    return match.group(1) if match else None


def _find_matching_mark(code: str, marks: list[dict]) -> dict | None:
    for m in marks:
        if code in (m.get("criterion_ref") or ""):
            return m
    return None


def fill_original_rubric_docx(original_path: str, marks: list[dict], output_path: str) -> int:
    """
    Opens the ACTUAL uploaded rubric file, writes each matched criterion's score/comment
    directly into that row's existing LAST cell (the "Scoring"-style blank column every
    real rubric in this document set already has), and saves the result - nothing else in
    the document is touched. Returns how many rows were actually filled, so the caller can
    tell whether this rubric's shape matched what was expected.

    marks: [{"criterion_ref": str, "numeric_score": float|None, "max_score": float|None,
             "mark": str, "comment": str}, ...]
    """
    doc = Document(original_path)
    filled_count = 0

    for table in doc.tables:
        for row in table.rows:
            if len(row.cells) < 2:
                continue  # not a real data row (e.g. a stray single-cell row)
            code = None
            for cell in row.cells:
                code = _extract_criterion_code(cell.text)
                if code:
                    break
            if not code:
                continue  # header row or non-criterion row - leave completely alone

            matched_mark = _find_matching_mark(code, marks)
            if not matched_mark:
                continue  # no mark for this criterion - leave the existing blank cell as-is

            if matched_mark.get("numeric_score") is not None and matched_mark.get("max_score") is not None:
                score_text = f"{matched_mark['numeric_score']}/{matched_mark['max_score']}"
            else:
                score_text = matched_mark.get("mark", "")
            comment_text = matched_mark.get("comment", "")

            last_cell = row.cells[-1]
            # Only ever writes into a cell that was already blank or already the target
            # column - never overwrites genuine rubric content (band descriptions etc.)
            if last_cell.text.strip():
                continue
            last_cell.text = f"{score_text}\n{comment_text}" if comment_text else score_text
            filled_count += 1

    doc.save(output_path)
    return filled_count
