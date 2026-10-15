"""Stage 5: learned table layouts

Revision ID: 0004_learned_table_layouts
Revises: 0003_spec_quality_versions
Create Date: 2026-10-16

Adds one NULLABLE column, discovered_spec_formats.layout_json, where a table layout that
CourseCraft worked out for a new spec is saved, so the next spec with that layout is read
with no AI. Same safe pattern as 0002/0003: only adds the column if it is missing (a brand-new
install already has it from models.py). Existing learned text patterns get NULL and keep
working exactly as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_learned_table_layouts"
down_revision = "0003_spec_quality_versions"
branch_labels = None
depends_on = None


def _existing_columns(bind, table_name):
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table_name})"))}


def upgrade():
    bind = op.get_bind()
    if "layout_json" not in _existing_columns(bind, "discovered_spec_formats"):
        bind.execute(sa.text("ALTER TABLE discovered_spec_formats ADD COLUMN layout_json TEXT"))


def downgrade():
    # SQLite can't easily drop columns; the column is nullable and ignored by older code.
    pass
