from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Course, Student
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.post("/courses/{course_id}/students/create")
def create_student(
    course_id: int,
    name: str = Form(...),
    student_ref: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = get_course_for_teacher(db, course_id, teacher)
    if course:
        student = Student(course_id=course.id, name=name, student_ref=student_ref or None)
        db.add(student)
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


@router.get("/courses/{course_id}/students/{student_id}", response_class=HTMLResponse)
def student_detail(
    course_id: int,
    student_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = get_course_for_teacher(db, course_id, teacher)
    student = db.query(Student).filter(Student.id == student_id, Student.course_id == course_id).first()
    has_parsed_spec = bool(course and course.spec_version and course.spec_version.structured_json)
    return templates.TemplateResponse(
        "student_detail.html",
        {"request": request, "teacher": teacher, "course": course, "student": student, "has_parsed_spec": has_parsed_spec},
    )


@router.post("/courses/{course_id}/students/{student_id}/edit")
def edit_student(
    course_id: int,
    student_id: int,
    name: str = Form(...),
    student_ref: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = get_course_for_teacher(db, course_id, teacher)
    student = db.query(Student).filter(Student.id == student_id, Student.course_id == course_id).first()
    if course and student:
        student.name = name
        student.student_ref = student_ref or None
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}/students/{student_id}", status_code=303)


@router.post("/courses/{course_id}/students/{student_id}/delete")
def delete_student(
    course_id: int,
    student_id: int,
    confirm_name: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Removes a student from the course for good - e.g. added by mistake, or left the college.
    A student with NO submissions is simply removed. One WITH submissions (marks, feedback,
    IV records) needs their name typed to confirm, and all of that work goes too - archiving
    stays the safe choice for anyone whose history should be kept.
    """
    from urllib.parse import quote as q
    from ..models import Submission, AuditLogEntry
    from .submissions import delete_submission
    course = get_course_for_teacher(db, course_id, teacher)
    student = db.query(Student).filter(Student.id == student_id, Student.course_id == course_id).first()
    if not course or not student:
        return RedirectResponse(url="/", status_code=303)
    submissions = db.query(Submission).filter(Submission.student_id == student.id).all()
    if submissions and confirm_name.strip() != student.name:
        message = f"Nothing was deleted - type {student.name}'s name exactly to delete them and their {len(submissions)} submission(s)."
        return RedirectResponse(url=f"/courses/{course_id}/students/{student_id}?error={q(message)}", status_code=303)
    db.add(AuditLogEntry(actor=teacher.email, action="student_deleted",
                         detail=f"Deleted student id={student.id} from course id={course.id} with {len(submissions)} submission(s)."))
    db.commit()
    for sub in submissions:
        delete_submission(sub.id, db, teacher)
    db.delete(db.get(Student, student_id))
    db.commit()
    return RedirectResponse(url=f"/courses/{course_id}?imported={q('Student deleted.')}#students", status_code=303)


@router.post("/courses/{course_id}/students/{student_id}/toggle-active")
def toggle_student_active(
    course_id: int,
    student_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    No hard delete for students - a student with marked submissions has real history
    attached (marks, overrides, IV records). Archiving hides them from the active list
    without breaking any of that.
    """
    course = get_course_for_teacher(db, course_id, teacher)
    student = db.query(Student).filter(Student.id == student_id, Student.course_id == course_id).first()
    if course and student:
        student.is_active = not student.is_active
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


# ------------------------------------------------------------------ class list import (ProSolution)
# Upload (or paste) -> the table is kept for up to an hour in storage/imports (only on this
# server, only for the lecturer who uploaded it) so the column matches and class filter can be
# changed without uploading again -> preview -> import the ticked students -> the file is deleted.

import json as _json
import os as _os
import re as _re
import secrets as _secrets
import time as _time
from urllib.parse import quote as _quote

from fastapi import UploadFile, File
from ..class_list_import import (read_table, read_text, find_header_row, guess_columns, build_rows, groups_in,
                                 plan_import, FIELD_LABELS)

IMPORT_DIR = "storage/imports"
IMPORT_KEEP_SECONDS = 3600
MAX_IMPORT_BYTES = 5 * 1024 * 1024
_TOKEN_RE = _re.compile(r"^[A-Za-z0-9_-]{16,40}$")


def _import_path(course_id, teacher_id, token):
    if not _TOKEN_RE.match(token or ""):
        return None
    return _os.path.join(IMPORT_DIR, f"{int(course_id)}_{int(teacher_id)}_{token}.json")


def _clear_old_imports():
    try:
        for name in _os.listdir(IMPORT_DIR):
            path = _os.path.join(IMPORT_DIR, name)
            if _time.time() - _os.path.getmtime(path) > IMPORT_KEEP_SECONDS:
                _os.remove(path)
    except OSError:
        pass


def _load_import(course_id, teacher_id, token):
    path = _import_path(course_id, teacher_id, token)
    if not path or not _os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return _json.load(f)


def _existing(course):
    return [{"id": s.id, "name": s.name, "ref": s.student_ref or "", "active": s.is_active} for s in course.students]


def _int_or_none(value):
    try:
        return int(value) if str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


@router.get("/courses/{course_id}/class-list/import", response_class=HTMLResponse)
def import_students_page(course_id: int, request: Request, db: Session = Depends(get_db),
                         teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    q = request.query_params
    context = {"request": request, "teacher": teacher, "course": course, "error": q.get("error"), "token": None,
               "labels": FIELD_LABELS}
    token = q.get("t")
    if token:
        stored = _load_import(course.id, teacher.id, token)
        if stored is None:
            context["error"] = "That upload has expired (they are kept for an hour). Please upload the file again."
        else:
            rows, header = stored["rows"], stored["header"]
            headers = rows[header] if 0 <= header < len(rows) else []
            if "mapped" in q:  # the lecturer changed the matches on the preview
                columns = {f: _int_or_none(q.get(f"col_{f}")) for f in FIELD_LABELS}
            else:
                columns = guess_columns(headers)
            people = build_rows(rows, header, columns)
            groups = groups_in(people) if columns.get("group") is not None else []
            if "mapped" in q:
                chosen = set(q.getlist("group"))
            else:  # one class in the file -> take it; several -> the lecturer picks theirs
                chosen = {g for g, _ in groups} if len(groups) <= 1 else set()
            if groups:
                people = [p for p in people if p["group"] in chosen]
            archive = q.get("archive") == "1"
            plan = plan_import(_existing(course), people, archive_missing=archive)
            context.update({"token": token, "headers": headers, "columns": columns, "groups": groups, "chosen": chosen,
                            "archive": archive, "plan": plan, "people_count": len(people), "filename": stored.get("filename", ""),
                            "needs_group": bool(groups) and not chosen,
                            "has_name": any(columns.get(f) is not None for f in ("fullname", "forename", "preferred", "surname"))})
    return templates.TemplateResponse("students_import.html", context)


@router.post("/courses/{course_id}/class-list/import")
async def import_students_upload(course_id: int, request: Request, file: UploadFile = File(None), pasted: str = Form(""),
                                 db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    back = f"/courses/{course.id}/class-list/import"
    filename = ""
    try:
        if file is not None and file.filename:
            data = await file.read(MAX_IMPORT_BYTES + 1)
            if len(data) > MAX_IMPORT_BYTES:
                raise ValueError("That file is over 5 MB - export just your class, or save it as CSV.")
            rows, filename = read_table(file.filename, data), file.filename
        elif pasted.strip():
            rows, filename = read_text(pasted[:MAX_IMPORT_BYTES]), "pasted list"
        else:
            raise ValueError("Choose a file, or paste your class list into the box.")
        header = find_header_row(rows)
        if header < 0:
            if rows and len(rows[0]) <= 2:
                # a plain pasted list with no headings: "Name" or "Name, Ref" per line
                rows = [["Name", "Student ref"][:max(1, len(rows[0]))]] + rows
                header = 0
            else:
                raise ValueError("Couldn't find the column headings (like Forename, Surname, Student Ref). "
                                 "Make sure the export includes its heading row.")
        if len(rows) - header - 1 < 1:
            raise ValueError("The list has headings but no students under them.")
    except ValueError as e:
        return RedirectResponse(url=f"{back}?error={_quote(str(e))}", status_code=303)
    _os.makedirs(IMPORT_DIR, exist_ok=True)
    _clear_old_imports()
    token = _secrets.token_urlsafe(18)
    with open(_import_path(course.id, teacher.id, token), "w", encoding="utf-8") as f:
        _json.dump({"rows": rows, "header": header, "filename": filename[:120]}, f)
    return RedirectResponse(url=f"{back}?t={token}", status_code=303)


@router.post("/courses/{course_id}/class-list/import/confirm")
async def import_students_confirm(course_id: int, request: Request, db: Session = Depends(get_db),
                                  teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    form = await request.form()
    token = form.get("t", "")
    stored = _load_import(course.id, teacher.id, token)
    back = f"/courses/{course.id}/class-list/import"
    if stored is None:
        return RedirectResponse(url=f"{back}?error={_quote('That upload has expired - please upload it again.')}", status_code=303)
    columns = {f: _int_or_none(form.get(f"col_{f}")) for f in FIELD_LABELS}
    people = build_rows(stored["rows"], stored["header"], columns)
    if columns.get("group") is not None:
        chosen = set(form.getlist("group"))
        people = [p for p in people if p["group"] in chosen]
    plan = plan_import(_existing(course), people, archive_missing=form.get("archive") == "1")
    picked = set(form.getlist("pick"))
    by_id = {s.id: s for s in course.students}
    done = {"added": 0, "updated": 0, "reactivated": 0, "archived": 0}
    for item in plan["add"]:
        if item["key"] in picked:
            db.add(Student(course_id=course.id, name=item["name"], student_ref=item["ref"] or None))
            done["added"] += 1
    for kind in ("update", "reactivate"):
        for item in plan[kind]:
            student = by_id.get(item["id"])
            if student and item["key"] in picked:
                student.name = item["changes"].get("name", student.name)
                if "ref" in item["changes"]:
                    student.student_ref = item["changes"]["ref"]
                if kind == "reactivate":
                    student.is_active = True
                    done["reactivated"] += 1
                else:
                    done["updated"] += 1
    for item in plan["archive"]:
        student = by_id.get(item["id"])
        if student and item["key"] in picked:
            student.is_active = False  # archived, never deleted - their marks and history stay
            done["archived"] += 1
    db.commit()
    path = _import_path(course.id, teacher.id, token)
    if path and _os.path.exists(path):
        _os.remove(path)  # the uploaded list is not kept once it has been used
    parts = [f"{n} {word}" for word, n in (("added", done["added"]), ("updated", done["updated"]),
                                           ("reactivated", done["reactivated"]), ("archived", done["archived"])) if n]
    message = "Class list imported: " + (", ".join(parts) if parts else "no changes were needed") + "."
    return RedirectResponse(url=f"/courses/{course.id}?imported={_quote(message)}#students", status_code=303)
