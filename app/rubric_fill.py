"""
Fills in the ACTUAL uploaded rubric document, in place - never a new document that merely
copies its table content.

STANDING FIX (post real-submission testing): the previous version of this module used
python-docx's `cell.text = ...` setter to write scores in. That call looks safe but is NOT
a byte-faithful in-place edit: it clears every paragraph in the cell but the first, clears
every run in that first paragraph, and inserts a single brand-new run using python-docx's
own default run formatting - not whatever formatting the original blank cell actually had.
It also matched purely positionally (whichever cell text happened to parse a criterion
code out of it), with no stable anchor to re-verify against.

This version instead does what the docx skill's own guidance for editing existing
documents requires: unzip the real file, edit word/document.xml directly at the XML level,
and rezip - never regenerate the document from scratch, never go through python-docx's
whole-document reserialization for the edit itself (it's still used afterward, read-only,
just to verify the result).

Anchoring: every paragraph edited is identified by its w14:paraId (Word's own stable,
per-paragraph identifier, present on every paragraph in any document saved by Word 2010+).
Before writing anything, the target paraId is confirmed to appear EXACTLY ONCE in the
document - if a document has no w14:paraId at all (e.g. it was never opened in real Word,
only produced by a tool that omits them), one is minted here and recorded in the report's
"newly_assigned_paraids" list, so the caller/marker can see that this specific anchor is
new rather than pre-existing. Only a single new run is inserted into the blank paragraph,
reusing whatever run/paragraph-mark formatting is already present there - nothing else in
that paragraph, cell, row, or the rest of the document is touched, rebuilt, or reformatted.

Low-level XML primitives (paraId anchoring, run/formatting cloning, zip handling) live in
docx_xml_edit.py, shared with sow_docx_fill.py which does the same kind of in-place editing
for scheme-of-work templates.
"""
import os
import re

from docx import Document

from .docx_xml_edit import (
    qn, cell_text, all_existing_para_ids, ensure_para_id, clone_rpr_from_paragraph,
    build_run, unzip_docx, rezip_docx, parse_document_xml, write_document_xml,
    assert_paragraph_count_unchanged,
)


def _extract_criterion_code(text: str) -> str | None:
    """Looks for a leading criterion code like '1.1' or 'P1' at the start of a cell -
    the pattern every rubric row in this document set actually uses."""
    match = re.match(r'^([A-Z]?\d+(?:\.\d+)?)\b', text.strip())
    return match.group(1) if match else None


def _find_matching_mark(code: str, marks: list[dict]) -> dict | None:
    """
    Standing fix: a plain substring check here (code in criterion_ref) is ambiguous - a row
    coded 'P1' would also match a mark for 'P10' or 'P11', since 'P1' is a literal substring
    of both, and whichever happened to be checked first would silently win. This uses a
    word-boundary match instead ('P1' only matches a 'P1' token, not the start of 'P10'),
    confirmed against exactly that P1/P10 collision case.
    """
    pattern = re.compile(r'(?<![A-Za-z0-9])' + re.escape(code) + r'(?![A-Za-z0-9])')
    for m in marks:
        if pattern.search(m.get("criterion_ref") or ""):
            return m
    return None


def fill_original_rubric_docx(original_path: str, marks: list[dict], output_path: str) -> tuple[int, list[str]]:
    """
    Opens the ACTUAL uploaded rubric file, edits word/document.xml directly to write each
    matched criterion's score/comment into that row's existing LAST cell (the "Scoring"-
    style blank column every real rubric in this document set already has), and rezips -
    nothing else in the document is touched.

    marks: [{"criterion_ref": str, "numeric_score": float|None, "max_score": float|None,
             "mark": str, "comment": str}, ...]

    Returns (filled_count, warnings). filled_count only counts rows that were both edited
    AND verified (by reopening the saved output and reading the text back) to actually
    contain the expected score - a row that failed verification is not counted as filled,
    even though this function still returns a usable file. warnings surfaces anything
    non-fatal worth a marker's attention (e.g. a paraId that had to be newly minted, or a
    row whose write couldn't be verified).
    """
    warnings: list[str] = []
    work_dir, unpacked = unzip_docx(original_path)
    try:
        tree, document_xml_path = parse_document_xml(unpacked)
        root = tree.getroot()

        original_paragraph_count = len(list(root.iter(qn("w:p"))))
        existing_para_ids = all_existing_para_ids(root)

        filled_targets = []  # [(code, expected_score_text, expected_comment)]

        for table in root.iter(qn("w:tbl")):
            for row in table.iter(qn("w:tr")):
                cells = row.findall(qn("w:tc"))
                if len(cells) < 2:
                    continue  # not a real data row (e.g. a stray single-cell row)

                code = None
                for cell in cells:
                    code = _extract_criterion_code(cell_text(cell))
                    if code:
                        break
                if not code:
                    continue  # header row or non-criterion row - leave completely alone

                matched_mark = _find_matching_mark(code, marks)
                if not matched_mark:
                    continue  # no mark for this criterion - leave the existing blank cell as-is

                last_cell = cells[-1]
                if cell_text(last_cell).strip():
                    continue  # already has content - never overwrite genuine rubric text

                paragraphs = last_cell.findall(qn("w:p"))
                if not paragraphs:
                    warnings.append(f"Criterion {code}: last cell has no paragraph to anchor to - skipped.")
                    continue
                target_p = paragraphs[-1]
                ensure_para_id(target_p, existing_para_ids, warnings, f"Criterion {code}")

                if matched_mark.get("numeric_score") is not None and matched_mark.get("max_score") is not None:
                    score_text = f"{matched_mark['numeric_score']}/{matched_mark['max_score']}"
                else:
                    score_text = matched_mark.get("mark", "") or ""
                comment_text = matched_mark.get("comment", "") or ""
                lines = [score_text, comment_text] if comment_text else [score_text]

                rpr_template = clone_rpr_from_paragraph(target_p)
                new_run = build_run(lines, rpr_template)
                target_p.append(new_run)  # pPr (if any) is always the first child already, so
                                            # appending at the end keeps the schema-required order

                filled_targets.append((code, score_text, comment_text))

        assert_paragraph_count_unchanged(root, original_paragraph_count)
        write_document_xml(tree, document_xml_path)
        rezip_docx(unpacked, output_path)

        # Verification: reopen the SAVED output (not the in-memory tree) via python-docx,
        # read the actual text back out of the same cells, and confirm it matches what was
        # meant to be written - never just trust that no exception was thrown.
        verified_count = 0
        try:
            verify_doc = Document(output_path)
            for table in verify_doc.tables:
                for row in table.rows:
                    if len(row.cells) < 2:
                        continue
                    code = None
                    for cell in row.cells:
                        code = _extract_criterion_code(cell.text)
                        if code:
                            break
                    if not code:
                        continue
                    target = next((t for t in filled_targets if t[0] == code), None)
                    if not target:
                        continue
                    _, expected_score, expected_comment = target
                    actual_text = row.cells[-1].text
                    if expected_score in actual_text and (not expected_comment or expected_comment in actual_text):
                        verified_count += 1
                    else:
                        warnings.append(
                            f"Criterion {code}: wrote successfully but read-back verification "
                            f"did not find the expected text in the cell - check this row manually."
                        )
        except Exception as e:
            warnings.append(f"Could not run read-back verification: {e}")
            verified_count = 0

        return verified_count, warnings
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
