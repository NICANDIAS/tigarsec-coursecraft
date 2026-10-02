import os
import json
import uuid
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, Course, LessonDeck, LessonImage, SchemeOfWork, ResourceShare
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..pptx_generator import generate_lesson_outline, render_lesson_pptx, test_unsplash_connection, resolve_slide_image, pick_palette
from ..resource_export import render_resources_docx, extract_resources
from ..llm_gateway import get_effective_provider

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "storage/uploads"
PPTX_DIR = "storage/presentations"
IMAGE_CACHE_DIR = "storage/lesson_images"


def _match_lo_and_criteria(unit: dict, week_lo_codes: set) -> tuple[list, dict]:
    """
    Given a unit and the set of LO codes a scheme-of-work week actually covers, returns
    (lo_indices, criteria_selected) - lo_indices for well-structured, individually-named
    LOs whose own code matches; criteria_selected ({lo_idx: [criterion_idx, ...]}) for a
    flat/ungrouped LO (a "general-" code - see ExamExemplar's docstring in models.py)
    where the match has to happen one level down, against the individual criteria inside
    it, since the group itself was never given a real code of its own.

    Shared by both the exact-unit-name match and the whole-spec fallback below it, so this
    matching logic can't drift between the two paths.
    """
    lo_indices = []
    criteria_selected = {}
    for lo_idx, lo in enumerate(unit.get("learning_outcomes", [])):
        if lo.get("code") in week_lo_codes:
            lo_indices.append(lo_idx)
            continue
        matching_criteria = [
            c_idx for c_idx, crit in enumerate(lo.get("criteria", []))
            if crit.get("code") in week_lo_codes
        ]
        if matching_criteria:
            criteria_selected[lo_idx] = matching_criteria
    return lo_indices, criteria_selected


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
    course = get_course_for_teacher(db, course_id, teacher)
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
                criteria_selected = {}
                # Prefer the SOW's own selected_unit_names - an unambiguous record of which
                # unit(s) the whole scheme was actually generated for. Confirmed as a real,
                # standing bug: matching by criterion code alone (below) picked the wrong
                # unit when two different tracks in the same spec (Core Component vs
                # Occupational Specialism) independently reuse the same numbering - "2.1" in
                # one track is not the same criterion as "2.1" in another, but the old code
                # had no way to tell them apart.
                selected_names = set(target_sow.selected_unit_names or [])
                if selected_names:
                    for u_idx, unit in enumerate(units):
                        if unit.get("name") in selected_names:
                            unit_index = u_idx
                            lo_indices, criteria_selected = _match_lo_and_criteria(unit, week_lo_codes)
                            break
                if unit_index is None:
                    # Fallback for a "whole spec" scheme (no specific unit(s) selected at
                    # generation time) - code-based matching is the best available signal
                    # when there's no unit name to go on at all.
                    for u_idx, unit in enumerate(units):
                        matching, matching_criteria = _match_lo_and_criteria(unit, week_lo_codes)
                        if matching or matching_criteria:
                            unit_index = u_idx
                            lo_indices = matching
                            criteria_selected = matching_criteria
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
                    "criteria_selected": criteria_selected,
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
                "criteria_selected": {int(k): v for k, v in (source_deck.selected_criteria_by_lo or {}).items()},
                "previous_context": previous_context,
            }

    return templates.TemplateResponse(
        "presentation_new.html",
        {"request": request, "teacher": teacher, "course": course, "units": units,
         "sow_weeks": sow_weeks, "prefill": prefill},
    )


def _unit_content_text(unit: dict, selected_lo_indices: list[int] | None = None, selected_criteria_by_lo: dict | None = None) -> str:
    """
    Flattens a unit's LOs/criteria into plain text for the LLM prompt. If selected_lo_indices
    is given (non-empty), scopes to just those whole LOs - this is what lets a single lesson
    target specific topics within a content area instead of the whole unit, which usually
    spans months of teaching.

    selected_criteria_by_lo additionally supports picking INDIVIDUAL criteria within one LO,
    rather than always including the whole thing - confirmed as a real, necessary gap: a spec
    with a flat structure (no real named LO grouping) puts every criterion in the content area
    under ONE synthetic LO (see models.py's ExamExemplar docstring / the "general-" code
    convention), so without this, selecting that one LO meant pulling in everything - 25
    criteria for one lesson - rather than the few actually relevant to it.
    """
    all_los = unit.get("learning_outcomes", [])
    selected_criteria_by_lo = selected_criteria_by_lo or {}
    selected_lo_index_set = set(selected_lo_indices or [])
    has_any_selection = bool(selected_lo_index_set) or bool(selected_criteria_by_lo)
    lines = [f"Unit: {unit.get('name')}"]
    for lo_idx, lo in enumerate(all_los):
        crit_indices = selected_criteria_by_lo.get(lo_idx)
        if has_any_selection:
            # Something was picked somewhere - only include a LO here if IT was picked
            # (whole or in part). A criteria-only pick with no whole-LO ticks must not
            # fall through to "nothing selected -> use everything", or picking three
            # specific criteria would still pull in every other LO in full.
            if lo_idx not in selected_lo_index_set and crit_indices is None:
                continue
        # else: nothing selected anywhere - matches the existing "leave all unticked to
        # build a bigger lesson covering the whole content area" behavior.
        criteria = lo.get("criteria", [])
        criteria_to_use = (
            [criteria[i] for i in crit_indices if 0 <= i < len(criteria)]
            if crit_indices else criteria
        )
        lines.append(f"\n{lo.get('code')} - {lo.get('title')}")
        for c in criteria_to_use:
            lines.append(f"  {c.get('code')}: {c.get('description')}")
    return "\n".join(lines)


@router.post("/courses/{course_id}/presentations/generate")
async def generate_presentation(
    course_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    unit_index: int = Form(...),
    title: str = Form(...),
    duration_hours: float = Form(1.0),
    previous_lesson_context: str = Form(""),
    include_practicals: str = Form(""),
    source_sow_id: int = Form(0),
    source_week: int = Form(0),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    # The form now takes hours (a 5-hour session should just work, not be capped at 240
    # minutes) - converted to minutes right here, at the one boundary where the form value
    # enters the system. Everything downstream (deck.duration_minutes storage, regenerate,
    # the generation job, generate_lesson_outline) is untouched and keeps working in
    # minutes exactly as before.
    duration_minutes = round(duration_hours * 60)
    course = get_course_for_teacher(db, course_id, teacher)
    if not course or not course.spec_version or not course.spec_version.structured_json:
        return RedirectResponse(url=f"/courses/{course_id}", status_code=303)

    units = course.spec_version.structured_json.get("units", [])
    if unit_index < 0 or unit_index >= len(units):
        return RedirectResponse(url=f"/courses/{course_id}/presentations/new", status_code=303)

    unit = units[unit_index]

    form = await request.form()
    raw_selections = form.getlist("selected_los")
    # Two value shapes arrive here: a plain integer ("3") is a whole-LO pick, unchanged
    # from before. "lo_idx.crit_idx" ("0.5") is a pick of one specific criterion WITHIN a
    # flat/ungrouped LO - added because a spec with no real LO grouping puts every
    # criterion in a content area under one synthetic LO, so without this, picking that
    # one LO meant pulling in everything under it rather than the few actually relevant
    # to this lesson (confirmed as a real, reported problem, not a hypothetical one).
    selected_lo_indices = []
    selected_criteria_by_lo = {}
    for v in raw_selections:
        if "." in v:
            lo_part, crit_part = v.split(".", 1)
            if lo_part.isdigit() and crit_part.isdigit():
                selected_criteria_by_lo.setdefault(int(lo_part), []).append(int(crit_part))
        elif v.isdigit():
            selected_lo_indices.append(int(v))

    all_los = unit.get("learning_outcomes", [])
    topic_label = unit.get("name", "Untitled unit")
    label_parts = []
    for i in selected_lo_indices:
        if 0 <= i < len(all_los):
            label_parts.append(all_los[i].get("title") or all_los[i].get("code", ""))
    for lo_idx, crit_indices in selected_criteria_by_lo.items():
        if 0 <= lo_idx < len(all_los):
            criteria = all_los[lo_idx].get("criteria", [])
            codes = [criteria[i].get("code", "") for i in crit_indices if 0 <= i < len(criteria)]
            if codes:
                label_parts.append("Criteria " + ", ".join(c for c in codes if c))
    if label_parts:
        topic_label += " — " + ", ".join(label_parts)

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
        selected_criteria_by_lo=selected_criteria_by_lo or None,
        source_sow_id=source_sow_id or None,
        source_week=source_week or None,
        status="generating",
    )
    db.add(deck)
    db.commit()
    db.refresh(deck)

    content_text = _unit_content_text(unit, selected_lo_indices, selected_criteria_by_lo)
    background_tasks.add_task(
        _run_generation_job, deck.id, content_text, duration_minutes, previous_lesson_context, bool(include_practicals)
    )

    return RedirectResponse(url=f"/lesson-decks/{deck.id}", status_code=303)


@router.post("/lesson-decks/{deck_id}/regenerate")
def regenerate_deck(
    deck_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Standing fix: generating a PowerPoint had no retry at all - a failure meant going back
    to "New presentation" and re-entering everything (unit, LOs, duration, context) from
    scratch. Every parameter needed to regenerate is already stored on the deck's own row
    (unit_index, selected_lo_indices, duration_minutes, previous_lesson_context,
    include_practicals) - this re-derives content_text exactly as the original generation
    did and re-queues the same job on the SAME deck row, so the deck keeps its one
    permanent URL rather than spawning a duplicate.
    """
    deck = db.get(LessonDeck, deck_id)
    if not deck or not _can_access_deck(deck, teacher, db):
        return RedirectResponse(url="/", status_code=303)

    course = db.get(Course, deck.course_id)
    if not course or not course.spec_version or not course.spec_version.structured_json:
        return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)
    units = course.spec_version.structured_json.get("units", [])
    if deck.unit_index is None or deck.unit_index < 0 or deck.unit_index >= len(units):
        deck.status = "failed"
        deck.error_detail = "Can't regenerate - the course specification has changed since this deck was first generated, so its original unit no longer exists. Generate a new one from the course page instead."
        db.commit()
        return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)

    unit = units[deck.unit_index]
    # JSON round-tripping always turns dict keys into strings - convert back to int here
    # so this matches what _unit_content_text expects (the same keys enumerate() produces).
    stored_criteria_by_lo = {int(k): v for k, v in (deck.selected_criteria_by_lo or {}).items()}
    content_text = _unit_content_text(unit, deck.selected_lo_indices or [], stored_criteria_by_lo)

    deck.status = "generating"
    deck.error_detail = None
    db.commit()

    background_tasks.add_task(
        _run_generation_job, deck.id, content_text, deck.duration_minutes,
        deck.previous_lesson_context or "", bool(deck.include_practicals),
    )

    return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)


def _run_generation_job(deck_id: int, content_text: str, duration_minutes: int, previous_lesson_context: str, include_practicals: bool):
    db = SessionLocal()
    try:
        deck = db.get(LessonDeck, deck_id)
        if not deck:
            return
        try:
            outline = generate_lesson_outline(content_text, duration_minutes, previous_lesson_context, include_practicals)
            deck.generated_by_provider = get_effective_provider(for_draft_generation=True)

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


def _can_access_deck(deck: LessonDeck, teacher: Teacher, db: Session) -> bool:
    """
    Standing fix: every deck route below previously had NO ownership check at all - any
    authenticated teacher could open, download, or present ANY other teacher's lesson deck
    just by knowing/guessing its numeric ID. Access is now the deck's own creator, OR
    someone it's been explicitly shared with via ResourceShare (see the sharing routes
    below) - sharing is a deliberate, visible, revocable grant, not an accident of a
    missing check.
    """
    if not deck:
        return False
    if deck.teacher_id == teacher.id:
        return True
    return db.query(ResourceShare).filter(
        ResourceShare.lesson_deck_id == deck.id,
        ResourceShare.shared_with_teacher_id == teacher.id,
    ).first() is not None


@router.post("/lesson-decks/{deck_id}/delete")
def delete_deck(
    deck_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Creator-only, not the broader _can_access_deck check - someone a deck was shared with
    can view/use it, but deleting the creator's work is a different, stronger permission
    that sharing was never meant to grant.
    """
    deck = db.get(LessonDeck, deck_id)
    if not deck or deck.teacher_id != teacher.id:
        return RedirectResponse(url="/", status_code=303)
    course_id = deck.course_id
    if deck.pptx_path and os.path.exists(deck.pptx_path):
        os.remove(deck.pptx_path)
    db.query(ResourceShare).filter(ResourceShare.lesson_deck_id == deck_id).delete()
    db.delete(deck)
    db.commit()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


@router.get("/lesson-decks/{deck_id}", response_class=HTMLResponse)
def view_deck(
    deck_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    if not _can_access_deck(deck, teacher, db):
        return RedirectResponse(url="/", status_code=303)
    resource_count = len(extract_resources(deck.outline_json)) if deck and deck.outline_json else 0
    shares = db.query(ResourceShare).filter(ResourceShare.lesson_deck_id == deck_id).all() if deck.teacher_id == teacher.id else []
    is_owner = deck.teacher_id == teacher.id
    return templates.TemplateResponse(
        "presentation_detail.html",
        {"request": request, "teacher": teacher, "deck": deck, "resource_count": resource_count,
         "shares": shares, "is_owner": is_owner},
    )


@router.get("/lesson-decks/{deck_id}/status")
def deck_status(
    deck_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    if not _can_access_deck(deck, teacher, db):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": deck.status, "error_detail": deck.error_detail}


@router.get("/lesson-decks/{deck_id}/download")
def download_deck(
    deck_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    if not _can_access_deck(deck, teacher, db):
        return RedirectResponse(url="/", status_code=303)
    if not deck.pptx_path or not os.path.exists(deck.pptx_path):
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
    if not _can_access_deck(deck, teacher, db):
        return RedirectResponse(url="/", status_code=303)
    if not deck.outline_json:
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
    if not _can_access_deck(deck, teacher, db):
        return RedirectResponse(url="/", status_code=303)
    if not deck.outline_json:
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
    if not _can_access_deck(deck, teacher, db):
        return RedirectResponse(url="/", status_code=303)
    if not deck.outline_json:
        return RedirectResponse(url=f"/lesson-decks/{deck_id}", status_code=303)
    return templates.TemplateResponse("presentation_present.html", {"request": request, "teacher": teacher, "deck": deck})


# --- Cover/absence resource sharing ---

@router.get("/lesson-decks/{deck_id}/share", response_class=HTMLResponse)
def share_deck_page(
    deck_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Send this week's PowerPoint/activities to a named colleague - e.g. for cover during
    an absence. Only the deck's own creator can share it (a shared recipient can view/
    download but not re-share further, keeping the grant list auditable back to one owner)."""
    deck = db.get(LessonDeck, deck_id)
    if not deck or deck.teacher_id != teacher.id:
        return RedirectResponse(url="/", status_code=303)
    other_teachers = db.query(Teacher).filter(Teacher.id != teacher.id, Teacher.is_active == True).order_by(Teacher.name).all()
    shares = db.query(ResourceShare).filter(ResourceShare.lesson_deck_id == deck_id).all()
    return templates.TemplateResponse(
        "presentation_share.html",
        {"request": request, "teacher": teacher, "deck": deck, "other_teachers": other_teachers, "shares": shares},
    )


@router.post("/lesson-decks/{deck_id}/share")
def share_deck(
    deck_id: int,
    shared_with_teacher_id: int = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    if not deck or deck.teacher_id != teacher.id:
        return RedirectResponse(url="/", status_code=303)
    target = db.get(Teacher, shared_with_teacher_id)
    if not target or target.id == teacher.id:
        return RedirectResponse(url=f"/lesson-decks/{deck_id}/share", status_code=303)

    existing = db.query(ResourceShare).filter(
        ResourceShare.lesson_deck_id == deck_id, ResourceShare.shared_with_teacher_id == target.id
    ).first()
    if not existing:
        db.add(ResourceShare(
            lesson_deck_id=deck_id, shared_by_teacher_id=teacher.id,
            shared_with_teacher_id=target.id, note=note.strip() or None,
        ))
        db.commit()
    return RedirectResponse(url=f"/lesson-decks/{deck_id}/share", status_code=303)


@router.post("/lesson-decks/{deck_id}/share/{share_id}/revoke")
def revoke_deck_share(
    deck_id: int,
    share_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    deck = db.get(LessonDeck, deck_id)
    if not deck or deck.teacher_id != teacher.id:
        return RedirectResponse(url="/", status_code=303)
    share = db.query(ResourceShare).filter(ResourceShare.id == share_id, ResourceShare.lesson_deck_id == deck_id).first()
    if share:
        db.delete(share)
        db.commit()
    return RedirectResponse(url=f"/lesson-decks/{deck_id}/share", status_code=303)


@router.get("/shared-with-me", response_class=HTMLResponse)
def shared_with_me(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Everything a colleague has explicitly sent this teacher - e.g. cover resources for
    an absence - regardless of which course it belongs to."""
    shares = (
        db.query(ResourceShare)
        .filter(ResourceShare.shared_with_teacher_id == teacher.id)
        .order_by(ResourceShare.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "shared_with_me.html", {"request": request, "teacher": teacher, "shares": shares},
    )


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
