"""baseline schema - matches models.py as of the migration system's introduction

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-16

This is a bootstrap migration, not a hand-written one - it delegates to
Base.metadata.create_all(), which already correctly describes every table/column in
models.py, rather than re-transcribing every column by hand (error-prone and redundant).

For a BRAND NEW database (new install), running `alembic upgrade head` executes this and
creates every table from scratch.

For an EXISTING database that already has all these tables (every prior update applied via
the old reset-and-recreate scripts), this migration should never actually run - instead,
stamp the existing database as already being at this revision with:
    alembic stamp head
This tells Alembic "this database is already here, just start tracking from this point
onward" without touching any table or data. From then on, every future schema change is a
real incremental migration (added columns/tables only), and `alembic upgrade head` just
applies whatever's new - no more resets, ever.
"""
from alembic import op
from app.models import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade():
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
