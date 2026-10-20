"""content_items.extra and publications.extra (rendition fields, job ids, tailoring, assist packs)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('content_items', sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False))
    op.add_column('publications', sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('publications', 'extra')
    op.drop_column('content_items', 'extra')
