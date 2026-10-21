"""The exit pack: everything a college has put into CourseCraft, in one zip it can open without
CourseCraft - every table as a CSV (opens in Excel) plus every stored file. Available to
admins at any time and at every licence stage, so a college can always leave with its data.

Left out on purpose (secrets, not data): password hashes, the email password, AI keys, login
codes, students' private browser tokens, and the licensing tables (TIGARSEC's, not theirs).

This module never touches the database itself - app/routers/data_export.py hands it the rows -
so it can be tested on its own (tools/test_exit_pack.py).
"""
import csv
import datetime
import io
import json
import os
import zipfile

EXCLUDED_TABLES = {"login_otps", "licence_records", "licence_meta", "licence_events", "alembic_version"}
EXCLUDED_COLUMNS = {
    "teachers": {"hashed_password"},
    "system_settings": {"smtp_password"},
    "ai_provider_configs": {"api_key"},
    "live_participants": {"token"},
    "homework_attempts": {"token"},
}
SKIP_FILE_DIRS = {"__pycache__"}

README = """CourseCraft data export
=======================

Made: {made}
Tables: {tables} (in the "tables" folder - one CSV per table, opens in Excel)
Files: {files} (in the "files" folder - uploads, generated PowerPoints, images, as stored)

How to read it
- Each CSV's first row names its columns. "id" is the row's number; a column ending in "_id"
  points at the row with that id in another table (e.g. students.course_id -> courses.id).
- Dates and times are in ISO format (2026-10-21T14:05:00), stored in UTC.
- Columns holding structured data (answers, criteria, settings) contain JSON text.
- A file path stored in a table (e.g. "storage/uploads/abc.docx") is in the "files" folder
  under the same path without the leading "storage/".

Not included, on purpose: password hashes, the email password, AI provider keys, one-time
login codes and students' private browser tokens. These are secrets, not your data.

manifest.json lists every table with its row count.
"""


def cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary data, {len(bytes(value))} bytes>"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def keep_columns(table: str, columns: list) -> list:
    hidden = EXCLUDED_COLUMNS.get(table, set())
    return [c for c in columns if c not in hidden]


def write_pack(zip_path: str, tables, files_root=None, now=None) -> dict:
    """tables: iterable of (name, columns, rows) where rows yields dicts or tuples in column order.
    -> manifest dict. Writes the zip at zip_path."""
    now = now or datetime.datetime.utcnow()
    manifest = {"made_utc": now.replace(microsecond=0).isoformat(), "tables": {}, "files": 0,
                "excluded_tables": sorted(EXCLUDED_TABLES),
                "excluded_columns": {t: sorted(c) for t, c in EXCLUDED_COLUMNS.items()}}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for name, columns, rows in tables:
            if name in EXCLUDED_TABLES:
                continue
            kept = keep_columns(name, list(columns))
            buf = io.StringIO()
            buf.write("﻿")  # so Excel reads accents and symbols correctly
            writer = csv.writer(buf)
            writer.writerow(kept)
            count = 0
            for row in rows:
                record = row if isinstance(row, dict) else dict(zip(columns, row))
                writer.writerow([cell(record.get(c)) for c in kept])
                count += 1
            z.writestr(f"tables/{name}.csv", buf.getvalue())
            manifest["tables"][name] = count
        if files_root and os.path.isdir(files_root):
            for folder, dirs, names in os.walk(files_root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_FILE_DIRS and not os.path.islink(os.path.join(folder, d)))
                for fname in sorted(names):
                    full = os.path.join(folder, fname)
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    rel = os.path.relpath(full, files_root).replace(os.sep, "/")
                    z.write(full, f"files/{rel}")
                    manifest["files"] += 1
        z.writestr("README.txt", README.format(made=manifest["made_utc"] + " UTC",
                                                tables=len(manifest["tables"]), files=manifest["files"]))
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def folder_size(root) -> tuple:
    """-> (file count, bytes) of what the export would copy."""
    count = size = 0
    if root and os.path.isdir(root):
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_FILE_DIRS]
            for fname in names:
                full = os.path.join(folder, fname)
                if os.path.isfile(full) and not os.path.islink(full):
                    count += 1
                    size += os.path.getsize(full)
    return count, size
