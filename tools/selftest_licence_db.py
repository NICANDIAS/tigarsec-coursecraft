"""
Licensing + data export - self-test through the REAL code, with a THROWAWAY database in /tmp and a
throwaway signing key made here. Your real database, your real licence folder and your real
TIGARSEC key are not touched. No AI, no email is sent.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_licence_db.py
Expected last line: "ALL LICENSING CHECKS PASSED (33)"
"""
import asyncio
import base64
import datetime
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_licence_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_licence_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, LicenceRecord, LicenceEvent, AuditLogEntry  # noqa: E402
from app import licence as L, licence_service as svc  # noqa: E402
from app.auth import create_access_token  # noqa: E402
from app.routers import vendor as vr, data_export as dx  # noqa: E402

TMP = tempfile.mkdtemp(prefix="cc-licence-test-")
svc.LICENCE_DIR = os.path.join(TMP, "licence")
svc.INBOX = os.path.join(svc.LICENCE_DIR, "inbox")
svc.INSTALLED_DIR = os.path.join(svc.LICENCE_DIR, "installed")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


key = Ed25519PrivateKey.generate()
KEYS = {"k1": base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()}


def sign(payload):
    body = L.canonical(payload)
    return L.b64u_encode(body) + "." + L.b64u_encode(key.sign(body))


def licence_text(**over):
    p = {"v": 1, "kid": "k1", "id": "CC-2026-0001", "licensee": "Test College", "edition": "campus", "seats": 40,
         "valid_from": "2026-01-01", "valid_until": "2027-10-31", "type": "paid", "issued": "2026-10-21"}
    p.update(over)
    return L.BEGIN + "\n" + sign(p) + "\n" + L.END + "\n"


def request(path="/", method="GET", cookies=None):
    headers = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": method, "path": path, "query_string": b"", "headers": headers,
                    "client": ("203.0.113.9", 5000), "server": ("test", 80), "scheme": "http", "root_path": ""}, receive)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
admin = Teacher(name="Admin", email="admin@test.ac.uk", hashed_password="x", role="admin")
lect = Teacher(name="Lect", email="lect@test.ac.uk", hashed_password="x", role="lecturer")
db.add_all([admin, lect])
db.commit()

# 1-2: identity and evaluation
m1 = svc.get_meta(db)
m2 = svc.get_meta(db)
check("install ID made once and kept", m1.install_id == m2.install_id and len(m1.install_id) == 19)
check("install ID also written to the licence folder", open(os.path.join(svc.LICENCE_DIR, "install-id.txt")).read().strip() == m1.install_id)
s = svc.current_state(db, KEYS)
check("no licence -> evaluation", s["stage"] == "evaluation" and s["licence"] is None)

# 3-4: installing
lic = svc.install(db, licence_text(), "TIGARSEC console", KEYS)
check("genuine licence installed", db.query(LicenceRecord).count() == 1 and lic["id"] == "CC-2026-0001")
svc.install(db, licence_text(), "TIGARSEC console", KEYS)
check("the same file twice isn't stored twice", db.query(LicenceRecord).count() == 1)
check("now licensed", svc.current_state(db, KEYS, datetime.date(2026, 10, 21))["stage"] == "licensed")
bad = licence_text().replace(L.extract_token(licence_text())[5:15], "AAAAAAAAAA")
try:
    svc.install(db, bad, "TIGARSEC console", KEYS)
    check("tampered licence refused", False)
except L.LicenceError:
    check("tampered licence refused, nothing stored", db.query(LicenceRecord).count() == 1)
check("licence events recorded (not in the college audit log)",
      db.query(LicenceEvent).filter(LicenceEvent.action == "licence_installed").count() == 1
      and db.query(AuditLogEntry).count() == 0)

# 5: file drop
os.makedirs(svc.INBOX)
with open(os.path.join(svc.INBOX, "renewal.cclicence"), "w") as f:
    f.write(licence_text(id="CC-2027-0001", valid_from="2027-11-01", valid_until="2028-10-31"))
with open(os.path.join(svc.INBOX, "junk.cclicence"), "w") as f:
    f.write("not a licence")
res = dict(svc.import_inbox(db, KEYS))
check("dropped genuine file installed and moved", res.get("renewal.cclicence") == "CC-2027-0001"
      and os.path.exists(os.path.join(svc.INSTALLED_DIR, "renewal.cclicence")))
check("dropped junk renamed .rejected", res.get("junk.cclicence") is None and os.path.exists(os.path.join(svc.INBOX, "junk.cclicence.rejected")))
check("renewal takes over on its start date", svc.current_state(db, KEYS, datetime.date(2027, 11, 1))["licence"]["id"] == "CC-2027-0001")
check("a licence that no longer verifies is listed as a problem, not used",
      len(svc.installed_licences(db, {})[1]) == 2 and svc.current_state(db, {}, datetime.date(2026, 10, 21))["licence"] is None)

# 6: hourly checks, stage changes, reminders
sent = []
svc.run_checks(db, KEYS, datetime.date(2026, 10, 21), send=lambda *a: sent.append(a), email_ready=lambda: True)
check("first check records the stage quietly", svc.get_meta(db).last_stage == "licensed" and not sent)
svc.run_checks(db, KEYS, datetime.date(2028, 9, 15), send=lambda *a: sent.append(a), email_ready=lambda: True)
check("60-day reminder emailed to admins only", len(sent) == 1 and sent[0][0] == "admin@test.ac.uk")
check("reminder never names seats or edition", "40" not in sent[0][2] and "campus" not in sent[0][2])
svc.run_checks(db, KEYS, datetime.date(2028, 9, 16), send=lambda *a: sent.append(a), email_ready=lambda: True)
check("...and isn't sent twice", len(sent) == 1)
svc.run_checks(db, KEYS, datetime.date(2028, 11, 5), send=lambda *a: sent.append(a), email_ready=lambda: True)
check("stage change to grace logged and its reminder sent",
      db.query(LicenceEvent).filter(LicenceEvent.action == "stage_changed").count() == 1
      and svc.get_meta(db).last_stage == "grace" and len(sent) == 2)
svc.run_checks(db, KEYS, datetime.date(2028, 12, 5), send=lambda *a: sent.append(a), email_ready=lambda: False)
check("no email set up: read-only reminder not marked as sent (it goes once email works)",
      len(sent) == 2 and "CC-2027-0001:read_only" not in (svc.get_meta(db).reminders_sent or [])
      and svc.get_meta(db).last_stage == "read_only")

# 7: middleware
admin_cookie = {"access_token": create_access_token(admin.id)}
lect_cookie = {"access_token": create_access_token(lect.id)}


def run_mw(stage, path, method="GET", cookies=None, accept=None):
    calls, out = [], []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"app"})

    async def send(message):
        out.append(message)
    headers = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    if accept:
        headers.append((b"accept", accept.encode()))
    mw = svc.LicenceMiddleware(app, state_getter=lambda: {"stage": stage}, session_factory=SessionLocal)
    asyncio.run(mw({"type": "http", "path": path, "method": method, "headers": headers}, None, send))
    return bool(calls), out[0]["status"], dict(out[0].get("headers", [])), out[1]["body"] if len(out) > 1 else b""


check("licensed: request reaches the app", run_mw("licensed", "/courses/1/presentations/generate", "POST")[0])
ok_, status, _, body = run_mw("read_only", "/courses/1/presentations/generate", "POST")
check("read-only: generation blocked with a clear page", not ok_ and status == 403 and b"read-only" in body)
ok_, status, hdrs, body = run_mw("read_only", "/live/s/ABC/answer", "POST", accept="application/json")
check("read-only: student answer gets JSON 'ended'", status == 403 and hdrs.get(b"content-type") == b"application/json"
      and b"ended" in body and b"licence" not in body.lower())
_, status, hdrs, _ = run_mw("export_only", "/", "GET", admin_cookie)
check("export-only: admin sent to Data export", status == 303 and hdrs.get(b"location") == b"/admin/export")
check("export-only: admin can download the export", run_mw("export_only", "/admin/export/build", "POST", admin_cookie)[0])
check("export-only: lecturer locked out", run_mw("export_only", "/", "GET", lect_cookie)[1] == 403)
check("export-only: forged cookie gets nothing", run_mw("export_only", "/admin/export", "GET", {"access_token": "forged"})[1] == 403)

# 8: data export from the real database tables
store = os.path.join(TMP, "storage")
os.makedirs(os.path.join(store, "uploads"))
with open(os.path.join(store, "uploads", "a.txt"), "w") as f:
    f.write("hello")
zpath = os.path.join(TMP, "export.zip")
manifest = dx.build_export(zpath, engine, store)
z = zipfile.ZipFile(zpath)
teachers_csv = z.read("tables/teachers.csv").decode()
check("export has every staff account, without password hashes",
      "admin@test.ac.uk" in teachers_csv and "hashed_password" not in teachers_csv and manifest["tables"]["teachers"] == 2)
check("export leaves licensing out", not any(n.startswith("tables/licence_") for n in z.namelist()))
check("export includes stored files", z.read("files/uploads/a.txt") == b"hello")

# 9: the TIGARSEC console
vr.TRUSTED_PUBLIC_KEYS = KEYS
svc.TRUSTED_PUBLIC_KEYS = KEYS
svc.invalidate_cache()
page = vr.vendor_home(request("/vendor"), db)
check("console without signing in shows only a challenge", page.template.name == "vendor_challenge.html")
code = page.context["code"]
fake = vr.vendor_login(request("/vendor/login", "POST"), code, "CCVENDOR." + sign({"purpose": "vendor-login", "kid": "k1", "challenge": "000000-000000-000000", "exp": int(time.time()) + 600}), db)
check("an answer for another code is refused", getattr(fake, "status_code", 0) == 403)
code = vr._new_challenge()
resp = vr.vendor_login(request("/vendor/login", "POST"), code,
                       "CCVENDOR." + sign({"purpose": "vendor-login", "kid": "k1", "challenge": code, "exp": int(time.time()) + 600}), db)
cookie = resp.headers.get("set-cookie", "")
check("TIGARSEC's signed answer opens a 1-hour console session", resp.status_code == 303 and "cc_vendor=" in cookie and "Path=/vendor" in cookie)
token = cookie.split("cc_vendor=")[1].split(";")[0]
console = vr.vendor_home(request("/vendor", cookies={"cc_vendor": token}), db)
check("console shows the licence and history", console.template.name == "vendor_console.html"
      and console.context["lic"]["licensee"] == "Test College" and len(console.context["rows"]) == 2)
again = vr.vendor_login(request("/vendor/login", "POST"), code,
                        "CCVENDOR." + sign({"purpose": "vendor-login", "kid": "k1", "challenge": code, "exp": int(time.time()) + 600}), db)
check("a challenge works only once", getattr(again, "status_code", 0) == 403)

db.close()
n = sum(RESULTS)
if all(RESULTS):
    print(f"\nALL LICENSING CHECKS PASSED ({n})")
else:
    print(f"\n{len(RESULTS) - n} of {len(RESULTS)} CHECKS FAILED")
    sys.exit(1)
