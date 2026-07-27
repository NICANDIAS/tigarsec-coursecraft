"""
Shared low-level primitives for editing an EXISTING .docx's word/document.xml directly -
used by both rubric_fill.py (filling a marking rubric's scoring cells) and sow_docx_fill.py
(filling a scheme-of-work template's blank teaching weeks). Kept in one place so both
callers get the same tested anchoring/formatting-preservation behaviour rather than two
subtly different implementations.

Standing principle throughout: never regenerate a document from scratch, never touch
anything beyond the specific paragraphs being filled, and always verify by reading the
saved file back - see rubric_fill.py's module docstring for the fuller rationale.
"""
import os
import shutil
import secrets
import tempfile
import zipfile

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
NSMAP = {"w": W_NS, "w14": W14_NS}


def qn(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NSMAP[prefix]}}}{local}"


def cell_text(tc) -> str:
    return "".join(t.text or "" for t in tc.iter(qn("w:t")))


def all_existing_para_ids(root) -> set:
    ids = set()
    for p in root.iter(qn("w:p")):
        pid = p.get(qn("w14:paraId"))
        if pid:
            ids.add(pid)
    return ids


def mint_para_id(existing: set) -> str:
    """
    Generates an 8-hex-char paraId in the same format Word uses. The top bit of a paraId
    is reserved by the OOXML spec (a value >= 0x80000000 is invalid) - confirmed via real
    XSD validation catching exactly this - so the value is capped below that.
    """
    while True:
        candidate_int = secrets.randbelow(0x7FFFFFFF) + 1  # 1..0x7FFFFFFF, never 0 or >=0x80000000
        candidate = f"{candidate_int:08X}"
        if candidate not in existing:
            existing.add(candidate)
            return candidate


def ensure_para_id(paragraph, existing_ids: set, warnings: list, label: str) -> str:
    """
    Returns a stable paraId for this paragraph, minting and recording one if it doesn't
    already have a unique one - and warning the caller either way, since a newly-minted
    paraId means this specific anchor didn't exist before this edit.
    """
    para_id = paragraph.get(qn("w14:paraId"))
    if para_id:
        count_with_id = sum(1 for p in paragraph.getroottree().getroot().iter(qn("w:p")) if p.get(qn("w14:paraId")) == para_id)
        if count_with_id != 1:
            warnings.append(f"{label}: existing paraId {para_id} is not unique in this document ({count_with_id} matches) - minting a fresh one instead.")
            para_id = None
    if not para_id:
        para_id = mint_para_id(existing_ids)
        paragraph.set(qn("w14:paraId"), para_id)
        warnings.append(f"{label}: no existing paraId - assigned a new one ({para_id}).")
    return para_id


def clone_rpr_from_paragraph(paragraph):
    """
    Finds formatting to reuse for a new/replacement run: prefers an existing run's rPr in
    this paragraph, falls back to the paragraph mark's own rPr (w:pPr/w:rPr) - what an
    empty paragraph in Word actually stores its "next text will look like this" formatting
    in. Returns None if neither exists, so the new run just inherits the cell/table style
    rather than guessing a format.
    """
    for run in paragraph.findall(qn("w:r")):
        rpr = run.find(qn("w:rPr"))
        if rpr is not None:
            return etree.fromstring(etree.tostring(rpr))
    ppr = paragraph.find(qn("w:pPr"))
    if ppr is not None:
        mark_rpr = ppr.find(qn("w:rPr"))
        if mark_rpr is not None:
            return etree.fromstring(etree.tostring(mark_rpr))
    return None


def build_run(text_lines: list[str], rpr_template):
    """Builds a detached <w:r> element with the given rPr (or none) and one <w:t> per
    line, joined by <w:br/> for anything beyond the first line."""
    scratch = etree.Element("_scratch")
    run = etree.SubElement(scratch, qn("w:r"))
    if rpr_template is not None:
        run.append(etree.fromstring(etree.tostring(rpr_template)))
    for i, line in enumerate(text_lines):
        if i > 0:
            etree.SubElement(run, qn("w:br"))
        t = etree.SubElement(run, qn("w:t"))
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line
    scratch.remove(run)
    return run


def replace_paragraph_text(paragraph, text_lines: list[str]) -> None:
    """
    Replaces a paragraph's existing content with new text, reusing the FIRST existing
    run's formatting (font/bold/colour/etc.) rather than Word/python-docx defaults, and
    removing any other runs in the paragraph (including fragmented runs from spell-check
    splitting a phrase across several <w:r> elements) so nothing stale is left behind.
    Used for cells that already contain a real value being corrected (e.g. a Start Date
    field) - as opposed to build_run, which is for genuinely blank paragraphs being
    filled in for the first time.
    """
    existing_runs = paragraph.findall(qn("w:r"))
    rpr_template = None
    if existing_runs:
        rpr = existing_runs[0].find(qn("w:rPr"))
        if rpr is not None:
            rpr_template = etree.fromstring(etree.tostring(rpr))
    else:
        rpr_template = clone_rpr_from_paragraph(paragraph)

    for r in existing_runs:
        paragraph.remove(r)
    new_run = build_run(text_lines, rpr_template)
    paragraph.append(new_run)


def unzip_docx(original_path: str) -> tuple[str, str]:
    """Unzips to a fresh temp dir (stripping any symlinks, since the input is untrusted),
    returns (work_dir, unpacked_dir)."""
    work_dir = tempfile.mkdtemp(prefix="docx_edit_")
    unpacked = os.path.join(work_dir, "unpacked")
    with zipfile.ZipFile(original_path) as zf:
        zf.extractall(unpacked)
    for dirpath, _, filenames in os.walk(unpacked):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                os.remove(full)
    return work_dir, unpacked


def rezip_docx(unpacked_dir: str, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(unpacked_dir):
            for name in filenames:
                full = os.path.join(dirpath, name)
                arcname = os.path.relpath(full, unpacked_dir)
                zf.write(full, arcname)


def parse_document_xml(unpacked_dir: str):
    document_xml_path = os.path.join(unpacked_dir, "word", "document.xml")
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(document_xml_path, parser)
    return tree, document_xml_path


def write_document_xml(tree, document_xml_path: str) -> None:
    tree.write(document_xml_path, xml_declaration=True, encoding="UTF-8", standalone=True)


def assert_paragraph_count_unchanged(root, original_count: int) -> None:
    new_count = len(list(root.iter(qn("w:p"))))
    if new_count != original_count:
        raise RuntimeError(
            f"Refusing to save: paragraph count changed ({original_count} -> {new_count}). "
            f"This should never happen from editing text inside existing paragraphs, so "
            f"something went wrong - the original file was not modified."
        )
