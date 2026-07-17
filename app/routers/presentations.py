import os
import json
import uuid
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, Course, LessonDeck, LessonImage, SchemeOfWork
from ..auth import get_current_teacher
from ..pptx_generator import generate_lesson_outline, render_lesson_pptx, test_unsplash_connection, resolve_slide_image, pick_palette
from ..resource_export import render_resources_docx, extract_resources

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "storage/uploads"
PPTX_DIR = "storage/presentations"
IMAGE_CACHE_DIR = "storage/lesson_images"


@router.get("/courses/{course_id}/presentations/new", response_class=HTMLResponse)
def new_presentation_page(
    course_id: int,
    request: Request,
    sow_id: int = 0,
    week: int = 0,
    from_deck_id: int = 0,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    units = []
    if course and course.spec_version and course.spec_version.structured_json:
        units = course.spec_version.structured_json.get("units", [])

    # If a scheme of work exists, offer its weeks as a quick way to supply "what was
    # covered last lesson" context - much more useful than a blank text box.
    sow_weeks = []
    if course and course.schemes_of_work:
        sow_weeks = course.schemes_of_work[0].weeks_json

    # Prefill from a specific SOW week if linked here from the scheme-of-work page -
    # matches the week's content back to a unit + specific learning outcomes, and pulls
    # the previous week's activities as ready-made "what was covered last lesson" context.
    prefill = None
    if sow_id and week:
        target_sow = db.query(SchemeOfWork).filter(
            SchemeOfWork.id == sow_id, SchemeOfWork.course_id == course_id
        ).first()
        if target_sow and target_sow.weeks_json:
            week_entry = next((w for w in target_sow.weeks_json if w.get("week") == week), None)
            if week_entry:
                week_lo_codes = set(week_entry.get("learning_outcomes", []))
                unit_index = None
                lo_indices = []
                for u_idx, unit in enumerate(units):
                    matching = [
                        lo_idx for lo_idx, lo in enumerate(unit.get("learning_outcomes", []))
                        if lo.get("code") in week_lo_codes
                    ]
                    if matching:
                        unit_index = u_idx
                        lo_indices = matching
                        break

                previous_week_entry = next((w for w in target_sow.weeks_json if w.get("week") == week - 1), None)
                previous_context = ""
                if previous_week_entry:
                    previous_context = (
                        f"Week {previous_week_entry.get('week')}: {previous_week_entry.get('title', '')} - covered: "
                        f"{', '.join(previous_week_entry.get('learning_outcomes', []))}"
                    )

                prefill = {
                    "title": week_entry.get("title", ""),
                    "unit_index": unit_index,
                    "lo_indices": lo_indices,
                    "previous_context": previous_context,
                    "source_sow_id": sow_id,
                    "source_week": week,
                }

    # Prefill a FOLLOW-UP lesson from a deck that didn't fit in its requested time - reuses
    # the exact same unit/learning-outcome selection, and folds the original deck's own
    # scope_note into the retrieval-starter context so the AI knows what was already
    # covered and can focus the new content on what's left, rather than starting blind.
    elif from_deck_id:
        source_deck = db.query(LessonDeck).filter(
            LessonDeck.id == from_deck_id, LessonDeck.course_id == course_id
        ).first()
        if source_deck and source_deck.unit_index is not None:
            previous_context = f"Previous lesson: {source_deck.title}."
            if source_deck.scope_note:
                previous_context += f" {source_deck.scope_note}"
            prefill = {
                "title": f"{source_deck.title} (Part 2)",
                "unit_index": source_deck.unit_index,
                "lo_indices": source_deck.selected_lo_indices or [],
                "previous_context": previous_context,
            }

    return templates.TemplateResponse(
        "presentation_new.html",
        {"request": request, "teacher": teacher, "course": course, "units": units,
         "sow_weeks": sow_weeks, "prefill": prefill},
    )


def _unit_content_text(unit: dict, selected_lo_indices: list[int] | None = None) -> str:
    """Flattens a unit's LOs/criteria into plain text for the LLM prompt. If
    selected_lo_indices is given (non-empty), scopes to just those LOs - this is what lets
    a single lesson target specific topics within a content area instead of the whole unit,
    which usually spans months of teaching."""
    all_los = unit.get("learning_outcomes", [])
    los_to_use = (
        [all_los[i] for i in selected_lo_indices if 0 <= i < len(all_los)]
        if selected_lo_indices else all_los
    )
    lines = [f"Unit: {unit.get('name')}"]
    for lo in los_to_use:
        lines.append(f"\n{lo.get('code')} - {lo.get('title')}")
        for c in lo.get("criteria", []):
            lines.append(f"  {c.get('code')}: {c.get('description')}")
    return "\n".join(lines)


@router.post("/courses/{course_id}/presentations/generate")
async def generate_presentation(
    course_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    unit_index: int = Form(...),
    title: str = Form(...),
    duration_minutes: int = Form(60),
    previous_lesson_context: str = Form(""),
    include_practicals: str = Form(""),
    source_sow_id: int = Form(0),
    source_week: int = Form(0),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    if not course or not course.spec_version or not course.spec_version.structured_json:
        return RedirectResponse(url=f"/courses/{course_id}", status_code=303)

    units = course.spec_version.structured_json.get("units", [])
    if unit_index < 0 or unit_index >= len(units):
        return RedirectResponse(url=f"/courses/{course_id}/presentations/new", status_code=303)

    unit = units[unit_index]

    form = await request.form()
    selected_lo_indices = [int(v) for v in form.getlist("selected_los") if v.isdigit()]
    topic_label = unit.get("name", "Untitled unit")
    if selected_lo_indices:
        all_los = unit.get("learning_outcomes", [])
        picked_titles = [all_los[i].get("title", all_los[i].get("code", "")) for i in selected_lo_indices if 0 <= i < len(all_los)]
        if picked_titles:
            topic_label += " — " + ", ".join(picked_titles)

    deck = LessonDeck(
        course_id=course.id,
        teacher_id=teacher.id,
        content_area_name=topic_label,
        title=title,
        duration_minutes=duration_minutes,
        previous_lesson_context=previous_lesson_context or None,
        include_practicals=bool(include_practicals),
        unit_index=unit_index,
        selected_lo_indices=selected_lo_indices or None,
        source_sow_id=source_sow_id or None,
        source_week=source_week or None,
        status="generating",
    )
    db.add(deck)
    db.commit()
    db.refresh(deck)

    content_text = _unit_content_text(unit, selected_lo_indices)
    background_tasks.add_task(
        _run_generation_job, deck.id, content_text, duration_minutes, previous_lesson_context, bool(include_practicals)
    )

    return RedirectResponse(url=f"/lesson-decks/{deck.id}", status_code=303)


def _run_generation_job(deck_id: int, content_text: str, duration_minutes: int, previous_lesson_context: str, include_practicals: bool):
    db = SessionLocal()
    try:
        deck = db.get(LessonDeck, deck_id)
        if not deck:
            return
        try:
            outline = generate_lesson_outline(content_text, duration_minutes, previous_lesson_context, include_practicals)

            # Resolve images and the colour palette ONCE, here, and store both in
            # outline_json - both the .pptx renderer and the "Present in browser" view
            # read from these same stored values, so they show identical photos/colours
            # instead of resolving separately (which would risk different photos each time
            # and burn through the Unsplash rate limit twice for no reason).
            outline["palette"] = pick_palette(outline.get("lesson_title", "lesson"))
            os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
            for slide in outline.get("slides", []):
                if slide.get("type") in ("content", "real_life_example"):
                    slide["resolved_image"] = resolve_slide_image(slide.get("image_query", ""), db, IMAGE_CACHE_DIR)

            deck.outline_json = outline
            deck.fits_in_requested_time = outline.get("fits_in_requested_time", True)
            deck.scope_note = outline.get("scope_note") or None

            os.makedirs(PPTX_DIR, exist_ok=True)
            output_path = os.path.join(PPTX_DIR, f"{uuid.uuid4().hex}_{deck.title}.pptx")
            generated_fallback_slides = render_lesson_pptx(outline, db, IMAGE_CACHE_DIR, output_path)

            deck.pptx_path = output_path
            deck.status = "ready"
            deck.error_detail = None
            if generated_fallback_slides:
                deck.error_detail = (
                    f"Note: {len(generated_fallback_slides)} slide(s) used a generated placeholder graphic "
                    f"instead of a real photo (no Unsplash key configured and no matching image in the "
                    f"library): {', '.join(generated_fallback_slides)}"
                )
        except Exception as e:
            deck.status = "failed"
            deck.error_detail = str(e)
        db.commit()
    finally:
        db.close()


@router.get("/lesson-decks/{deck_id}", response_class=HTMLResponse)
def view_deck(
    deck_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    resource_count = len(extract_resources(deck.outline_json)) if deck and deck.outline_json else 0
    return templates.TemplateResponse(
        "presentation_detail.html",
        {"request": request, "teacher": teacher, "deck": deck, "resource_count": resource_count},
    )


@router.get("/lesson-decks/{deck_id}/status")
def deck_status(
    deck_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    if not deck:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": deck.status, "error_detail": deck.error_detail}


@router.get("/lesson-decks/{deck_id}/download")
def download_deck(
    deck_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    if not deck or not deck.pptx_path or not os.path.exists(deck.pptx_path):
        return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)
    return FileResponse(
        deck.pptx_path,
        filename=f"{deck.title}.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


@router.get("/lesson-decks/{deck_id}/resources")
def download_deck_resources(
    deck_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Generated fresh from the deck's stored outline_json every time - so this stays
    available and re-downloadable any time after the PowerPoint itself was generated,
    without needing to regenerate the lesson at all.
    """
    deck = db.get(LessonDeck, deck_id)
    if not deck or not deck.outline_json:
        return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)

    output_path = os.path.join(PPTX_DIR, f"{uuid.uuid4().hex}_resources.docx")
    os.makedirs(PPTX_DIR, exist_ok=True)
    count = render_resources_docx(deck.outline_json, deck.title, output_path)
    if count == 0:
        return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)

    return FileResponse(
        output_path,
        filename=f"{deck.title} - Activity Resources.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.get("/lesson-decks/{deck_id}/slide-image/{slide_index}")
def deck_slide_image(
    deck_id: int,
    slide_index: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Serves the actual resolved photo for a slide, so present mode can show the SAME
    real image the PowerPoint uses - reads the path already resolved and stored at
    generation time, never re-resolves (that would risk a different photo each visit)."""
    deck = db.get(LessonDeck, deck_id)
    if not deck or not deck.outline_json:
        return RedirectResponse(url="/", status_code=303)
    slides = deck.outline_json.get("slides", [])
    if slide_index < 0 or slide_index >= len(slides):
        return RedirectResponse(url="/", status_code=303)
    image_info = slides[slide_index].get("resolved_image")
    if not image_info or image_info.get("type") != "photo" or not os.path.exists(image_info.get("path", "")):
        return RedirectResponse(url="/", status_code=303)
    return FileResponse(image_info["path"])


@router.get("/lesson-decks/{deck_id}/present", response_class=HTMLResponse)
def present_deck(
    deck_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    A full-screen, keyboard-navigable web view of the lesson - built from the exact same
    outline_json as the PowerPoint, so you don't need PowerPoint installed (or a download
    at all) to actually teach the lesson. Not a pixel-perfect match of the .pptx styling,
    but the content and structure are identical.
    """
    deck = db.get(LessonDeck, deck_id)
    if not deck or not deck.outline_json:
        return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)
    return templates.TemplateResponse("presentation_present.html", {"request": request, "teacher": teacher, "deck": deck})


# --- Shared image library ---

@router.get("/media-library", response_class=HTMLResponse)
def media_library(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    images = db.query(LessonImage).order_by(LessonImage.id.desc()).all()
    return templates.TemplateResponse(
        "media_library.html", {"request": request, "teacher": teacher, "images": images, "unsplash_test": None}
    )


@router.post("/media-library/test-unsplash", response_class=HTMLResponse)
def test_unsplash(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Runs a real, visible test call and shows you the ACTUAL result - no more guessing
    whether it's a wrong key, wrong key type, rate limit, or something else."""
    images = db.query(LessonImage).order_by(LessonImage.id.desc()).all()
    result = test_unsplash_connection()
    return templates.TemplateResponse(
        "media_library.html", {"request": request, "teacher": teacher, "images": images, "unsplash_test": result}
    )


@router.post("/media-library/upload")
async def upload_image(
    tags: str = Form(...),
    image_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{image_file.filename}"
    stored_path = os.path.join(UPLOAD_DIR, stored_name)
    contents = await image_file.read()
    with open(stored_path, "wb") as f:
        f.write(contents)

    db.add(LessonImage(uploaded_by_teacher_id=teacher.id, tags=tags, file_path=stored_path))
    db.commit()
    return RedirectResponse(url="/media-library", status_code=303)
