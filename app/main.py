import asyncio

from fastapi import FastAPI, Request, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .database import engine, Base, SessionLocal
from .models import Course, CourseTeacher
from .auth import NotAuthenticated, NotAuthorized, get_current_teacher
from .routers import auth_router, courses, specs, scheme_of_work, students, submissions, admin, analytics, iv, presentations, tasks, feedback, ai_providers, data_retention, sow_templates, exam_exemplars, timetabling, practice, live, quizzes, marking_report, tracker, term_plan, homework
from .schema_check import diff_schema, apply_schema_fix
from . import retention

# Schema creation/updates are now handled by Alembic (run automatically on container
# startup via docker-entrypoint.sh) instead of create_all() here - this is what lets
# schema changes ship as real migrations instead of requiring a database reset.

app = FastAPI(title="TIGARSEC CourseCraft")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def sync_schema_on_startup():
    """
    Standing fix, added after a real deployment lockout: an update added a new column that
    the LOGIN flow itself needs (Teacher.two_factor_enabled - SQLAlchemy selects every
    mapped column on any query for a Teacher, so a missing column breaks login outright,
    not just the 2FA feature specifically). Fixing it needed Admin: Schema Check - which
    needs being logged in as admin - which needed the very column that was missing. No
    manual step can escape that loop on its own.

    Runs the same purely-additive sync as Admin: Schema Check (create missing tables, add
    missing columns - never drops or modifies anything existing) automatically, before the
    app starts accepting requests. Admin: Schema Check remains for manually reviewing what
    changed, but is no longer a REQUIRED step to unblock the app after an update.
    """
    try:
        diff = diff_schema(engine, Base)
        report = apply_schema_fix(engine, Base, diff)
        if report:
            print(f"[startup] Schema sync applied: {'; '.join(report)}", flush=True)
        else:
            print("[startup] Schema sync: nothing to do, already up to date.", flush=True)
    except Exception as e:
        # Never let a schema-sync problem stop the app from starting at all - worst case,
        # Admin: Schema Check is still there as a manual fallback for anything this couldn't
        # fix itself, and the server keeps running for everything unaffected by the gap.
        print(f"[startup] Schema sync failed (non-fatal, app is still starting): {e}", flush=True)


@app.on_event("startup")
def backfill_course_teachers_on_startup():
    """
    One-time (but safely repeatable) data backfill, run right after schema sync above so
    the course_teachers table already exists by the time this runs.

    CourseTeacher replaces Course.teacher_id as the real access-control mechanism (see its
    docstring in models.py) - but every course that existed before this change only has
    the old single teacher_id, no CourseTeacher row yet. Without this backfill, every
    existing course would suddenly become inaccessible to the teacher who's been using it,
    the moment this ships - the exact kind of lockout the schema-sync startup hook above
    was written to prevent for a different column, now applied here for the same reason.

    Idempotent - only creates a row where one doesn't already exist for that exact
    (course, teacher) pair, so running this every startup (not just once) is safe and
    correct, not just "probably fine".
    """
    db = SessionLocal()
    try:
        created = 0
        for course in db.query(Course).filter(Course.teacher_id.isnot(None)).all():
            exists = (
                db.query(CourseTeacher)
                .filter(CourseTeacher.course_id == course.id, CourseTeacher.teacher_id == course.teacher_id)
                .first()
            )
            if not exists:
                db.add(CourseTeacher(course_id=course.id, teacher_id=course.teacher_id))
                created += 1
        if created:
            db.commit()
            print(f"[startup] Backfilled {created} course_teachers row(s) from existing Course.teacher_id.", flush=True)
        else:
            print("[startup] course_teachers backfill: nothing to do, already up to date.", flush=True)
    except Exception as e:
        # Same principle as schema sync - never let this stop the app from starting.
        # Worst case, some pre-existing courses are temporarily inaccessible until this is
        # investigated, rather than the whole app failing to come up at all.
        db.rollback()
        print(f"[startup] course_teachers backfill failed (non-fatal, app is still starting): {e}", flush=True)
    finally:
        db.close()


async def _retention_loop():
    """
    Runs for the lifetime of the app process, checking once a day whether anything needs
    marking for deletion, purging, or a reminder email. A plain asyncio loop rather than a
    scheduler library - this app is a single long-running process, so a daily cadence needs
    nothing more than sleeping between checks. Runs once immediately on startup too (not
    just after the first 24h), so a container that was down when a check should have
    happened doesn't wait a full day to catch up.
    """
    while True:
        db = SessionLocal()
        try:
            summary = retention.run_retention_check(db)
            if summary["marked"] or summary["purged"]:
                print(f"[retention] marked={summary['marked']} purged={summary['purged']} upcoming={summary['upcoming']}", flush=True)
        except Exception as e:
            # Same principle as schema sync - a retention-check failure must never crash
            # the app or stop it accepting requests. Worst case, nothing gets purged today
            # and it tries again tomorrow; Admin: Data Retention still shows the true state.
            print(f"[retention] daily check failed (non-fatal): {e}", flush=True)
        finally:
            db.close()
        await asyncio.sleep(24 * 3600)


@app.on_event("startup")
async def start_retention_loop():
    asyncio.create_task(_retention_loop())


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login")


@app.exception_handler(NotAuthorized)
async def not_authorized_handler(request: Request, exc: NotAuthorized):
    return RedirectResponse(url="/")


@app.get("/coming-soon")
def coming_soon(request: Request, feature: str = "This feature", teacher=Depends(get_current_teacher)):
    return templates.TemplateResponse("coming_soon.html", {"request": request, "teacher": teacher, "feature": feature})


@app.get("/early-access/{feature_key}")
def early_access_placeholder(feature_key: str, request: Request, teacher=Depends(get_current_teacher)):
    """
    Standing placeholder for an in-development feature someone has been granted early
    access to (see auth.py's EARLY_ACCESS_FEATURES/has_early_access) - checks access
    independently here too, not just in the nav link that got them here, since a direct
    URL guess must be blocked at the route itself, not only hidden from view.

    When a feature here is actually built for real, replace THIS placeholder with the
    real route at whatever URL makes sense for it, and update the nav link in base.html
    to point there for the granted case instead - the access check moves to the real
    route at that point, this one stops being reachable for that feature key.
    """
    from .auth import EARLY_ACCESS_FEATURES, has_early_access
    label = EARLY_ACCESS_FEATURES.get(feature_key)
    if not label or not has_early_access(teacher, feature_key):
        return RedirectResponse(url=f"/coming-soon?feature={label or 'This feature'}", status_code=303)
    return templates.TemplateResponse(
        "early_access_placeholder.html", {"request": request, "teacher": teacher, "feature": label},
    )


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
app.include_router(ai_providers.router)
app.include_router(data_retention.router)
app.include_router(sow_templates.router)
app.include_router(exam_exemplars.router)
app.include_router(timetabling.router)
app.include_router(practice.router)
app.include_router(live.router)
app.include_router(quizzes.router)
app.include_router(marking_report.router)
app.include_router(tracker.router)
app.include_router(term_plan.router)
app.include_router(homework.router)
