"""Admin -> Data export: download everything as one zip (app/exit_pack.py). Admins only, and
available at every licence stage - read-only and export-only included."""
import datetime
import os
import tempfile

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import MetaData, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..auth import require_admin
from ..database import get_db, engine
from ..models import Teacher, AuditLogEntry
from .. import exit_pack

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
STORAGE = "storage"


def _tables(conn):
    """Every table actually in the database (not just the ones today's code knows), streamed."""
    meta = MetaData()
    meta.reflect(bind=conn)
    for table in meta.sorted_tables:
        if table.name in exit_pack.EXCLUDED_TABLES:
            continue
        columns = [c.name for c in table.columns]
        result = conn.execute(select(table)).mappings()
        yield table.name, columns, (dict(r) for r in result)


def build_export(zip_path: str, eng=None, storage=STORAGE) -> dict:
    with (eng or engine).connect() as conn:
        return exit_pack.write_pack(zip_path, _tables(conn), storage)


@router.get("/admin/export", response_class=HTMLResponse)
def export_page(request: Request, teacher: Teacher = Depends(require_admin)):
    files, size = exit_pack.folder_size(STORAGE)
    return templates.TemplateResponse("data_export.html", {
        "request": request, "teacher": teacher, "files": files, "size_mb": round(size / 1_048_576, 1),
    })


@router.post("/admin/export/build")
def export_build(teacher: Teacher = Depends(require_admin), db: Session = Depends(get_db)):
    os.makedirs(os.path.join("data", "exports"), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    fd, path = tempfile.mkstemp(prefix=f"coursecraft-export-{stamp}-", suffix=".zip", dir=os.path.join("data", "exports"))
    os.close(fd)
    try:
        manifest = build_export(path)
    except Exception:
        os.remove(path)
        raise
    db.add(AuditLogEntry(actor=teacher.email, action="data_export",
                         detail=f"Downloaded a full data export: {len(manifest['tables'])} tables, "
                                f"{sum(manifest['tables'].values())} rows, {manifest['files']} files."))
    db.commit()
    return FileResponse(path, media_type="application/zip", filename=f"coursecraft-export-{stamp}.zip",
                        background=BackgroundTask(lambda: os.path.exists(path) and os.remove(path)))
