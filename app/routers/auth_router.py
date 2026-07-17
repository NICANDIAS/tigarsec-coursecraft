from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher
from ..auth import hash_password, verify_password, create_access_token

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    # v1 keeps registration open for simplicity on a single-college internal server.
    # If deploying somewhere less trusted, gate this behind an invite code or disable
    # after your initial teacher accounts are created.
    is_first_account = db.query(Teacher).count() == 0
    return templates.TemplateResponse(
        "register.html", {"request": request, "error": None, "is_first_account": is_first_account}
    )


@router.post("/register")
def register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.query(Teacher).filter(Teacher.email == email).first()
    if existing:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "An account with that email already exists."}
        )

    is_first_account = db.query(Teacher).count() == 0
    teacher = Teacher(
        name=name,
        email=email,
        hashed_password=hash_password(password),
        role="admin" if is_first_account else "lecturer",
        is_primary_admin=is_first_account,
    )
    db.add(teacher)
    db.commit()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    teacher = db.query(Teacher).filter(Teacher.email == email).first()
    if not teacher or not verify_password(password, teacher.hashed_password):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Incorrect email or password."}
        )
    if not teacher.is_active:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "This account has been deactivated. Contact your admin."}
        )
    token = create_access_token(teacher.id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="access_token", value=token, httponly=True, max_age=12 * 3600)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response
