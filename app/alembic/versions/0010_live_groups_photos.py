"""Live lessons: small groups and photo answers

Revision ID: 0010_live_groups_photos
Revises: 0009_live_started_and_breaks
Create Date: 2026-10-20

Adds three NULLABLE columns: live_sessions.groups_json, live_responses.group_no and
live_responses.image_path. Each only added if missing. Nothing existing is changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_live_groups_photos"
down_revision = "0009_live_started_and_breaks"
branch_labels = None
depends_on = None

NEW_COLUMNS = {"live_sessions": [("groups_json", "JSON")],
               "live_responses": [("group_no", "INTEGER"), ("image_path", "VARCHAR(500)")]}


def upgrade():
    bind = op.get_bind()
    for table, columns in NEW_COLUMNS.items():
        existing = {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}
        for name, kind in columns:
            if existing and name not in existing:
                bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))


def downgrade():
    pass
