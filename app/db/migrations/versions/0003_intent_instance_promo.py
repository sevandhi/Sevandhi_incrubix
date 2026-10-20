"""publish_intents.instance, .connection_ref and .is_promotional

* ``instance``: the destination's instance_param value at publish time (e.g. the forum's base_url), so
  get_status / delete / fetch_metrics can be called with only the Appendix A arguments.
* ``connection_ref``: the connection id or a short hash of inline credentials (never a secret), used when
  the same platform_post_id exists on two instances.
* ``is_promotional``: the promo detector's verdict for the post, so ``self_promo: ratio(n)`` can count it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('publish_intents', sa.Column('instance', sa.Text(), nullable=True))
    op.add_column('publish_intents', sa.Column('connection_ref', sa.String(length=64), nullable=True))
    op.add_column('publish_intents', sa.Column('is_promotional', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('publish_intents', 'is_promotional')
    op.drop_column('publish_intents', 'connection_ref')
    op.drop_column('publish_intents', 'instance')
