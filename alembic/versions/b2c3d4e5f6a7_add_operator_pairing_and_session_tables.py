"""Add operator pairing challenges and operator sessions tables (P10.1.1)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-25 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_pairing_challenges",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("salt", sa.String(length=32), nullable=False),
        sa.Column("approver", sa.String(length=128), nullable=False, server_default="human_operator"),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    with op.batch_alter_table("operator_pairing_challenges", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_operator_pairing_challenges_expires_at"),
            ["expires_at"],
            unique=False,
        )

    op.create_table(
        "operator_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("approver", sa.String(length=128), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    with op.batch_alter_table("operator_sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_operator_sessions_expires_at"),
            ["expires_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("operator_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_operator_sessions_expires_at"))
    op.drop_table("operator_sessions")

    with op.batch_alter_table("operator_pairing_challenges", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_operator_pairing_challenges_expires_at"))
    op.drop_table("operator_pairing_challenges")
