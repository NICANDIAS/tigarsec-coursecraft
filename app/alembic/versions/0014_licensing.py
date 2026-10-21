"""Licensing: installed licences, this installation's identity, licensing history

Revision ID: 0014_licensing
Revises: 0013_scheme_pace
Create Date: 2026-10-21

Adds three NEW tables (licence_records, licence_meta, licence_events), each only if missing.
Nothing existing is changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_licensing"
down_revision = "0013_scheme_pace"
branch_labels = None
depends_on = None

NEW_TABLES = ["licence_records", "licence_meta", "licence_events"]


def upgrade():
    from app.models import Base
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in NEW_TABLES:
        if name not in existing:
            Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[name]])


def downgrade():
    pass  # new tables only; older code never reads them
