"""Checks for app/exit_pack.py (the full data export). No database needed.
Run: docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_exit_pack.py
"""
import csv
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
import zipfile

spec = importlib.util.spec_from_file_location("exit_pack", os.path.join(os.path.dirname(__file__), "..", "app", "exit_pack.py"))
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)

passed = 0


def check(name, condition):
    global passed
    if not condition:
        print(f"FAIL: {name}")
        sys.exit(1)
    passed += 1
    print(f"ok   {name}")


tmp = tempfile.mkdtemp()
storage = os.path.join(tmp, "storage")
os.makedirs(os.path.join(storage, "uploads"))
os.makedirs(os.path.join(storage, "__pycache__"))
with open(os.path.join(storage, "uploads", "essay.docx"), "wb") as f:
    f.write(b"docx bytes")
with open(os.path.join(storage, "__pycache__", "x.pyc"), "wb") as f:
    f.write(b"junk")
os.symlink("/etc/passwd", os.path.join(storage, "uploads", "sneaky"))

when = datetime.datetime(2026, 10, 21, 14, 5, 0)
tables = [
    ("teachers", ["id", "name", "email", "hashed_password", "is_active"],
     [(1, "Ada Lovelace", "ada@x.ac.uk", "$2b$12$secret", True), {"id": 2, "name": "Zoë Ñúñez", "email": "z@x", "hashed_password": "h", "is_active": False}]),
    ("students", ["id", "name", "answers_json", "created_at"],
     [(1, "Sam", {"0": 2, "1": [1, 2]}, when), (2, "Kai", None, datetime.date(2026, 9, 1))]),
    ("login_otps", ["id", "code_hash"], [(1, "x")]),
    ("licence_records", ["id", "raw"], [(1, "licence")]),
    ("ai_provider_configs", ["id", "provider", "api_key"], [(1, "anthropic", "sk-ant-SECRET")]),
    ("homework_attempts", ["id", "token", "score"], [(1, "tok-SECRET", 7)]),
    ("lesson_images", ["id", "data"], [(1, b"\x00\x01\x02")]),
]
zpath = os.path.join(tmp, "out.zip")
manifest = E.write_pack(zpath, tables, storage, now=when)
z = zipfile.ZipFile(zpath)
names = set(z.namelist())


def read_csv(name):
    text = z.read(f"tables/{name}.csv").decode("utf-8")
    check(f"{name}.csv starts with the Excel UTF-8 marker", text.startswith("﻿"))
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


teachers = read_csv("teachers")
check("password hash column dropped", teachers[0] == ["id", "name", "email", "is_active"])
check("rows as tuples and dicts both written, accents kept", teachers[2][1] == "Zoë Ñúñez" and teachers[1][3] == "true")
students = read_csv("students")
check("JSON columns written as JSON text", json.loads(students[1][2]) == {"0": 2, "1": [1, 2]})
check("dates in ISO format, empty for none", students[1][3] == "2026-10-21T14:05:00" and students[2][2] == "")
check("login codes and licensing tables left out", "tables/login_otps.csv" not in names and "tables/licence_records.csv" not in names)
check("AI key column dropped", "api_key" not in read_csv("ai_provider_configs")[0])
check("student browser tokens dropped", read_csv("homework_attempts")[0] == ["id", "score"])
check("binary data described, not dumped", read_csv("lesson_images")[1][1] == "<binary data, 3 bytes>")
check("stored files copied", z.read("files/uploads/essay.docx") == b"docx bytes")
check("symlinks and caches not followed", "files/uploads/sneaky" not in names and not any("__pycache__" in n for n in names))
check("README and manifest included", "README.txt" in names and "manifest.json" in names)
m = json.loads(z.read("manifest.json"))
check("manifest counts rows and files", m["tables"]["teachers"] == 2 and m["files"] == 1 and m == manifest)
check("manifest says what was left out", "login_otps" in m["excluded_tables"] and "hashed_password" in m["excluded_columns"]["teachers"])
check("README says what's missing on purpose", "password hashes" in z.read("README.txt").decode())
check("folder size skips caches and links", E.folder_size(storage) == (1, len(b"docx bytes")))
check("missing storage folder is fine", E.write_pack(os.path.join(tmp, "b.zip"), [], os.path.join(tmp, "nope"))["files"] == 0)
check("nothing secret anywhere in the zip",
      not any(b"SECRET" in z.read(n) or b"$2b$12$secret" in z.read(n) for n in names))

print(f"\nALL EXPORT CHECKS PASSED ({passed})")
