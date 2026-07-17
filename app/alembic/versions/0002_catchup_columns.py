"""catch-up: every column added via manual ALTER instructions since baseline

Revision ID: 0002_catchup_columns
Revises: 0001_baseline
Create Date: 2026-07-17

This exists because several columns were added to models.py across recent updates, each
shipped with a manual "run this ALTER TABLE command" instruction instead of a real
migration - which defeats the entire point of having Alembic. This migration brings any
database up to date regardless of which of those manual commands were or weren't actually
run: it checks each table's ACTUAL current columns first (via PRAGMA table_info) and only
adds what's genuinely missing, so it's safe to run on any database in any partial state.

From this point on, schema changes get a real migration file like this one, applied
automatically by `docker compose up --build` - no more manual ALTER instructions.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_catchup_columns"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# (table, column, SQL type) for every column that was ever shipped as a manual ALTER
COLUMNS_TO_ENSURE = [
    ("submissions", "submitted_at", "DATE"),
    ("submissions", "achieved_grade", "VARCHAR(50)"),
    ("submissions", "marking_scheme_notes", "TEXT"),
    ("submissions", "similarity_warning", "TEXT"),
    ("lesson_decks", "unit_index", "INTEGER"),
    ("lesson_decks", "selected_lo_indices", "TEXT"),
    ("lesson_decks", "fits_in_requested_time", "BOOLEAN"),
    ("lesson_decks", "scope_note", "TEXT"),
    ("lesson_decks", "source_sow_id", "INTEGER"),
    ("lesson_decks", "source_week", "INTEGER"),
    ("schemes_of_work", "planned_assessments", "TEXT"),
]


def _existing_columns(bind, table_name):
    result = bind.execute(sa.text(f"PRAGMA table_info({table_name})"))
    return {row[1] for row in result}  # row[1] is the column name in PRAGMA table_info output


def upgrade():
    bind = op.get_bind()
    for table, column, sql_type in COLUMNS_TO_ENSURE:
        existing = _existing_columns(bind, table)
        if column not in existing:
            bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def downgrade():
    # SQLite can't easily drop columns - this migration is intentionally one-directional.
    pass
