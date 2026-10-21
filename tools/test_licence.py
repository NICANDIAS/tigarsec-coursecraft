"""Checks for app/licence.py: signatures, stages, notices, reminders, what each stage blocks,
and the TIGARSEC console sign-in. No database needed; uses a throwaway key made here.
Run: docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_licence.py
"""
import base64
import datetime
import importlib.util
import os
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

spec = importlib.util.spec_from_file_location("licence", os.path.join(os.path.dirname(__file__), "..", "app", "licence.py"))
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

passed = 0


def check(name, condition):
    global passed
    if not condition:
        print(f"FAIL: {name}")
        sys.exit(1)
    passed += 1
    print(f"ok   {name}")


def raises(fn, words=""):
    try:
        fn()
    except L.LicenceError as e:
        return words.lower() in str(e).lower()
    return False


key = Ed25519PrivateKey.generate()
other = Ed25519PrivateKey.generate()


def pub(k):
    return base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


KEYS = {"k1": pub(key)}


def sign(payload, k=key):
    body = L.canonical(payload)
    return L.b64u_encode(body) + "." + L.b64u_encode(k.sign(body))


def licence_file(**over):
    payload = {"v": 1, "kid": "k1", "id": "CC-2026-0001", "licensee": "Dudley College", "edition": "campus",
               "seats": 40, "valid_from": "2026-11-01", "valid_until": "2027-10-31", "grace_days": 30,
               "readonly_days": 60, "type": "paid", "issued": "2026-10-21"}
    payload.update(over)
    token = sign(payload)
    return "Licensee: whatever humans read\n" + L.BEGIN + "\n" + "\n".join(token[i:i + 64] for i in range(0, len(token), 64)) + "\n" + L.END + "\n"


# --- signatures
lic = L.verify_licence(licence_file(), KEYS)
check("a genuine licence verifies (wrapped over many lines, with human text above)", lic["id"] == "CC-2026-0001")
check("dates parsed", lic["_from"] == datetime.date(2026, 11, 1) and lic["_until"] == datetime.date(2027, 10, 31))
check("the bare token (no BEGIN/END) also verifies", L.verify_licence(L.extract_token(licence_file()), KEYS)["seats"] == 40)

good = licence_file()
token = L.extract_token(good)
body, sig = token.split(".")
import json
p = json.loads(L.b64u_decode(body))
p["valid_until"] = "2099-12-31"
forged = L.b64u_encode(L.canonical(p)) + "." + sig
check("changing the end date breaks the signature", raises(lambda: L.verify_licence(forged, KEYS), "signature"))
check("a licence signed by someone else is refused",
      raises(lambda: L.verify_licence(L.BEGIN + sign({**p, "valid_until": "2027-10-31"}, other) + L.END, KEYS), "signature"))
check("an unknown key id is refused clearly", raises(lambda: L.verify_licence(licence_file(kid="k9"), KEYS), "k9"))
check("no public key in this build is explained", raises(lambda: L.verify_licence(good, {}), "isn't set up"))
check("damaged text is refused", raises(lambda: L.verify_licence("hello.world", KEYS), "damaged"))
check("random text is refused", raises(lambda: L.verify_licence("not a licence", KEYS), "doesn't look like"))
check("a newer format is refused with advice", raises(lambda: L.verify_licence(licence_file(v=2), KEYS), "update CourseCraft"))
check("ends-before-starts is refused", raises(lambda: L.verify_licence(licence_file(valid_until="2026-01-01"), KEYS), "ends before"))
check("missing fields named", raises(lambda: L.verify_licence(L.BEGIN + sign({"v": 1, "kid": "k1"}) + L.END, KEYS), "missing"))
check("a wrong-length public key is explained",
      raises(lambda: L.verify_licence(good, {"k1": base64.b64encode(b"short").decode()}), "wrong length"))

# --- which licence is in force
old = {**lic, "id": "OLD", "_from": datetime.date(2025, 11, 1), "_until": datetime.date(2026, 10, 31)}
new = {**lic, "id": "NEW", "_from": datetime.date(2026, 11, 1), "_until": datetime.date(2027, 10, 31)}
check("before the renewal starts, the current one is in force", L.choose_licence([old, new], datetime.date(2026, 10, 20))["id"] == "OLD")
check("once the renewal starts, it takes over", L.choose_licence([old, new], datetime.date(2026, 11, 1))["id"] == "NEW")
check("a licence that hasn't started yet doesn't count on its own", L.choose_licence([new], datetime.date(2026, 10, 1)) is None)

# --- stages
first = datetime.date(2026, 10, 21)


def stage(day, l=lic):
    return L.compute_state(l, day, first)


check("licensed up to and including the end date", stage(datetime.date(2027, 10, 31))["stage"] == "licensed")
check("days left counts to the end date", stage(datetime.date(2027, 10, 1))["days_left"] == 30)
check("grace starts the day after", stage(datetime.date(2027, 11, 1))["stage"] == "grace")
check("grace lasts 30 days", stage(datetime.date(2027, 11, 30))["stage"] == "grace")
check("then read-only", stage(datetime.date(2027, 12, 1))["stage"] == "read_only")
check("read-only lasts 60 days", stage(datetime.date(2028, 1, 29))["stage"] == "read_only")
check("then export-only", stage(datetime.date(2028, 1, 30))["stage"] == "export_only")
check("export-only has no next change", stage(datetime.date(2029, 1, 1))["next_change"] is None)
check("next change named", stage(datetime.date(2027, 6, 1))["next_change"] == datetime.date(2027, 11, 1))
nograce = {**lic, "grace_days": 0}
check("zero grace goes straight to read-only", stage(datetime.date(2027, 11, 1), nograce)["stage"] == "read_only")
check("no licence = 30-day evaluation from first start", L.compute_state(None, datetime.date(2026, 11, 20), first)["stage"] == "evaluation")
check("evaluation then read-only (no grace)", L.compute_state(None, datetime.date(2026, 11, 21), first)["stage"] == "read_only")

# --- notices (only the end date and who to contact)
C = "support@tigarsec.com"
far = stage(datetime.date(2027, 1, 1))
check("far from the end: no notice for anyone", L.banner_for(far, "admin", C) is None and L.banner_for(far, "lecturer", C) is None)
sixty = stage(datetime.date(2027, 9, 1))
check("60 days out: admins told, lecturers not", L.banner_for(sixty, "admin", C) is not None and L.banner_for(sixty, "lecturer", C) is None)
fourteen = stage(datetime.date(2027, 10, 17))
b = L.banner_for(fourteen, "lecturer", C)
check("14 days out: lecturers told too", b is not None and "14 days" in b["text"])
check("a notice never shows licence details (seats, edition, ID)",
      all(w not in b["text"] for w in ("40", "campus", "CC-2026")) and C in b["text"])
check("grace notice for everyone", L.banner_for(stage(datetime.date(2027, 11, 5)), "lecturer", C)["level"] == "warning")
check("read-only notice is red", L.banner_for(stage(datetime.date(2027, 12, 5)), "admin", C)["level"] == "danger")
check("evaluation: admins see it", "evaluation" in L.banner_for(L.compute_state(None, first, first), "admin", C)["text"])

# --- reminders
check("no reminder far out", L.reminder_due(far, set()) is None)
r = L.reminder_due(sixty, set())
check("60-day reminder due", r and r[0] == "CC-2026-0001:60")
check("...and only once", L.reminder_due(sixty, {"CC-2026-0001:60"}) is None)
check("a late start sends only the most urgent one", L.reminder_due(stage(datetime.date(2027, 10, 28)), set())[0] == "CC-2026-0001:7")
check("grace reminder", L.reminder_due(stage(datetime.date(2027, 11, 2)), set())[0] == "CC-2026-0001:grace")

# --- what each stage blocks
D = L.decide
check("full use: everything allowed", all(D(s, "POST", "/courses/1/presentations/generate") is None for s in ("licensed", "grace", "evaluation")))
check("read-only: pages still open", D("read_only", "GET", "/courses/1") is None)
check("read-only: downloads still work", D("read_only", "GET", "/lesson-decks/3/download") is None)
check("read-only: AI generation blocked", D("read_only", "POST", "/courses/1/presentations/generate") == ("readonly", 403))
check("read-only: login still works", D("read_only", "POST", "/login") is None and D("read_only", "POST", "/login/verify-2fa") is None)
check("read-only: export works", D("read_only", "POST", "/admin/export/build") is None)
check("read-only: students told the session ended", D("read_only", "POST", "/live/s/ABC123/answer") == ("student_ended", 403))
check("read-only: homework submit blocked", D("read_only", "POST", "/hw/XYZ/submit") == ("student_ended", 403))
check("read-only: console always reachable", D("read_only", "POST", "/vendor/licence") is None)
check("export-only: lecturer locked out", D("export_only", "GET", "/", "lecturer") == ("ended", 403))
check("export-only: admin sent to export", D("export_only", "GET", "/", "admin") == ("to_export", 303))
check("export-only: admin can export", D("export_only", "GET", "/admin/export", "admin") is None and D("export_only", "POST", "/admin/export/build", "admin") is None)
check("export-only: lecturer can't export", D("export_only", "GET", "/admin/export", "lecturer") == ("ended", 403))
check("export-only: sign-in still possible", D("export_only", "GET", "/login") is None and D("export_only", "POST", "/login") is None)
check("export-only: styles load", D("export_only", "GET", "/static/css/style.css") is None)
check("prefix match is exact (/vendors is not /vendor)", D("export_only", "GET", "/vendors", "lecturer") == ("ended", 403))
check("export-only: student pages show ended", D("export_only", "GET", "/join") == ("student_ended", 403))

# --- TIGARSEC console sign-in
now = time.time()


def answer(**over):
    payload = {"purpose": "vendor-login", "kid": "k1", "challenge": "1A2B3C-4D5E6F-7A8B9C", "exp": int(now) + 600}
    payload.update(over)
    return "CCVENDOR." + sign(payload)


check("a signed answer for this challenge signs in", L.verify_vendor_response(answer(), KEYS, "1A2B3C-4D5E6F-7A8B9C", now)["purpose"] == "vendor-login")
check("answer for another challenge refused", raises(lambda: L.verify_vendor_response(answer(), KEYS, "FFFFFF-FFFFFF-FFFFFF", now), "different challenge"))
check("expired answer refused", raises(lambda: L.verify_vendor_response(answer(exp=int(now) - 1), KEYS, "1A2B3C-4D5E6F-7A8B9C", now), "expired"))
fake = "CCVENDOR." + sign({"purpose": "vendor-login", "kid": "k1", "challenge": "1A2B3C-4D5E6F-7A8B9C", "exp": int(now) + 600}, other)
check("an answer made with any other key refused (an admin can't make one)", raises(lambda: L.verify_vendor_response(fake, KEYS, "1A2B3C-4D5E6F-7A8B9C", now), "signature"))
check("a licence can't be used as a sign-in answer", raises(lambda: L.verify_vendor_response(good, KEYS, "x", now), "isn't a TIGARSEC"))
check("a sign-in answer can't be installed as a licence",
      raises(lambda: L.verify_licence(answer()[len("CCVENDOR."):], KEYS)))
check("public view drops internal fields", "_from" not in L.public_view(lic) and L.public_view(lic)["id"] == "CC-2026-0001")

print(f"\nALL LICENCE CHECKS PASSED ({passed})")
