import datetime
import json
import random
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Room, StaffUnavailability, TeacherUnitQualification, SpecVersion, ROOM_TYPES, Course, TimetableSession, DAYS_OF_WEEK, TeachingHoursBand, TimetablingSettings, CourseUnitRequirement, Site, CourseTeacher, TeacherCPDRecord, TeacherIVOutcome, SkillRankingWeights
from ..auth import require_timetabling_access, require_admin, hash_password, get_current_teacher
from ..timetabling_settings import get_timetabling_settings, get_skill_ranking_weights
from .. import timetable_generator
from .. import skill_ranking

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def calculate_teaching_hours(contracted_hours: float, bands: list) -> tuple:
    """
    Given a teacher's contracted hours and the college's own defined bands (contracted
    hours -> max teaching hours), returns (teaching_hours, is_estimate).

    Deliberately a real, editable policy table rather than a fixed formula or percentage -
    a real example given directly contradicted a simple fixed percentage: 35 contracted
    hours -> 24 teaching hours (~68.6%), but 20 contracted hours -> 13 teaching hours
    (65%) at the same college. The ratio itself shifts with contract size, most likely
    because some admin/prep overhead doesn't shrink proportionally just because someone's
    contract does - so this has to look up real policy points, not compute a percentage.

    Exact matches return that band's value directly (is_estimate=False). Values between
    two defined bands are linearly interpolated (also not an estimate - it's a real
    calculation within a range the college has actually defined policy for). Values
    outside the whole defined range are extrapolated from the nearest band's own ratio and
    flagged as an estimate, since no explicit college policy exists for that range yet.
    """
    if not bands:
        return None, False
    sorted_bands = sorted(bands, key=lambda b: b[0])
    for ch, th in sorted_bands:
        if abs(ch - contracted_hours) < 1e-9:
            return th, False
    if contracted_hours < sorted_bands[0][0]:
        ch, th = sorted_bands[0]
        return contracted_hours * (th / ch), True
    if contracted_hours > sorted_bands[-1][0]:
        ch, th = sorted_bands[-1]
        return contracted_hours * (th / ch), True
    for i in range(len(sorted_bands) - 1):
        x1, y1 = sorted_bands[i]
        x2, y2 = sorted_bands[i + 1]
        if x1 <= contracted_hours <= x2:
            return y1 + (contracted_hours - x1) * (y2 - y1) / (x2 - x1), False
    return None, False


@router.get("/timetabling", response_class=HTMLResponse)
def timetabling_home(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    room_count = db.query(Room).filter(Room.is_active == True).count()
    staff_count = db.query(Teacher).filter(Teacher.is_active == True).count()
    sessions = db.query(TimetableSession).all()
    clash_count = len(_detect_clashes(sessions, db))
    settings = get_timetabling_settings(db)
    return templates.TemplateResponse(
        "timetabling_home.html",
        {"request": request, "teacher": teacher, "room_count": room_count, "staff_count": staff_count,
         "session_count": len(sessions), "clash_count": clash_count, "settings": settings},
    )


@router.post("/timetabling/settings/update")
def update_timetabling_settings(
    working_day_start: str = Form(...),
    working_day_end: str = Form(...),
    lunch_start: str = Form(""),
    lunch_end: str = Form(""),
    min_break_minutes: int = Form(15),
    break_after_hours: float = Form(3.0),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    import datetime as _dt
    if working_day_end <= working_day_start:
        return RedirectResponse(url="/timetabling", status_code=303)
    # Both lunch fields together, or neither - a half-set lunch window doesn't mean anything.
    has_lunch = bool(lunch_start) and bool(lunch_end)
    settings = get_timetabling_settings(db)
    if not db.get(TimetablingSettings, 1):
        db.add(settings)
    settings.working_day_start = working_day_start
    settings.working_day_end = working_day_end
    settings.lunch_start = lunch_start if has_lunch else None
    settings.lunch_end = lunch_end if has_lunch else None
    settings.min_break_minutes = min_break_minutes
    settings.break_after_hours = break_after_hours
    settings.updated_at = _dt.datetime.utcnow()
    settings.updated_by_teacher_id = teacher.id
    db.commit()
    return RedirectResponse(url="/timetabling", status_code=303)


@router.get("/timetabling/rooms", response_class=HTMLResponse)
def list_rooms(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    rooms = db.query(Room).order_by(Room.is_active.desc(), Room.name).all()
    sites = db.query(Site).order_by(Site.name).all()
    return templates.TemplateResponse(
        "timetabling_rooms.html",
        {"request": request, "teacher": teacher, "rooms": rooms, "room_types": ROOM_TYPES, "sites": sites,
         "delete_error": request.query_params.get("delete_error")},
    )


@router.post("/timetabling/sites/add")
def add_site(
    name: str = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    name = name.strip()
    if name:
        db.add(Site(name=name))
        db.commit()
    return RedirectResponse(url="/timetabling/rooms", status_code=303)


@router.post("/timetabling/sites/{site_id}/remove")
def remove_site(
    site_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    site = db.get(Site, site_id)
    if site:
        db.delete(site)
        db.commit()
    return RedirectResponse(url="/timetabling/rooms", status_code=303)


@router.post("/timetabling/rooms/create")
def create_room(
    name: str = Form(...),
    capacity: int = Form(...),
    room_type: str = Form("general_classroom"),
    building: str = Form(""),
    supports_isolated_network: str = Form(""),
    accessibility_features: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """
    Splits the name field on commas and newlines into one room per name, all sharing the
    same capacity/type/building settings - confirmed as a real, needed gap: typing several
    room numbers together (e.g. "DI212, DI214") previously created ONE room with that
    literal comma in its name, rather than two real, separate rooms, which then showed up
    combined on every session in the generator's preview.
    """
    valid_types = {key for key, _ in ROOM_TYPES}
    names = [n.strip() for n in re.split(r"[,\n]+", name) if n.strip()]
    for room_name in names:
        db.add(Room(
            name=room_name,
            capacity=capacity,
            room_type=room_type if room_type in valid_types else "general_classroom",
            building=building.strip() or None,
            supports_isolated_network=bool(supports_isolated_network),
            accessibility_features=accessibility_features.strip() or None,
        ))
    db.commit()
    return RedirectResponse(url="/timetabling/rooms", status_code=303)


@router.post("/timetabling/rooms/{room_id}/edit")
def edit_room(
    room_id: int,
    name: str = Form(...),
    capacity: int = Form(...),
    room_type: str = Form("general_classroom"),
    building: str = Form(""),
    supports_isolated_network: str = Form(""),
    accessibility_features: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    room = db.get(Room, room_id)
    if room:
        valid_types = {key for key, _ in ROOM_TYPES}
        room.name = name.strip()
        room.capacity = capacity
        room.room_type = room_type if room_type in valid_types else "general_classroom"
        room.building = building.strip() or None
        room.supports_isolated_network = bool(supports_isolated_network)
        room.accessibility_features = accessibility_features.strip() or None
        db.commit()
    return RedirectResponse(url="/timetabling/rooms", status_code=303)


@router.post("/timetabling/rooms/{room_id}/toggle-active")
def toggle_room_active(
    room_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """
    Deliberately a toggle, not a delete - a room going out of use (refurbishment, closure)
    shouldn't lose its history once real sessions start referencing it in later phases.
    """
    room = db.get(Room, room_id)
    if room:
        room.is_active = not room.is_active
        db.commit()
    return RedirectResponse(url="/timetabling/rooms", status_code=303)


@router.post("/timetabling/rooms/{room_id}/delete")
def delete_room(
    room_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """
    A real delete, but conservative - only allowed when no session currently references
    this room. A genuinely mistaken or never-used room (e.g. created with a botched
    combined name before the bulk-creation split existed) can be cleanly removed; a room
    with real scheduling history stays protected, matching the reasoning already given for
    why this was a toggle rather than a delete in the first place.
    """
    room = db.get(Room, room_id)
    if not room:
        return RedirectResponse(url="/timetabling/rooms", status_code=303)
    in_use = db.query(TimetableSession).filter(TimetableSession.room_id == room_id).count()
    if in_use:
        message = f"Can't delete - {in_use} session(s) still use this room. Deactivate it instead, or move those sessions first."
        return RedirectResponse(url=f"/timetabling/rooms?delete_error={quote(message)}", status_code=303)
    db.delete(room)
    db.commit()
    return RedirectResponse(url="/timetabling/rooms", status_code=303)


@router.get("/timetabling/staff", response_class=HTMLResponse)
def list_staff_availability(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    staff = db.query(Teacher).filter(Teacher.is_active == True).order_by(Teacher.name).all()
    qualifications_by_teacher = {}
    for q in db.query(TeacherUnitQualification).all():
        qualifications_by_teacher.setdefault(q.teacher_id, []).append(q)
    unavailability_by_teacher = {}
    for u in db.query(StaffUnavailability).order_by(StaffUnavailability.start_date.desc()).all():
        unavailability_by_teacher.setdefault(u.teacher_id, []).append(u)

    # Grouped by spec (with a "select all in this spec" toggle in the template) rather
    # than a flat list - directly supports the real use case of qualifying someone for
    # every unit in one programme at once, not ticking each individually.
    spec_unit_groups = []
    for spec in db.query(SpecVersion).filter(SpecVersion.parse_status == "parsed").order_by(SpecVersion.label).all():
        unit_names = [unit.get("name", "?") for unit in (spec.structured_json or {}).get("units", [])]
        if unit_names:
            spec_unit_groups.append({"spec_id": spec.id, "spec_label": spec.label, "unit_names": unit_names})

    bands_query = db.query(TeachingHoursBand).order_by(TeachingHoursBand.contracted_hours).all()
    bands = [(b.contracted_hours, b.max_teaching_hours) for b in bands_query]
    teaching_hours_by_teacher = {}
    for person in staff:
        if person.contracted_hours_per_week is not None:
            hours, is_estimate = calculate_teaching_hours(person.contracted_hours_per_week, bands)
            if hours is not None:
                teaching_hours_by_teacher[person.id] = {
                    "teaching_hours": round(hours, 1),
                    "prep_hours": round(person.contracted_hours_per_week - hours, 1),
                    "is_estimate": is_estimate,
                }

    return templates.TemplateResponse(
        "timetabling_staff.html",
        {"request": request, "teacher": teacher, "staff": staff, "weekdays": DAYS_OF_WEEK,
         "qualifications_by_teacher": qualifications_by_teacher,
         "unavailability_by_teacher": unavailability_by_teacher,
         "teaching_hours_by_teacher": teaching_hours_by_teacher,
         "bands": bands_query,
         "spec_unit_groups": spec_unit_groups},
    )


@router.post("/timetabling/teaching-hours-bands/add")
def add_teaching_hours_band(
    contracted_hours: float = Form(...),
    max_teaching_hours: float = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    if contracted_hours > 0 and max_teaching_hours >= 0:
        db.add(TeachingHoursBand(contracted_hours=contracted_hours, max_teaching_hours=max_teaching_hours))
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/teaching-hours-bands/{band_id}/remove")
def remove_teaching_hours_band(
    band_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    band = db.get(TeachingHoursBand, band_id)
    if band:
        db.delete(band)
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/{teacher_id}/update")
async def update_staff_availability(
    teacher_id: int,
    request: Request,
    contracted_hours_per_week: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    form = await request.form()
    non_teaching_days = [d for d in form.getlist("non_teaching_days") if d in DAYS_OF_WEEK]
    target = db.get(Teacher, teacher_id)
    if target:
        target.contracted_hours_per_week = (
            float(contracted_hours_per_week) if contracted_hours_per_week.strip().replace(".", "", 1).isdigit() else None
        )
        target.non_teaching_days = non_teaching_days or None
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/{teacher_id}/qualifications/add")
async def add_qualification(
    teacher_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """
    Accepts MULTIPLE spec_units in one submission - confirmed as a real need: selecting
    every relevant unit for a person one at a time, submitting after each, was real,
    avoidable friction. Each arrives as "{spec_id}|{unit_name}" (same combined-field
    format as before, just now a checkbox group instead of a single dropdown - see
    timetabling_staff.html).
    """
    target = db.get(Teacher, teacher_id)
    if not target:
        return RedirectResponse(url="/timetabling/staff", status_code=303)

    form = await request.form()
    spec_units = form.getlist("spec_units")

    existing = {
        (q.spec_version_id, q.unit_name)
        for q in db.query(TeacherUnitQualification).filter(TeacherUnitQualification.teacher_id == teacher_id).all()
    }
    added = 0
    for spec_unit in spec_units:
        if "|" not in spec_unit:
            continue
        spec_id_str, unit_name = spec_unit.split("|", 1)
        if not spec_id_str.isdigit():
            continue
        key = (int(spec_id_str), unit_name)
        if key in existing:
            continue
        db.add(TeacherUnitQualification(teacher_id=teacher_id, spec_version_id=key[0], unit_name=unit_name))
        existing.add(key)
        added += 1
    if added:
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/qualifications/{qualification_id}/remove")
def remove_qualification(
    qualification_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    q = db.get(TeacherUnitQualification, qualification_id)
    if q:
        db.delete(q)
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/qualifications/{qualification_id}/approve")
def approve_qualification(
    qualification_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """Confirms a teacher-requested qualification, making it count for real generator
    placement decisions - the completion of the self-service request workflow."""
    q = db.get(TeacherUnitQualification, qualification_id)
    if q:
        q.status = "confirmed"
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/qualifications/{qualification_id}/keep")
def keep_qualification(
    qualification_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """Declines a teacher's removal request - reverts back to confirmed rather than
    deleting. The other half of reviewing a "pending_removal" request; the existing
    remove_qualification route below already handles the "confirm removal" side (an
    unconditional delete, regardless of status)."""
    q = db.get(TeacherUnitQualification, qualification_id)
    if q and q.status == "pending_removal":
        q.status = "confirmed"
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/qualifications/{qualification_id}/skill")
def update_qualification_skill_data(
    qualification_id: int,
    years_experience: str = Form(""),
    admin_rating: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """
    Sets years of experience and/or a discretionary admin rating (1-5) for this specific
    (teacher, unit) qualification - the two simplest skill-ranking factors, feeding
    directly into skill_ranking.score_teacher_for_unit as a tie-breaker when the generator
    has more than one qualified, available teacher to choose from. Both fields are
    optional and can be cleared - a college that never fills these in gets exactly today's
    load-balancing-only behaviour, unchanged. Malformed input is silently ignored rather
    than crashing the page - the existing value is simply left as it was.
    """
    q = db.get(TeacherUnitQualification, qualification_id)
    if not q:
        return RedirectResponse(url="/timetabling/staff", status_code=303)
    if not years_experience.strip():
        q.years_experience = None
    else:
        try:
            val = float(years_experience)
            if val >= 0:
                q.years_experience = val
        except ValueError:
            pass
    if not admin_rating.strip():
        q.admin_rating = None
    else:
        try:
            val = int(admin_rating)
            if 1 <= val <= 5:
                q.admin_rating = val
        except ValueError:
            pass
    db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/{teacher_id}/unavailability/add")
def add_unavailability(
    teacher_id: int,
    start_date: str = Form(...),
    end_date: str = Form(...),
    portion: str = Form("full_day"),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    target = db.get(Teacher, teacher_id)
    if portion not in ("full_day", "am", "pm"):
        portion = "full_day"
    try:
        start = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
    except ValueError:
        return RedirectResponse(url="/timetabling/staff", status_code=303)
    # A half-day absence spanning a date range doesn't make sense - force it to a single
    # date regardless of what was submitted, rather than trusting the frontend alone to
    # prevent that combination.
    if portion in ("am", "pm"):
        end = start
    if target and end >= start:
        db.add(StaffUnavailability(
            teacher_id=teacher_id, start_date=start, end_date=end, portion=portion,
            reason=reason.strip() or None, entered_by_teacher_id=teacher.id,
        ))
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/staff/unavailability/{unavailability_id}/remove")
def remove_unavailability(
    unavailability_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    u = db.get(StaffUnavailability, unavailability_id)
    if u:
        db.delete(u)
        db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.get("/timetabling/my-availability", response_class=HTMLResponse)
def my_availability(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Self-service for ANY logged-in teacher, not just those with timetabling access -
    everyone should be able to manage their own contracted hours and unavailable dates
    without needing an admin to do it for them. Deliberately a separate page from the
    admin Staff view rather than one page behaving two different ways depending on who's
    looking, which was the exact reasoning agreed for this feature.

    Contracted hours, non-teaching days, and one-off unavailability are all immediately
    self-editable. Qualifications are not - a request here creates a "pending" row that
    doesn't yet count for real generator placement decisions until an admin confirms it,
    since a mistaken or optimistic self-entry could otherwise place someone in front of a
    class they're not actually ready for.
    """
    my_qualifications = db.query(TeacherUnitQualification).filter(TeacherUnitQualification.teacher_id == teacher.id).all()
    my_unavailability = db.query(StaffUnavailability).filter(StaffUnavailability.teacher_id == teacher.id).order_by(StaffUnavailability.start_date.desc()).all()

    spec_unit_groups = []
    for spec in db.query(SpecVersion).filter(SpecVersion.parse_status == "parsed").order_by(SpecVersion.label).all():
        unit_names = [unit.get("name", "?") for unit in (spec.structured_json or {}).get("units", [])]
        if unit_names:
            spec_unit_groups.append({"spec_id": spec.id, "spec_label": spec.label, "unit_names": unit_names})

    bands_query = db.query(TeachingHoursBand).order_by(TeachingHoursBand.contracted_hours).all()
    bands = [(b.contracted_hours, b.max_teaching_hours) for b in bands_query]
    teaching_hours = None
    if teacher.contracted_hours_per_week is not None:
        hours, is_estimate = calculate_teaching_hours(teacher.contracted_hours_per_week, bands)
        if hours is not None:
            teaching_hours = {"teaching_hours": round(hours, 1), "prep_hours": round(teacher.contracted_hours_per_week - hours, 1), "is_estimate": is_estimate}

    return templates.TemplateResponse(
        "my_availability.html",
        {"request": request, "teacher": teacher, "weekdays": DAYS_OF_WEEK,
         "my_qualifications": my_qualifications, "my_unavailability": my_unavailability,
         "spec_unit_groups": spec_unit_groups, "teaching_hours": teaching_hours,
         "saved": request.query_params.get("saved")},
    )


@router.post("/timetabling/my-availability/update")
async def update_my_availability(
    request: Request,
    contracted_hours_per_week: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    form = await request.form()
    non_teaching_days = [d for d in form.getlist("non_teaching_days") if d in DAYS_OF_WEEK]
    teacher.contracted_hours_per_week = (
        float(contracted_hours_per_week) if contracted_hours_per_week.strip().replace(".", "", 1).isdigit() else None
    )
    teacher.non_teaching_days = non_teaching_days or None
    db.commit()
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)


@router.post("/timetabling/my-availability/unavailability/add")
def add_my_unavailability(
    start_date: str = Form(...),
    end_date: str = Form(...),
    portion: str = Form("full_day"),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    if portion not in ("full_day", "am", "pm"):
        portion = "full_day"
    try:
        start = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
    except ValueError:
        return RedirectResponse(url="/timetabling/my-availability", status_code=303)
    if portion in ("am", "pm"):
        end = start
    if end >= start:
        db.add(StaffUnavailability(
            teacher_id=teacher.id, start_date=start, end_date=end, portion=portion,
            reason=reason.strip() or None, entered_by_teacher_id=teacher.id,
        ))
        db.commit()
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)


@router.post("/timetabling/my-availability/unavailability/{unavailability_id}/remove")
def remove_my_unavailability(
    unavailability_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    u = db.get(StaffUnavailability, unavailability_id)
    if u and u.teacher_id == teacher.id:  # ownership check - an id alone is not proof this entry belongs to the requesting teacher
        db.delete(u)
        db.commit()
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)


@router.post("/timetabling/my-availability/qualifications/request")
async def request_my_qualification(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    form = await request.form()
    spec_units = form.getlist("spec_units")
    existing = {
        (q.spec_version_id, q.unit_name)
        for q in db.query(TeacherUnitQualification).filter(TeacherUnitQualification.teacher_id == teacher.id).all()
    }
    added = 0
    for spec_unit in spec_units:
        if "|" not in spec_unit:
            continue
        spec_id_str, unit_name = spec_unit.split("|", 1)
        if not spec_id_str.isdigit():
            continue
        key = (int(spec_id_str), unit_name)
        if key in existing:
            continue
        db.add(TeacherUnitQualification(teacher_id=teacher.id, spec_version_id=key[0], unit_name=unit_name, status="pending"))
        existing.add(key)
        added += 1
    if added:
        db.commit()
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)


@router.post("/timetabling/my-availability/qualifications/{qualification_id}/withdraw")
def withdraw_my_qualification_request(
    qualification_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """A teacher can withdraw their own PENDING request - but not remove a CONFIRMED
    qualification directly, which is an admin decision, not a self-service one. For a
    confirmed one, see request_my_qualification_removal below instead."""
    q = db.get(TeacherUnitQualification, qualification_id)
    if q and q.teacher_id == teacher.id and q.status == "pending":
        db.delete(q)
        db.commit()
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)


@router.post("/timetabling/my-availability/qualifications/{qualification_id}/request-removal")
def request_my_qualification_removal(
    qualification_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    A confirmed qualification is trusted for real scheduling decisions, so a teacher can't
    delete it outright - but they need SOME way to flag a mistake without hunting down an
    admin themselves. Flags it "pending_removal" instead: still counted as confirmed for
    generator purposes until an admin actually acts on it (an unreviewed removal request
    could otherwise disrupt scheduling that's already relying on it), but now visible to
    admin as something needing a decision.
    """
    q = db.get(TeacherUnitQualification, qualification_id)
    if q and q.teacher_id == teacher.id and q.status == "confirmed":
        q.status = "pending_removal"
        db.commit()
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)


@router.post("/timetabling/my-availability/qualifications/{qualification_id}/cancel-removal")
def cancel_my_qualification_removal(
    qualification_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Lets a teacher undo their own removal request before an admin has acted on it."""
    q = db.get(TeacherUnitQualification, qualification_id)
    if q and q.teacher_id == teacher.id and q.status == "pending_removal":
        q.status = "confirmed"
        db.commit()
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)
    return RedirectResponse(url="/timetabling/my-availability?saved=1", status_code=303)


def _times_overlap(s1: TimetableSession, s2: TimetableSession) -> bool:
    """Two sessions overlap if they're on the same day and their time ranges intersect.
    start_time/end_time are zero-padded 24-hour "HH:MM" strings specifically so plain
    string comparison already matches chronological order - verified directly (not just
    assumed) before relying on it here."""
    return s1.day_of_week == s2.day_of_week and s1.start_time < s2.end_time and s2.start_time < s1.end_time


_WEEKDAY_INDEX = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}


def _dates_matching_weekday_in_range(day_of_week: str, range_start, range_end, min_date=None) -> list:
    """
    Every real calendar date within [range_start, range_end] that falls on day_of_week -
    the actual occurrences of a recurring weekly session, needed to check a one-off dated
    unavailability against it. Optionally bounded below by min_date (a course's own start
    date): a session's weekly pattern isn't actually live before the course itself has
    started, so an unavailability entirely before that has nothing real to conflict with.
    No min_date (course has no start_date set) means every date in range is considered
    possibly live - the same "don't assume it's fine" direction taken elsewhere in this
    project when a course's own dates aren't known.
    """
    target_weekday = _WEEKDAY_INDEX.get(day_of_week)
    if target_weekday is None:
        return []
    effective_start = max(range_start, min_date) if min_date else range_start
    if effective_start > range_end:
        return []
    days_ahead = (target_weekday - effective_start.weekday()) % 7
    matches = []
    d = effective_start + datetime.timedelta(days=days_ahead)
    while d <= range_end:
        matches.append(d)
        d += datetime.timedelta(days=7)
    return matches


def _unavailability_portion_overlaps_session(portion: str, session_start: str, session_end: str, lunch_start, lunch_end) -> bool:
    """A morning-only absence shouldn't flag a genuinely afternoon session, and vice versa
    - uses the college's own lunch time as the AM/PM boundary, the same convention this
    app already uses for lunch-splitting and mid-session breaks. No lunch time defined at
    all means there's no real boundary to check against - defaults to assuming overlap
    rather than risking silently missing a real clash."""
    if portion == "full_day" or not lunch_start:
        return True
    boundary = _grid_time_to_minutes(lunch_start)
    if portion == "am":
        return _grid_time_to_minutes(session_start) < boundary
    if portion == "pm":
        return _grid_time_to_minutes(session_end) > boundary
    return True


def _grid_time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


# The grid's vertical axis is a fixed, wide display window - independent of the college's
# configured working hours (TimetablingSettings) - covering both normal daytime hours and
# rare evening sessions (e.g. adult education 18:00-21:00). Confirmed directly: using the
# configured working hours as the display range instead would clamp an evening session's
# top_pct to 100%, leaving it a 0%-height block - completely invisible, not just squished.
GRID_DISPLAY_START = "09:00"
GRID_DISPLAY_END = "21:00"


def _generate_time_labels(day_start: str, day_end: str, interval_minutes: int = 15) -> list:
    """
    Every interval_minutes mark from day_start to day_end inclusive, each with its position
    as a percentage of the full range - lets the grid's time gutter show every 15-minute
    mark (09:00, 09:15, 09:30, ...) rather than only the two endpoints, so someone can read
    off a session's exact end time or a mid-session break directly from the axis.
    """
    start_min = _grid_time_to_minutes(day_start)
    end_min = _grid_time_to_minutes(day_end)
    length = max(1, end_min - start_min)
    labels = []
    t = start_min
    while t <= end_min:
        labels.append({"time": f"{t // 60:02d}:{t % 60:02d}", "top_pct": round((t - start_min) / length * 100, 3)})
        t += interval_minutes
    return labels


def compute_grid_positions(sessions: list, day_start: str, day_end: str, lunch_start: str | None, lunch_end: str | None) -> dict:
    """
    Adds top_pct/height_pct to each session dict - its position within its day column, as
    a percentage of the working day - plus a lunch band position if a lunch window is
    configured. The single shared positioning math behind the grid view, used identically
    by the Sessions page (real, committed data) and the Generate preview (in-memory,
    not-yet-saved data), so the two never visually drift apart.

    Clamped to [0, 100] and floored at a minimum 1% height even for very short (e.g.
    30-minute) sessions, so nothing renders invisibly thin or spills outside its column -
    a defensive guard against malformed settings (already blocked by the settings form's
    own validation, but worth protecting directly rather than trusting that alone) as much
    as against genuinely tiny real sessions.

    A session's own end time CAN legitimately run past the configured lunch_start - the
    mid-session break is deliberately allowed to spill slightly into what would otherwise
    be the lunch window (an earlier, established design choice) - so the lunch band and a
    session block may overlap by a few percent. This is correct, not a bug: the caller
    must render the lunch band BEHIND session blocks (lower stacking order) so the real
    class stays fully readable on top of it.

    Expects each session dict to already carry "start" and "end" as "HH:MM" strings -
    everything else on the dict passes through untouched.
    """
    day_start_min = _grid_time_to_minutes(day_start)
    day_end_min = _grid_time_to_minutes(day_end)
    day_length = day_end_min - day_start_min
    if day_length <= 0:
        day_length = 1

    positioned = []
    for s in sessions:
        start_min = _grid_time_to_minutes(s["start"])
        end_min = _grid_time_to_minutes(s["end"])
        top_pct = min(100.0, max(0.0, (start_min - day_start_min) / day_length * 100))
        height_pct = min(100.0 - top_pct, max(1.0, (end_min - start_min) / day_length * 100))
        positioned.append({**s, "top_pct": round(top_pct, 2), "height_pct": round(height_pct, 2)})

    lunch = None
    if lunch_start and lunch_end:
        ls_min = _grid_time_to_minutes(lunch_start)
        le_min = _grid_time_to_minutes(lunch_end)
        l_top = min(100.0, max(0.0, (ls_min - day_start_min) / day_length * 100))
        lunch = {
            "top_pct": round(l_top, 2),
            "height_pct": round(min(100.0 - l_top, max(0.0, (le_min - ls_min) / day_length * 100)), 2),
        }

    return {"sessions": positioned, "lunch": lunch}


def _detect_clashes(sessions: list, db: Session) -> list[dict]:
    """
    Checks the hard constraints from the requirements analysis (section 3) against a set
    of sessions - room/teacher/group double-booking, room capacity, staff qualification,
    staff non-teaching days, room type/equipment matching, and one-off dated
    unavailability. Returns every clash found rather than stopping at the first one, since
    fixing a real timetable benefits from seeing the whole picture at once rather than one
    issue at a time across repeated re-runs.

    Room type/equipment matching was added after CourseUnitRequirement.preferred_room_type
    existed to check against - the generator itself already filters by preferred room type
    when PLACING a new session, but nothing was re-validating it for a session moved
    manually afterwards (e.g. via the grid's move/repair), which could silently put a
    class in the wrong kind of room with no warning at all.

    One-off StaffUnavailability needed the course's own date range to know which actual
    calendar occurrences of a recurring weekly session fall inside a specific unavailable
    date range - genuinely blocked before Course.start_date existed, not an oversight.
    Uses _dates_matching_weekday_in_range to compute the real dates a session's own
    weekday pattern lands on within an unavailability period, and
    _unavailability_portion_overlaps_session so a morning-only absence doesn't wrongly
    flag a genuinely afternoon session. No course start_date set means every date in
    range is treated as possibly live, matching the same "don't assume it's fine" choice
    made elsewhere in this project when a course's own dates aren't known.
    """
    clashes = []
    room_type_labels = dict(ROOM_TYPES)
    requirements_by_key = {
        (r.course_id, r.unit_name): r
        for r in db.query(CourseUnitRequirement).filter(CourseUnitRequirement.preferred_room_type.isnot(None)).all()
    }
    settings_row = get_timetabling_settings(db)
    unavailability_by_teacher = {}
    for u in db.query(StaffUnavailability).all():
        unavailability_by_teacher.setdefault(u.teacher_id, []).append(u)

    for i, s1 in enumerate(sessions):
        for s2 in sessions[i + 1:]:
            if not _times_overlap(s1, s2):
                continue
            if s1.room_id and s1.room_id == s2.room_id:
                clashes.append({
                    "type": "room_double_booked",
                    "message": f"Room double-booked: {s1.room.name if s1.room else '?'} — {s1.day_of_week} {s1.start_time}-{s1.end_time} clashes with {s2.start_time}-{s2.end_time}",
                    "session_ids": [s1.id, s2.id],
                })
            if s1.teacher_id and s1.teacher_id == s2.teacher_id:
                clashes.append({
                    "type": "teacher_double_booked",
                    "message": f"Staff double-booked: {s1.teacher.name if s1.teacher else '?'} — {s1.day_of_week} {s1.start_time}-{s1.end_time} clashes with {s2.start_time}-{s2.end_time}",
                    "session_ids": [s1.id, s2.id],
                })
            if s1.course_id == s2.course_id:
                clashes.append({
                    "type": "group_double_booked",
                    "message": f"Student group double-booked: {s1.course.name if s1.course else '?'} — {s1.day_of_week} {s1.start_time}-{s1.end_time} clashes with {s2.start_time}-{s2.end_time}",
                    "session_ids": [s1.id, s2.id],
                })

    for s in sessions:
        if s.room_id and s.room and s.course:
            student_count = len(s.course.students)
            if student_count > s.room.capacity:
                clashes.append({
                    "type": "room_too_small",
                    "message": f"{s.room.name} (capacity {s.room.capacity}) is too small for {s.course.name} ({student_count} students) — {s.day_of_week} {s.start_time}",
                    "session_ids": [s.id],
                })
        if s.teacher_id and s.unit_name and s.course and s.course.spec_version_id:
            qualified = (
                db.query(TeacherUnitQualification)
                .filter(TeacherUnitQualification.teacher_id == s.teacher_id,
                        TeacherUnitQualification.spec_version_id == s.course.spec_version_id,
                        TeacherUnitQualification.unit_name == s.unit_name)
                .first()
            )
            if not qualified:
                clashes.append({
                    "type": "teacher_not_qualified",
                    "message": f"{s.teacher.name if s.teacher else '?'} isn't marked as qualified for '{s.unit_name}' — {s.day_of_week} {s.start_time}",
                    "session_ids": [s.id],
                })
        if s.teacher_id and s.teacher and s.day_of_week in (s.teacher.non_teaching_days or []):
            clashes.append({
                "type": "teacher_non_teaching_day",
                "message": f"{s.teacher.name} has {s.day_of_week} marked as a non-teaching day — session at {s.start_time}",
                "session_ids": [s.id],
            })
        if s.room_id and s.room and s.unit_name:
            req = requirements_by_key.get((s.course_id, s.unit_name))
            if req and req.preferred_room_type and s.room.room_type != req.preferred_room_type:
                have = room_type_labels.get(s.room.room_type, s.room.room_type)
                need = room_type_labels.get(req.preferred_room_type, req.preferred_room_type)
                clashes.append({
                    "type": "wrong_room_type",
                    "message": f"{s.room.name} is a {have}, but {s.unit_name} needs a {need} — {s.day_of_week} {s.start_time}",
                    "session_ids": [s.id],
                })
        if s.teacher_id and s.teacher:
            course_start = s.course.start_date if s.course else None
            for u in unavailability_by_teacher.get(s.teacher_id, []):
                matching_dates = _dates_matching_weekday_in_range(s.day_of_week, u.start_date, u.end_date, course_start)
                if not matching_dates:
                    continue
                if not _unavailability_portion_overlaps_session(u.portion, s.start_time, s.end_time, settings_row.lunch_start, settings_row.lunch_end):
                    continue
                date_desc = matching_dates[0].strftime("%a %d %b %Y")
                if len(matching_dates) > 1:
                    date_desc += f" and {len(matching_dates) - 1} more date(s)"
                clashes.append({
                    "type": "teacher_unavailable_date",
                    "message": f"{s.teacher.name} is marked unavailable on {date_desc} — clashes with their {s.day_of_week} {s.start_time} session",
                    "session_ids": [s.id],
                })

    return clashes


@router.get("/timetabling/sessions", response_class=HTMLResponse)
def list_sessions(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    sessions = db.query(TimetableSession).order_by(TimetableSession.day_of_week, TimetableSession.start_time).all()
    clashes = _detect_clashes(sessions, db)
    clashed_session_ids = {sid for c in clashes for sid in c["session_ids"]}

    courses = db.query(Course).order_by(Course.name).all()
    rooms = db.query(Room).filter(Room.is_active == True).order_by(Room.name).all()
    staff = db.query(Teacher).filter(Teacher.is_active == True).order_by(Teacher.name).all()

    settings_row = get_timetabling_settings(db)
    bands = [(b.contracted_hours, b.max_teaching_hours) for b in db.query(TeachingHoursBand).order_by(TeachingHoursBand.contracted_hours).all()]

    # Allocated hours reuses the generator's own _actual_teaching_minutes (clock time
    # minus any lunch overlap) so what's shown here always matches what the generator
    # itself uses for a teacher's weekly ceiling check - never a second, drifting
    # calculation of the same thing.
    teacher_hours = {}
    for t in staff:
        allocated_minutes = sum(
            timetable_generator._actual_teaching_minutes(s.start_time, s.end_time, settings_row.lunch_start, settings_row.lunch_end)
            for s in sessions if s.teacher_id == t.id
        )
        max_hours = None
        if t.contracted_hours_per_week is not None:
            max_hours, _ = calculate_teaching_hours(t.contracted_hours_per_week, bands)
        teacher_hours[t.id] = {"allocated": round(allocated_minutes / 60, 1), "max": max_hours}

    grid_raw = [
        {
            "session_id": s.id, "day": s.day_of_week, "start": s.start_time, "end": s.end_time,
            "course_name": s.course.name if s.course else "?", "unit_name": s.unit_name, "label": s.label,
            "teacher_name": s.teacher.name if s.teacher else None, "room_name": s.room.name if s.room else None,
            "is_clash": s.id in clashed_session_ids, "shortfall_reason": None,
            "course_id": s.course_id, "teacher_id": s.teacher_id, "room_id": s.room_id,
        }
        for s in sessions
    ]
    grid_positioned = compute_grid_positions(grid_raw, GRID_DISPLAY_START, GRID_DISPLAY_END, settings_row.lunch_start, settings_row.lunch_end)
    grid_time_labels = _generate_time_labels(GRID_DISPLAY_START, GRID_DISPLAY_END, 15)

    return templates.TemplateResponse(
        "timetabling_sessions.html",
        {"request": request, "teacher": teacher, "sessions": sessions, "clashes": clashes,
         "clashed_session_ids": clashed_session_ids, "courses": courses, "rooms": rooms,
         "staff": staff, "weekdays": DAYS_OF_WEEK,
         "grid_sessions": grid_positioned["sessions"], "grid_lunch": grid_positioned["lunch"],
         "grid_courses": courses, "grid_teachers": staff, "grid_rooms": rooms,
         "grid_weekdays": DAYS_OF_WEEK, "grid_day_start": GRID_DISPLAY_START,
         "grid_day_end": GRID_DISPLAY_END, "grid_time_labels": grid_time_labels,
         "grid_teacher_hours": teacher_hours,
         "edit_error": request.query_params.get("edit_error"),
         "grid_editable": True},
    )


@router.post("/timetabling/sessions/create")
def create_session(
    course_id: int = Form(...),
    day_of_week: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    teacher_id: str = Form(""),
    room_id: str = Form(""),
    unit_name: str = Form(""),
    label: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    if day_of_week not in DAYS_OF_WEEK or end_time <= start_time:
        return RedirectResponse(url="/timetabling/sessions", status_code=303)
    session = TimetableSession(
        course_id=course_id,
        teacher_id=int(teacher_id) if teacher_id.isdigit() else None,
        room_id=int(room_id) if room_id.isdigit() else None,
        day_of_week=day_of_week, start_time=start_time, end_time=end_time,
        unit_name=unit_name.strip() or None, label=label.strip() or None,
    )
    db.add(session)
    db.commit()
    return RedirectResponse(url="/timetabling/sessions", status_code=303)


@router.post("/timetabling/sessions/{session_id}/edit")
def edit_session(
    session_id: int,
    course_id: int = Form(...),
    day_of_week: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    teacher_id: str = Form(""),
    room_id: str = Form(""),
    unit_name: str = Form(""),
    label: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    session = db.get(TimetableSession, session_id)
    if not session or day_of_week not in DAYS_OF_WEEK or end_time <= start_time:
        return RedirectResponse(url="/timetabling/sessions", status_code=303)

    new_teacher_id = int(teacher_id) if teacher_id.isdigit() else None
    new_room_id = int(room_id) if room_id.isdigit() else None
    session.course = db.get(Course, course_id)
    session.teacher = db.get(Teacher, new_teacher_id) if new_teacher_id else None
    session.room = db.get(Room, new_room_id) if new_room_id else None
    session.day_of_week = day_of_week
    session.start_time = start_time
    session.end_time = end_time
    session.unit_name = unit_name.strip() or None
    session.label = label.strip() or None

    other_sessions = db.query(TimetableSession).filter(TimetableSession.id != session_id).all()
    clashes = _detect_clashes(other_sessions + [session], db)
    relevant_clashes = [c for c in clashes if session_id in c["session_ids"]]
    if relevant_clashes:
        db.rollback()
        message = "Not saved - " + " / ".join(c["message"] for c in relevant_clashes)
        return RedirectResponse(url=f"/timetabling/sessions?edit_error={quote(message)}", status_code=303)

    db.commit()
    return RedirectResponse(url="/timetabling/sessions", status_code=303)


@router.post("/timetabling/sessions/{session_id}/delete")
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    session = db.get(TimetableSession, session_id)
    if session:
        db.delete(session)
        db.commit()
    return RedirectResponse(url="/timetabling/sessions", status_code=303)


@router.get("/timetabling/requirements", response_class=HTMLResponse)
def list_requirements(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    courses = db.query(Course).order_by(Course.name).all()
    sites = db.query(Site).filter(Site.is_active == True).order_by(Site.name).all()
    requirements = db.query(CourseUnitRequirement).order_by(CourseUnitRequirement.course_id).all()
    requirements_by_course = {}
    requirement_by_key = {}
    for r in requirements:
        requirements_by_course.setdefault(r.course_id, []).append(r)
        requirement_by_key[(r.course_id, r.unit_name)] = r

    # Per-course unit options, same pattern as TeacherUnitQualification's spec_unit_groups -
    # real unit names from that course's own spec, not free text.
    unit_options_by_course = {}
    for c in courses:
        if c.spec_version and c.spec_version.structured_json:
            unit_options_by_course[c.id] = [u.get("name", "?") for u in c.spec_version.structured_json.get("units", [])]
        else:
            unit_options_by_course[c.id] = []

    # A real sanity check before any generation runs: can the available staff even cover
    # what's being asked for, in principle, before worrying about which specific day/room?
    # Broken down per course (confirmed as a real need with 5+ courses on one page - a
    # single aggregate number doesn't say WHICH course is driving a shortfall) and per
    # staff member (confirmed as a real need too - an aggregate total can hide a data-entry
    # mistake, like a contracted-hours value that's actually someone's teaching hours typed
    # into the wrong field, silently triggering an extrapolated estimate no one can see).
    hours_needed_by_course = {}
    for r in requirements:
        hours_needed_by_course[r.course_id] = hours_needed_by_course.get(r.course_id, 0) + r.hours_per_week
    total_hours_needed = sum(hours_needed_by_course.values())

    bands = [(b.contracted_hours, b.max_teaching_hours) for b in db.query(TeachingHoursBand).all()]
    capacity_by_staff = []
    total_teaching_capacity = 0.0
    capacity_known = False
    for person in db.query(Teacher).filter(Teacher.is_active == True).order_by(Teacher.name).all():
        if person.contracted_hours_per_week is not None:
            hours, is_estimate = calculate_teaching_hours(person.contracted_hours_per_week, bands)
            if hours is not None:
                total_teaching_capacity += hours
                capacity_known = True
                capacity_by_staff.append({
                    "name": person.name, "contracted_hours": person.contracted_hours_per_week,
                    "teaching_hours": round(hours, 2), "is_estimate": is_estimate,
                })

    return templates.TemplateResponse(
        "timetabling_requirements.html",
        {"request": request, "teacher": teacher, "courses": courses, "sites": sites,
         "requirements_by_course": requirements_by_course, "requirement_by_key": requirement_by_key,
         "unit_options_by_course": unit_options_by_course,
         "weekdays": DAYS_OF_WEEK, "room_types": ROOM_TYPES,
         "hours_needed_by_course": hours_needed_by_course, "capacity_by_staff": capacity_by_staff,
         "save_warning": request.query_params.get("save_warning"),
         "total_hours_needed": round(total_hours_needed, 2), "total_teaching_capacity": round(total_teaching_capacity, 2),
         "capacity_known": capacity_known},
    )


@router.post("/timetabling/requirements/save")
async def save_requirements(
    request: Request,
    course_id: int = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """
    Saves every row for a course's requirements form in one submission - confirmed as a
    real need: adding units one at a time, one full form submission each, was real,
    avoidable friction for a course that (like a real T-Level example given) can have
    several units each needing their own hours and day preferences at once.

    Upserts by (course_id, unit_name) rather than always inserting - re-submitting the
    same form (e.g. after tweaking one unit's hours) updates what's there instead of
    creating a duplicate. A row left blank for hours removes any existing requirement for
    that unit - clearing a row is a real way to delete something, not just a no-op.

    Row 0 is always "whole course / no specific unit" (unit_name=None) - a tutorial slot
    or a general requirement that isn't tied to one specific spec unit. Rows 1..N
    correspond to the course's own real unit names, in the same order the form was
    rendered with, re-derived here from the course's own spec rather than trusting
    anything the client sent, so a row can never be spoofed into a unit name that isn't
    genuinely part of this course.
    """
    course = db.get(Course, course_id)
    if not course:
        return RedirectResponse(url="/timetabling/requirements", status_code=303)
    unit_names = []
    if course.spec_version and course.spec_version.structured_json:
        unit_names = [u.get("name", "?") for u in course.spec_version.structured_json.get("units", [])]
    row_unit_names = [None] + unit_names

    existing_by_unit = {
        r.unit_name: r
        for r in db.query(CourseUnitRequirement).filter(CourseUnitRequirement.course_id == course_id).all()
    }

    form = await request.form()
    invalid_rows = []
    for i, unit_name in enumerate(row_unit_names):
        hours_str = form.get(f"hours_per_week_{i}", "").strip()
        existing = existing_by_unit.get(unit_name)
        hours = None
        if hours_str:
            try:
                hours = float(hours_str)
            except ValueError:
                hours = None
        if not hours or hours <= 0:
            # Blank rows are an intentional skip - no message needed. Something was
            # actually TYPED but came out invalid (zero, negative, not a number) is
            # different - that's very likely a mistake, not a deliberate choice, and
            # deserves a real message rather than a silent skip.
            if hours_str:
                row_label = unit_name or "Whole course / no specific unit"
                invalid_rows.append(row_label)
            if existing:
                db.delete(existing)
            continue

        preferred_days_count_str = form.get(f"preferred_days_count_{i}", "").strip()
        preferred_days = form.getlist(f"preferred_days_{i}") or None
        preferred_room_type = form.get(f"preferred_room_type_{i}", "").strip() or None
        preferred_building = form.get(f"preferred_building_{i}", "").strip() or None
        notes = form.get(f"notes_{i}", "").strip() or None
        parsed_days_count = int(preferred_days_count_str) if preferred_days_count_str.isdigit() else None

        if existing:
            existing.hours_per_week = hours
            existing.preferred_days_count = parsed_days_count
            existing.preferred_days = preferred_days
            existing.preferred_room_type = preferred_room_type
            existing.preferred_building = preferred_building
            existing.notes = notes
        else:
            db.add(CourseUnitRequirement(
                course_id=course_id, unit_name=unit_name, hours_per_week=hours,
                preferred_days_count=parsed_days_count, preferred_days=preferred_days,
                preferred_room_type=preferred_room_type, preferred_building=preferred_building, notes=notes,
            ))
    db.commit()
    if invalid_rows:
        message = f"Ignored invalid hours (must be a number greater than 0) for: {', '.join(invalid_rows)}. Everything else was saved."
        return RedirectResponse(url=f"/timetabling/requirements?save_warning={quote(message)}#course-{course_id}", status_code=303)
    return RedirectResponse(url=f"/timetabling/requirements#course-{course_id}", status_code=303)


@router.post("/timetabling/requirements/{requirement_id}/delete")
def delete_requirement(
    requirement_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    r = db.get(CourseUnitRequirement, requirement_id)
    if r:
        db.delete(r)
        db.commit()
    return RedirectResponse(url="/timetabling/requirements", status_code=303)


def _build_generation_inputs(db: Session):
    """
    Pulls everything the generator needs from the database and translates it into the
    plain-dict format timetable_generator.py expects, keeping that module pure/DB-free so
    its logic stays easy to test in isolation from the app itself.
    """
    settings_row = get_timetabling_settings(db)
    settings = {
        "start": settings_row.working_day_start, "end": settings_row.working_day_end,
        "lunch_start": settings_row.lunch_start, "lunch_end": settings_row.lunch_end,
        "break_minutes": settings_row.min_break_minutes, "break_after_hours": settings_row.break_after_hours,
    }

    bands = [(b.contracted_hours, b.max_teaching_hours) for b in db.query(TeachingHoursBand).all()]

    qual_rows = db.query(TeacherUnitQualification).filter(TeacherUnitQualification.status.in_(["confirmed", "pending_removal"])).all()
    qualifications_by_teacher = {}
    qual_row_by_key = {}
    for q in qual_rows:
        qualifications_by_teacher.setdefault(q.teacher_id, set()).add((q.spec_version_id, q.unit_name))
        qual_row_by_key[(q.teacher_id, q.spec_version_id, q.unit_name)] = q

    # General CPD (no unit_name set) is genuinely relevant across everything a teacher is
    # qualified for; unit-specific CPD only counts toward that one unit - see
    # skills_ranking_requirements_analysis.md section 4.3 for why this distinction matters.
    general_cpd_by_teacher = {}
    specific_cpd_by_teacher_unit = {}
    for c in db.query(TeacherCPDRecord).all():
        entry = {"date": c.date}
        if c.unit_name is None:
            general_cpd_by_teacher.setdefault(c.teacher_id, []).append(entry)
        else:
            specific_cpd_by_teacher_unit.setdefault((c.teacher_id, c.spec_version_id, c.unit_name), []).append(entry)

    iv_by_teacher_unit = {}
    for iv in db.query(TeacherIVOutcome).all():
        iv_by_teacher_unit.setdefault((iv.teacher_id, iv.spec_version_id, iv.unit_name), []).append({"date": iv.date, "outcome_level": iv.outcome_level})

    weights_row = get_skill_ranking_weights(db)
    weights = {
        "weight_experience": weights_row.weight_experience, "weight_admin_rating": weights_row.weight_admin_rating,
        "weight_cpd": weights_row.weight_cpd, "weight_iv": weights_row.weight_iv,
        "cpd_half_life_years": weights_row.cpd_half_life_years, "iv_half_life_years": weights_row.iv_half_life_years,
    }

    teachers = []
    for t in db.query(Teacher).filter(Teacher.is_active == True).all():
        max_hours = None
        if t.contracted_hours_per_week is not None:
            hours, _ = calculate_teaching_hours(t.contracted_hours_per_week, bands)
            max_hours = hours
        skill_scores = {}
        for (spec_version_id, unit_name) in qualifications_by_teacher.get(t.id, set()):
            qual = qual_row_by_key.get((t.id, spec_version_id, unit_name))
            cpd = general_cpd_by_teacher.get(t.id, []) + specific_cpd_by_teacher_unit.get((t.id, spec_version_id, unit_name), [])
            iv = iv_by_teacher_unit.get((t.id, spec_version_id, unit_name), [])
            skill_scores[(spec_version_id, unit_name)] = skill_ranking.score_teacher_for_unit(
                qual.years_experience if qual else None, qual.admin_rating if qual else None, cpd, iv, weights,
            )
        teachers.append({
            "id": t.id, "non_teaching_days": t.non_teaching_days or [],
            "max_teaching_hours": max_hours,
            "qualified_units": qualifications_by_teacher.get(t.id, set()),
            "skill_scores": skill_scores,
        })

    rooms = [
        {"id": r.id, "capacity": r.capacity, "room_type": r.room_type, "building": r.building}
        for r in db.query(Room).filter(Room.is_active == True).all()
    ]

    requirements = []
    for req in db.query(CourseUnitRequirement).all():
        course = db.get(Course, req.course_id)
        if not course:
            continue
        requirements.append({
            "course_id": req.course_id, "unit_name": req.unit_name,
            "spec_version_id": course.spec_version_id, "hours_per_week": req.hours_per_week,
            "preferred_days_count": req.preferred_days_count, "preferred_days": req.preferred_days,
            "preferred_room_type": req.preferred_room_type, "preferred_building": req.preferred_building,
            "student_count": len([s for s in course.students if s.is_active]),
        })

    existing_sessions = [
        {
            "course_id": s.course_id, "unit_name": s.unit_name, "teacher_id": s.teacher_id, "room_id": s.room_id,
            "day": s.day_of_week, "start": s.start_time, "end": s.end_time,
        }
        for s in db.query(TimetableSession).all()
    ]

    return requirements, teachers, rooms, settings, existing_sessions


@router.get("/timetabling/generate", response_class=HTMLResponse)
def generate_timetable_page(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    requirement_count = db.query(CourseUnitRequirement).count()
    existing_session_count = db.query(TimetableSession).count()
    active_room_count = db.query(Room).filter(Room.is_active == True).count()
    active_staff_count = db.query(Teacher).filter(Teacher.is_active == True).count()
    return templates.TemplateResponse(
        "timetabling_generate.html",
        {"request": request, "teacher": teacher, "requirement_count": requirement_count,
         "existing_session_count": existing_session_count, "active_room_count": active_room_count,
         "active_staff_count": active_staff_count, "preview": None},
    )


@router.post("/timetabling/generate/clear-sessions")
def clear_all_sessions(
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """A deliberate, explicit reset - useful while testing generation repeatedly, since
    earlier test-commits otherwise stay in place and can end up claiming every available
    room or staff slot across the week, leaving nothing for a fresh generation run to use."""
    db.query(TimetableSession).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url="/timetabling/generate", status_code=303)


@router.post("/timetabling/generate/preview", response_class=HTMLResponse)
def generate_timetable_preview(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """
    Runs the generator entirely in memory - nothing is written to the database here. The
    generated sessions are shown for review and encoded as JSON in a hidden field, which
    the commit route below reads back to actually create them, only once explicitly
    confirmed.
    """
    requirements, teachers, rooms, settings, existing_sessions = _build_generation_inputs(db)
    result = timetable_generator.generate_timetable(requirements, teachers, rooms, settings, existing_sessions)

    rooms_full = {r.id: r for r in db.query(Room).all()}
    teachers_full = {t.id: t for t in db.query(Teacher).all()}
    courses_full = {c.id: c for c in db.query(Course).all()}

    preview_sessions = []
    for s in result["placed"]:
        course = courses_full.get(s["course_id"])
        t_obj = teachers_full.get(s["teacher_id"]) if s["teacher_id"] else None
        r_obj = rooms_full.get(s["room_id"]) if s["room_id"] else None
        preview_sessions.append({
            "course_name": course.name if course else "?", "unit_name": s["unit_name"],
            "day": s["day"], "start": s["start"], "end": s["end"],
            "teacher_name": t_obj.name if t_obj else None,
            "room_name": r_obj.name if r_obj else None,
            "room_shortfall_reason": s.get("room_shortfall_reason"),
            "course_id": s["course_id"], "teacher_id": s["teacher_id"], "room_id": s["room_id"],
        })

    grid_raw = [{**ps, "is_clash": False, "shortfall_reason": ps.get("room_shortfall_reason")} for ps in preview_sessions]
    grid_positioned = compute_grid_positions(grid_raw, GRID_DISPLAY_START, GRID_DISPLAY_END, settings.get("lunch_start"), settings.get("lunch_end"))
    grid_time_labels = _generate_time_labels(GRID_DISPLAY_START, GRID_DISPLAY_END, 15)
    grid_course_list = list(courses_full.values())
    grid_teacher_list = [t for t in teachers_full.values() if t.is_active]
    grid_room_list = [r for r in rooms_full.values() if r.is_active]

    unplaced_display = []
    for u in result["unplaced"]:
        course = courses_full.get(u["course_id"])
        unplaced_display.append({
            "course_name": course.name if course else "?", "unit_name": u["unit_name"],
            "block_hours": u["block_hours"], "reason": u["reason"],
        })

    already_satisfied_display = []
    for a in result.get("already_satisfied", []):
        course = courses_full.get(a["course_id"])
        already_satisfied_display.append({
            "course_name": course.name if course else "?", "unit_name": a["unit_name"],
            "hours_per_week": a["hours_per_week"],
        })

    return templates.TemplateResponse(
        "timetabling_generate.html",
        {"request": request, "teacher": teacher,
         "requirement_count": len(requirements), "existing_session_count": len(existing_sessions),
         "active_room_count": len(rooms), "active_staff_count": len(teachers),
         "preview": {
             "sessions": preview_sessions, "needs_teacher_count": result["needs_teacher_count"],
             "needs_room_count": result["needs_room_count"], "unplaced": unplaced_display,
             "already_satisfied": already_satisfied_display,
             "sessions_json": json.dumps(result["placed"]),
         },
         "grid_sessions": grid_positioned["sessions"], "grid_lunch": grid_positioned["lunch"],
         "grid_courses": grid_course_list, "grid_teachers": grid_teacher_list, "grid_rooms": grid_room_list,
         "grid_weekdays": DAYS_OF_WEEK, "grid_day_start": GRID_DISPLAY_START, "grid_day_end": GRID_DISPLAY_END,
         "grid_time_labels": grid_time_labels},
    )


@router.post("/timetabling/generate/commit")
def commit_generated_timetable(
    sessions_json: str = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_timetabling_access),
):
    """Writes the previewed sessions as real TimetableSession rows - the only place in
    this whole flow that actually touches the database, and only reached by an explicit
    confirmation from the preview page."""
    try:
        sessions = json.loads(sessions_json)
    except (ValueError, TypeError):
        return RedirectResponse(url="/timetabling/generate", status_code=303)
    for s in sessions:
        db.add(TimetableSession(
            course_id=s["course_id"], teacher_id=s.get("teacher_id"), room_id=s.get("room_id"),
            day_of_week=s["day"], start_time=s["start"], end_time=s["end"], unit_name=s.get("unit_name"),
        ))
    db.commit()
    return RedirectResponse(url="/timetabling/sessions", status_code=303)


_DUMMY_FIRST_NAMES = ["Alex", "Jordan", "Sam", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Quinn", "Avery",
                      "Drew", "Reese", "Charlie", "Skyler", "Finley"]
_DUMMY_LAST_NAMES = ["Smith", "Jones", "Patel", "Khan", "Brown", "Wilson", "Davies", "Evans", "Clarke", "Hughes",
                     "Murphy", "Ahmed", "Walsh", "Singh", "Turner"]
_DUMMY_NON_TEACHING_PATTERNS = [
    [], ["Friday"], ["Monday"], ["Tuesday", "Wednesday"], ["Thursday", "Friday"],
    [], ["Wednesday"], ["Monday", "Friday"], [], ["Tuesday"],
]
_DUMMY_HOURS_PATTERNS = [35, 30, 25, 20, 16, 12, 35, 28, 22, 18]


@router.post("/timetabling/dummy-data/generate")
def generate_dummy_data(
    teacher_count: int = Form(10),
    room_count: int = Form(5),
    heavy_load: str = Form(""),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Bulk-generates clearly-labelled test staff and rooms for exercising the generator at
    scale - every record is flagged is_dummy=True so it can be cleared completely and
    safely afterward via the companion clear route below, with zero risk of ever touching
    real data (never identified by name-matching, which could collide with something real).

    Qualifications are drawn from real, currently-existing course requirements (their
    course's spec_version_id paired with the requirement's own unit_name) so dummy
    teachers are actually placeable by the generator against real data, not just inert
    rows sitting in the staff list.

    heavy_load switches from the default varied/realistic pattern to a fully-loaded one:
    every dummy teacher covers the WHOLE qualification pool (not a random 1-3 subset),
    contracted hours sit near full-time, and days off are minimal - useful specifically
    because a small real-requirements pool otherwise produces sparse, thin dummy coverage
    that doesn't really exercise the generator's choices (qualification matching,
    load-balancing, room contention) the way a fully-staffed real department would.

    Admin-only: this is a bulk, destructive-adjacent testing tool, not everyday
    timetabling work.
    """
    real_reqs = db.query(CourseUnitRequirement).filter(CourseUnitRequirement.unit_name.isnot(None)).all()
    qualification_pool = set()
    for req in real_reqs:
        course = db.get(Course, req.course_id)
        if course:
            qualification_pool.add((course.spec_version_id, req.unit_name))
    qualification_pool = list(qualification_pool)
    is_heavy = bool(heavy_load)

    for i in range(max(0, teacher_count)):
        first = _DUMMY_FIRST_NAMES[i % len(_DUMMY_FIRST_NAMES)]
        last = _DUMMY_LAST_NAMES[i % len(_DUMMY_LAST_NAMES)]
        unique = uuid.uuid4().hex[:10]
        if is_heavy:
            contracted_hours = random.choice([32, 35, 37])
            non_teaching_days = random.choice([[], [], ["Friday"]])
        else:
            contracted_hours = _DUMMY_HOURS_PATTERNS[i % len(_DUMMY_HOURS_PATTERNS)]
            non_teaching_days = _DUMMY_NON_TEACHING_PATTERNS[i % len(_DUMMY_NON_TEACHING_PATTERNS)]
        t = Teacher(
            name=f"[DUMMY] {first} {last}",
            email=f"dummy.{unique}@test.invalid",
            hashed_password=hash_password(uuid.uuid4().hex),  # random, never communicated - these accounts are never meant to be logged into
            role="lecturer",
            contracted_hours_per_week=contracted_hours,
            non_teaching_days=non_teaching_days,
            is_dummy=True,
        )
        db.add(t)
        db.flush()  # assigns t.id without a full commit, needed for the qualification rows below
        if qualification_pool:
            if is_heavy:
                units_to_assign = qualification_pool  # full department coverage, not a thin random slice
            else:
                qual_count = random.randint(1, min(3, len(qualification_pool)))
                units_to_assign = random.sample(qualification_pool, qual_count)
            for spec_version_id, unit_name in units_to_assign:
                db.add(TeacherUnitQualification(teacher_id=t.id, spec_version_id=spec_version_id, unit_name=unit_name))

    room_type_keys = [key for key, _ in ROOM_TYPES]
    for i in range(max(0, room_count)):
        db.add(Room(
            name=f"[DUMMY] Test Room {i + 1}",
            capacity=random.choice([15, 20, 25, 30]),
            room_type=random.choice(room_type_keys),
            is_dummy=True,
        ))

    db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)


@router.post("/timetabling/dummy-data/generate-requirements")
def generate_dummy_requirements(
    requirement_count: int = Form(15),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Adds a batch of dummy course-unit requirements for real scale-testing - a small
    handful of real requirements can't meaningfully exercise the generator's behaviour
    under load (multi-day spreading, room contention, qualification bottlenecks across
    many simultaneous demands at once).

    Spread across multiple dummy courses (~5 units each, a realistic course size) rather
    than one single course - confirmed as a real, necessary fix after actual stress
    testing: course-level conflict checking correctly treats every unit within the same
    course as sharing one student cohort that can only be in one class at a time, which is
    the right behaviour for a real course, but cramming 25+ units into a SINGLE course
    made it mathematically impossible to schedule most of them - not a staffing or room
    shortage, just one course fighting itself for space in the week. Every dummy course
    is flagged is_dummy=True, cleanly isolated from and never confused with a real course's
    actual curriculum.

    Reuses a real spec_version_id from an existing real course for every dummy course
    created, since qualification matching is scoped to (spec_version_id, unit_name) pairs -
    a dummy course with no real spec attached would produce requirements no teacher could
    ever be qualified for. Existing dummy teachers (however many, from whichever earlier
    generation run created them) are retroactively given a varied, randomised subset of
    qualifications for each new unit, so the new requirements are actually placeable, not
    just inert rows sitting on the requirements page.
    """
    UNITS_PER_COURSE = 5

    real_course = db.query(Course).filter(Course.spec_version_id.isnot(None), Course.is_dummy == False).first()
    real_spec_version_id = real_course.spec_version_id if real_course else None

    existing_dummy_courses = db.query(Course).filter(Course.is_dummy == True).order_by(Course.id).all()
    existing_unit_count = (
        db.query(CourseUnitRequirement)
        .join(Course, CourseUnitRequirement.course_id == Course.id)
        .filter(Course.is_dummy == True)
        .count()
    )
    dummy_teachers = db.query(Teacher).filter(Teacher.is_dummy == True).all()
    room_type_keys = [key for key, _ in ROOM_TYPES]

    current_course = existing_dummy_courses[-1] if existing_dummy_courses else None
    current_course_unit_count = (
        db.query(CourseUnitRequirement).filter(CourseUnitRequirement.course_id == current_course.id).count()
        if current_course else 0
    )

    new_units = []  # (spec_version_id, unit_name) pairs, for the retroactive-qualification pass below
    for i in range(max(0, requirement_count)):
        if current_course is None or current_course_unit_count >= UNITS_PER_COURSE:
            course_number = len(existing_dummy_courses) + 1
            current_course = Course(
                teacher_id=admin.id, name=f"[DUMMY] Stress-Test Course {course_number}",
                spec_version_id=real_spec_version_id, is_dummy=True,
            )
            db.add(current_course)
            db.flush()
            existing_dummy_courses.append(current_course)
            current_course_unit_count = 0

        unit_name = f"[DUMMY] Unit {existing_unit_count + i + 1}"
        db.add(CourseUnitRequirement(
            course_id=current_course.id, unit_name=unit_name,
            hours_per_week=random.choice([2, 3, 4, 6]),
            preferred_days_count=random.choice([1, 2, 3]),
            preferred_room_type=random.choice(room_type_keys) if random.random() < 0.3 else None,
        ))
        new_units.append((current_course.spec_version_id, unit_name))
        current_course_unit_count += 1

    if dummy_teachers:
        for spec_version_id, unit_name in new_units:
            if not spec_version_id:
                continue
            qualified_subset = random.sample(dummy_teachers, k=max(1, len(dummy_teachers) // 3))
            for t in qualified_subset:
                db.add(TeacherUnitQualification(teacher_id=t.id, spec_version_id=spec_version_id, unit_name=unit_name))

    db.commit()
    return RedirectResponse(url="/timetabling/requirements", status_code=303)


@router.post("/timetabling/dummy-data/clear")
def clear_dummy_data(
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Removes every dummy-flagged teacher, room, and course (with its requirements)
    completely, along with qualifications and any course-access rows - and, for any real
    generated session that ended up using a dummy teacher or room, nulls out just that
    reference rather than deleting the session itself, so the real day/time/course
    placement stays intact and simply needs a real teacher or room reassigned. A session
    that belongs to a dummy COURSE, though, is deleted outright rather than nulled -
    there's no real course left for it to belong to once the course itself is gone.
    Matches the same "never silently break, always flag" approach already used for the
    room-delete route.
    """
    dummy_teacher_ids = [row.id for row in db.query(Teacher.id).filter(Teacher.is_dummy == True).all()]
    dummy_room_ids = [row.id for row in db.query(Room.id).filter(Room.is_dummy == True).all()]
    dummy_course_ids = [row.id for row in db.query(Course.id).filter(Course.is_dummy == True).all()]

    if dummy_course_ids:
        db.query(TimetableSession).filter(TimetableSession.course_id.in_(dummy_course_ids)).delete(synchronize_session=False)
        db.query(CourseUnitRequirement).filter(CourseUnitRequirement.course_id.in_(dummy_course_ids)).delete(synchronize_session=False)
        db.query(CourseTeacher).filter(CourseTeacher.course_id.in_(dummy_course_ids)).delete(synchronize_session=False)
        db.query(Course).filter(Course.id.in_(dummy_course_ids)).delete(synchronize_session=False)

    if dummy_teacher_ids:
        db.query(TeacherUnitQualification).filter(TeacherUnitQualification.teacher_id.in_(dummy_teacher_ids)).delete(synchronize_session=False)
        db.query(StaffUnavailability).filter(StaffUnavailability.teacher_id.in_(dummy_teacher_ids)).delete(synchronize_session=False)
        db.query(CourseTeacher).filter(CourseTeacher.teacher_id.in_(dummy_teacher_ids)).delete(synchronize_session=False)
        db.query(TimetableSession).filter(TimetableSession.teacher_id.in_(dummy_teacher_ids)).update({"teacher_id": None}, synchronize_session=False)
        db.query(Teacher).filter(Teacher.id.in_(dummy_teacher_ids)).delete(synchronize_session=False)

    if dummy_room_ids:
        db.query(TimetableSession).filter(TimetableSession.room_id.in_(dummy_room_ids)).update({"room_id": None}, synchronize_session=False)
        db.query(Room).filter(Room.id.in_(dummy_room_ids)).delete(synchronize_session=False)

    db.commit()
    return RedirectResponse(url="/timetabling/staff", status_code=303)
