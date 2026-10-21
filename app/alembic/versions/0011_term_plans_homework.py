"""Plan the term, and homework

Revision ID: 0011_term_plans_homework
Revises: 0010_live_groups_photos
Create Date: 2026-10-20

Adds three NEW tables: term_plans, homeworks and homework_attempts. Each is only created if
missing (a new install already has them from models.py). Nothing existing is changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_term_plans_homework"
down_revision = "0010_live_groups_photos"
branch_labels = None
depends_on = None

NEW_TABLES = ["term_plans", "homeworks", "homework_attempts"]


def upgrade():
    from app.models import Base  # the table definitions live in one place: models.py
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in NEW_TABLES:
        if name not in existing:
            Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[name]])


def downgrade():
    # New tables only: older code never uses them, so rolling the CODE back is enough.
    pass
