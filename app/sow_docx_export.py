"""
Exports a generated scheme of work as a downloadable table-formatted Word document.

This is deliberately a GENERAL table shape (Week/Date | Learning Outcomes & Topic |
Key Content/Activities | Assessment), not an exact replica of any specific college's
template - matching an arbitrary uploaded template's exact columns/branding/fonts is a much
bigger problem than this solves. What this gives you is a real, submittable document
instead of only an in-app view, in a shape close to what most college SOW templates use.
"""
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH


def render_sow_docx(sow, course_name: str, output_path: str):
    doc = Document()

    title = doc.add_heading(sow.title, level=1)
    if sow.content_area:
        p = doc.add_paragraph(f"Content area: {sow.content_area}")
        p.runs[0].italic = True
    if sow.start_date and sow.end_date:
        doc.add_paragraph(f"{sow.start_date.strftime('%d/%m/%Y')} to {sow.end_date.strftime('%d/%m/%Y')}")
    doc.add_paragraph(f"Course: {course_name}")

    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    header_cells = table.rows[0].cells
    headers = ["Week", "Learning Outcomes & Topic", "Key Content / Activities", "Assessment"]
    for cell, text in zip(header_cells, headers):
        cell.text = text
        cell.paragraphs[0].runs[0].bold = True

    weeks = sow.weeks_json or []
    for week in weeks:
        row_cells = table.add_row().cells

        row_cells[0].text = f"Week {week.get('week', '')}"

        los = week.get("learning_outcomes", [])
        topic_text = week.get("title", "")
        if los:
            topic_text += "\n(" + ", ".join(los) + ")"
        row_cells[1].text = topic_text

        content_lines = list(week.get("activities", []))
        if week.get("practical"):
            content_lines.append(f"Practical: {week['practical']}")
        if week.get("notes"):
            content_lines.append(f"Notes: {week['notes']}")
        row_cells[2].text = "\n".join(f"- {line}" for line in content_lines)

        assignment = week.get("assignment")
        if assignment:
            assess_text = f"{assignment.get('title', '')}\n{assignment.get('brief', '')}"
            covered = assignment.get("criteria_covered", [])
            if covered:
                assess_text += f"\nCovers: {', '.join(covered)}"
            row_cells[3].text = assess_text
        else:
            row_cells[3].text = ""

    # Reasonable readable sizing rather than Word's cramped default column widths
    widths = [Cm(2), Cm(5), Cm(6.5), Cm(4.5)]
    for row in table.rows:
        for cell, width in zip(row.cells, widths):
            cell.width = width

    doc.save(output_path)
