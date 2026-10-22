"""add workflow runs and steps tables (P9.1)

Revision ID: 5f284da042eb
Revises: 4e195bc031da
Create Date: 2026-10-22 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5f284da042eb"
down_revision: Union[str, None] = "4e195bc031da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("task_id", sa.String(length=64), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True, unique=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("data_classification", sa.String(length=32), nullable=False, server_default="INTERNAL"),
        sa.Column("total_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_workflow_runs_task_id", "workflow_runs", ["task_id"])
    op.create_index("ix_workflow_runs_session_id", "workflow_runs", ["session_id"])
    op.create_index("ix_workflow_runs_correlation_id", "workflow_runs", ["correlation_id"])
    op.create_index("ix_workflow_runs_idempotency_key", "workflow_runs", ["idempotency_key"])
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index("ix_workflow_runs_updated_at", "workflow_runs", ["updated_at"])

    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("workflow_run_id", sa.String(length=64), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_key", sa.String(length=128), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("capability_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("dependencies_json", sa.JSON(), nullable=False),
        sa.Column("input_template_json", sa.JSON(), nullable=False),
        sa.Column("resolved_input_json", sa.JSON(), nullable=True),
        sa.Column("tool_execution_id", sa.String(length=64), sa.ForeignKey("tool_executions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workflow_run_id", "step_key", name="uq_workflow_steps_run_key"),
    )

    op.create_index("ix_workflow_steps_workflow_run_id", "workflow_steps", ["workflow_run_id"])
    op.create_index("ix_workflow_steps_step_key", "workflow_steps", ["step_key"])
    op.create_index("ix_workflow_steps_capability_id", "workflow_steps", ["capability_id"])
    op.create_index("ix_workflow_steps_status", "workflow_steps", ["status"])
    op.create_index("ix_workflow_steps_tool_execution_id", "workflow_steps", ["tool_execution_id"])
    op.create_index("ix_workflow_steps_updated_at", "workflow_steps", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_steps_updated_at", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_tool_execution_id", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_status", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_capability_id", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_step_key", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_workflow_run_id", table_name="workflow_steps")
    op.drop_table("workflow_steps")

    op.drop_index("ix_workflow_runs_updated_at", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_status", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_idempotency_key", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_correlation_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_session_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_task_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
