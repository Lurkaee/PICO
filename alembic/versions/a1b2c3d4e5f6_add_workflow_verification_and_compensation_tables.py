"""add workflow verifications and compensations tables (P9.3)

Revision ID: a1b2c3d4e5f6
Revises: 5f284da042eb
Create Date: 2026-10-22 22:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "5f284da042eb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_verifications",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workflow_step_id",
            sa.String(length=64),
            sa.ForeignKey("workflow_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tool_execution_id",
            sa.String(length=64),
            sa.ForeignKey("tool_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("policy_id", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
        sa.Column("verification_method", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("output_digest", sa.String(length=64), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("findings_json", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("data_classification", sa.String(length=32), nullable=False, server_default="INTERNAL"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_workflow_verifications_workflow_run_id", "workflow_verifications", ["workflow_run_id"])
    op.create_index("ix_workflow_verifications_workflow_step_id", "workflow_verifications", ["workflow_step_id"])
    op.create_index("ix_workflow_verifications_tool_execution_id", "workflow_verifications", ["tool_execution_id"])
    op.create_index("ix_workflow_verifications_policy_id", "workflow_verifications", ["policy_id"])
    op.create_index("ix_workflow_verifications_status", "workflow_verifications", ["status"])
    op.create_index("ix_workflow_verifications_updated_at", "workflow_verifications", ["updated_at"])

    op.create_table(
        "workflow_compensations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workflow_step_id",
            sa.String(length=64),
            sa.ForeignKey("workflow_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "original_tool_execution_id",
            sa.String(length=64),
            sa.ForeignKey("tool_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("compensation_capability_id", sa.String(length=128), nullable=False),
        sa.Column("compensation_capability_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
        sa.Column(
            "compensation_tool_execution_id",
            sa.String(length=64),
            sa.ForeignKey("tool_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="registered"),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_workflow_compensations_workflow_run_id", "workflow_compensations", ["workflow_run_id"])
    op.create_index("ix_workflow_compensations_workflow_step_id", "workflow_compensations", ["workflow_step_id"])
    op.create_index("ix_workflow_compensations_status", "workflow_compensations", ["status"])
    op.create_index("ix_workflow_compensations_updated_at", "workflow_compensations", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_compensations_updated_at", table_name="workflow_compensations")
    op.drop_index("ix_workflow_compensations_status", table_name="workflow_compensations")
    op.drop_index("ix_workflow_compensations_workflow_step_id", table_name="workflow_compensations")
    op.drop_index("ix_workflow_compensations_workflow_run_id", table_name="workflow_compensations")
    op.drop_table("workflow_compensations")

    op.drop_index("ix_workflow_verifications_updated_at", table_name="workflow_verifications")
    op.drop_index("ix_workflow_verifications_status", table_name="workflow_verifications")
    op.drop_index("ix_workflow_verifications_policy_id", table_name="workflow_verifications")
    op.drop_index("ix_workflow_verifications_tool_execution_id", table_name="workflow_verifications")
    op.drop_index("ix_workflow_verifications_workflow_step_id", table_name="workflow_verifications")
    op.drop_index("ix_workflow_verifications_workflow_run_id", table_name="workflow_verifications")
    op.drop_table("workflow_verifications")
