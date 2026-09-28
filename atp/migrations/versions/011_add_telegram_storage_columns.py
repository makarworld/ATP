"""add telegram storage columns

Revision ID: 011
Revises: 010
Create Date: 2026-09-28 21:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("videos", sa.Column("tg_file_id", sa.String(), nullable=True))
    op.add_column("videos", sa.Column("tg_likes_msg_id", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("tg_favs_msg_id", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("tg_deleted_msg_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("videos", "tg_deleted_msg_id")
    op.drop_column("videos", "tg_favs_msg_id")
    op.drop_column("videos", "tg_likes_msg_id")
    op.drop_column("videos", "tg_file_id")
