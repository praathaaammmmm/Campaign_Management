"""staff campaign assignments

Adds ``staff_campaigns``, the many-to-many link between staff accounts and the campaigns
they may work with. Purely additive: no existing staff, campaign or registration row is
touched, and the table starts empty, so existing staff have no assignments (and therefore
no campaign access) until an administrator grants some.

The composite primary key is both the uniqueness rule and the index for "campaigns of
this staff member"; the extra index serves the reverse lookup "staff of this campaign".

Revision ID: 20261011_0011
Revises: 20260910_0010
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261011_0011"
down_revision: Union[str, None] = "20260910_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_campaigns",
        sa.Column("staff_user_id", sa.Integer(), sa.ForeignKey("staff_users.id"), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), primary_key=True),
    )
    op.create_index("ix_staff_campaigns_campaign_id", "staff_campaigns", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_staff_campaigns_campaign_id", table_name="staff_campaigns")
    op.drop_table("staff_campaigns")
