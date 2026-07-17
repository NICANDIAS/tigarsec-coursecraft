"""
Renders the printable resources a lesson's activities/practicals actually need (sample
data tables, cards to cut up, scenario briefs, worksheets) into a downloadable Word
document - generated fresh from the deck's already-stored outline_json each time it's
requested, so it stays available (and re-downloadable) any time after the PowerPoint
itself was generated, without needing to regenerate anything.
"""
from docx import Document


def extract_resources(outline_json: dict) -> list[dict]:
    """Pulls out every slide that actually has a resource, in slide order."""
    resources = []
    for slide in outline_json.get("slides", []):
        resource = slide.get("resource")
        if resource and resource.get("needed"):
            resources.append({
                "slide_title": slide.get("title", ""),
                "resource_title": resource.get("title", "Resource"),
                "content": resource.get("content", ""),
            })
    return resources


def render_resources_docx(outline_json: dict, lesson_title: str, output_path: str) -> int:
    """Returns the number of resources written, so the caller can decide whether there's
    anything worth offering as a download at all."""
    resources = extract_resources(outline_json)
    if not resources:
        return 0

    doc = Document()
    doc.add_heading(f"Activity Resources: {lesson_title}", level=1)
    doc.add_paragraph(
        "Print and hand out as needed for the activities/practicals in this lesson - "
        "generated to match the PowerPoint exactly."
    )

    for i, res in enumerate(resources):
        if i > 0:
            doc.add_page_break()
        doc.add_heading(res["resource_title"], level=2)
        italic_para = doc.add_paragraph()
        italic_run = italic_para.add_run(f"For: {res['slide_title']}")
        italic_run.italic = True
        for line in res["content"].split("\n"):
            doc.add_paragraph(line)

    doc.save(output_path)
    return len(resources)
