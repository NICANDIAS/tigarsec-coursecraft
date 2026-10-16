"""Stage 6: exam practice question sets and live interactive lessons

Revision ID: 0005_practice_and_live
Revises: 0004_learned_table_layouts
Create Date: 2026-10-16

Adds four NEW tables (practice_sets, live_sessions, live_participants, live_responses).
Nothing existing is changed. Each table is only created if it is not there yet (a brand-new
install already has them from models.py), so this is safe to run on any database.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_practice_and_live"
down_revision = "0004_learned_table_layouts"
branch_labels = None
depends_on = None

NEW_TABLES = ("practice_sets", "live_sessions", "live_participants", "live_responses")


def upgrade():
    from app.models import Base  # the table definitions live in one place: models.py
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    tables = [Base.metadata.tables[name] for name in NEW_TABLES if name not in existing]
    if tables:
        Base.metadata.create_all(bind=bind, tables=tables)


def downgrade():
    # New, separate tables: older code simply never uses them, so rolling the CODE back is enough.
    pass
