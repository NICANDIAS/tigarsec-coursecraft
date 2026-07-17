from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Course, Student
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
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
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
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
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
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    student = db.query(Student).filter(Student.id == student_id, Student.course_id == course_id).first()
    if course and student:
        student.name = name
        student.student_ref = student_ref or None
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}/students/{student_id}", status_code=303)


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
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    student = db.query(Student).filter(Student.id == student_id, Student.course_id == course_id).first()
    if course and student:
        student.is_active = not student.is_active
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)
