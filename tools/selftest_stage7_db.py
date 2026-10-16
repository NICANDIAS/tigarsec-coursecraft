"""
Stage 7 self-test: presenting in the browser from the PowerPoint file, live lessons on the
real slides, and deleting ended live sessions - through the REAL page functions, with a
THROWAWAY database in /tmp. Your real database is not opened. No AI. No PDFs needed.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage7_db.py
Expected last line: "ALL STAGE 7 CHECKS PASSED (13)"
"""
import json
import os
import random
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_stage7_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"
_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in self-test"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage7_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, CourseTeacher, LessonDeck, LiveSession  # noqa: E402
from app.routers import presentations as pres, live as lv  # noqa: E402
from app import pptx_generator as pg  # noqa: E402

RESULTS = []
TMP = tempfile.mkdtemp(prefix="cc_stage7_db_")


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
                    "server": ("test", 80), "scheme": "http", "root_path": ""})


def deck_data(html, element_id):
    m = re.search(r'<script id="' + element_id + r'" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
other = Teacher(name="Other", email="other@example.invalid", hashed_password="x", role="lecturer")
db.add_all([lecturer, other])
db.commit()
course = Course(teacher_id=lecturer.id, name="Business Y12")
db.add(course)
db.commit()
db.add(CourseTeacher(course_id=course.id, teacher_id=lecturer.id))
db.commit()

from PIL import Image  # noqa: E402
photo = os.path.join(TMP, "photo.jpg")
Image.new("RGB", (1200, 800), (40, 90, 140)).save(photo)
outline = {"lesson_title": "Cash flow", "palette": pg.pick_palette("Cash flow"), "slides": [
    {"type": "title", "title": "Cash flow", "subtitle": "Lesson 3"},
    {"type": "retrieval_starter", "title": "Do Now", "questions": ["What is revenue?"], "time_minutes": 5},
    {"type": "content", "title": "What is cash flow?", "guiding_question": "Can a profitable firm run out of cash?",
     "key_points": ["Money in and out over time"], "check_question": "Why does timing matter?", "image_query": "till",
     "resolved_image": {"type": "photo", "path": photo, "attribution": ""}},
    {"type": "objectives_review", "objectives_check": [{"objective": "Explain cash flow", "self_assess_prompt": "Could I explain it?"}]},
]}
random.seed(2)
pptx_path = os.path.join(TMP, "deck.pptx")
pg.render_lesson_pptx(outline, None, TMP, pptx_path)
deck = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="Cash flow", title="Cash flow lesson",
                  status="ready", outline_json=outline, pptx_path=pptx_path)
db.add(deck)
db.commit()

# ------------------------------------------------------------------ present mode
html = pres.present_deck(deck.id, request("/p"), db, lecturer).body.decode()
layout = deck_data(html, "deck-data")
check("present mode draws the slides from the PowerPoint file", layout is not None and len(layout["slides"]) == 5
      and "/static/js/slides.js" in html, "no deck data in the page")
check("...with the file's own colours (title slide background)", layout["slides"][0]["background"] == outline["palette"]["accent"])
picture = next(e for s in layout["slides"] for e in s["elements"] if e["kind"] == "picture")
s_index, shape_index = map(int, picture["media"].split("/"))
media = pres.deck_pptx_media(deck.id, s_index, shape_index, db, lecturer)
check("the slide's photo is served from inside the file", media.status_code == 200 and media.body[:2] == b"\xff\xd8")
check("another lecturer cannot fetch the deck's pictures", pres.deck_pptx_media(deck.id, s_index, shape_index, db, other).status_code == 404)
check("asking for a non-picture gives 'not found'", pres.deck_pptx_media(deck.id, 0, 0, db, lecturer).status_code == 404)

# ------------------------------------------------------------------ live lessons on the real slides
lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
session = db.query(LiveSession).first()
check("live screens are paired with the PowerPoint's slides", [s.get("slide") for s in session.screens_json] == [0, 1, 2, 3, 4])
check("a content slide is two steps: question first, then the reveal",
      session.screens_json[2]["kind"] == "content_question" and session.screens_json[2]["questions"][0]["prompt"].startswith("Can a profitable"))
teach = lv.live_teach(session.id, request("/t"), db, lecturer).body.decode()
check("the live screen carries the real slides", (deck_data(teach, "layout-data") or {}).get("slides") is not None
      and len(deck_data(teach, "layout-data")["slides"]) == 5)

# ------------------------------------------------------------------ delete rules
lv.live_delete(session.id, db, lecturer)
db.expire_all()
check("a session that is still live cannot be deleted (end it first)", db.get(LiveSession, session.id) is not None)
lv.live_end(session.id, db, lecturer)
lv.live_delete(session.id, db, other)
db.expire_all()
check("another lecturer cannot delete it", db.get(LiveSession, session.id) is not None)
lv.live_delete(session.id, db, lecturer)
db.expire_all()
check("an ended session is deleted with its answers", db.get(LiveSession, session.id) is None)

# ------------------------------------------------------------------ fallbacks
os.rename(pptx_path, pptx_path + ".gone")
html = pres.present_deck(deck.id, request("/p"), db, lecturer).body.decode()
check("if the PowerPoint file has been removed, present mode still works (older view)",
      deck_data(html, "deck-data") is None and "Exit present mode" in html)
lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
later = db.query(LiveSession).order_by(LiveSession.id.desc()).first()
check("...and a live lesson still starts, with its text view", later and all("slide" not in s for s in later.screens_json)
      and "Cash flow" in lv.live_teach(later.id, request("/t"), db, lecturer).body.decode())

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 7 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
