from fastapi import FastAPI, Request, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .database import engine, Base
from .auth import NotAuthenticated, NotAuthorized, get_current_teacher
from .routers import auth_router, courses, specs, scheme_of_work, students, submissions, admin, analytics, iv, presentations, tasks, feedback

# Schema creation/updates are now handled by Alembic (run automatically on container
# startup via docker-entrypoint.sh) instead of create_all() here - this is what lets
# schema changes ship as real migrations instead of requiring a database reset.

app = FastAPI(title="TIGARSEC CourseCraft")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login")


@app.exception_handler(NotAuthorized)
async def not_authorized_handler(request: Request, exc: NotAuthorized):
    return RedirectResponse(url="/")


@app.get("/coming-soon")
def coming_soon(request: Request, feature: str = "This feature", teacher=Depends(get_current_teacher)):
    return templates.TemplateResponse("coming_soon.html", {"request": request, "teacher": teacher, "feature": feature})


app.include_router(auth_router.router)
app.include_router(courses.router)
app.include_router(specs.router)
app.include_router(scheme_of_work.router)
app.include_router(students.router)
app.include_router(submissions.router)
app.include_router(admin.router)
app.include_router(analytics.router)
app.include_router(iv.router)
app.include_router(presentations.router)
app.include_router(tasks.router)
app.include_router(feedback.router)
