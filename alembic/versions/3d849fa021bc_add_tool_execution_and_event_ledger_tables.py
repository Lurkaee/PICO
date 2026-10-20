"""add tool execution and event ledger tables

Revision ID: 3d849fa021bc
Revises: 2a021f70a1f2
Create Date: 2026-10-20 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3d849fa021bc"
down_revision: Union[str, None] = "2a021f70a1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. tool_executions table
    op.create_table(
        "tool_executions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("task_id", sa.String(length=64), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("capability_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_details_json", sa.JSON(), nullable=True),
        sa.Column("verification_status", sa.String(length=32), nullable=True, server_default="pending"),
        sa.Column("verification_details_json", sa.JSON(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("required_permission", sa.String(length=64), nullable=True),
        sa.Column("data_classification", sa.String(length=32), nullable=False, server_default="INTERNAL"),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_input_digest", sa.String(length=64), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_tool_executions_task_id", "tool_executions", ["task_id"])
    op.create_index("ix_tool_executions_session_id", "tool_executions", ["session_id"])
    op.create_index("ix_tool_executions_capability_id", "tool_executions", ["capability_id"])
    op.create_index("ix_tool_executions_status", "tool_executions", ["status"])
    op.create_index("ix_tool_executions_idempotency_key", "tool_executions", ["idempotency_key"], unique=True)
    op.create_index("ix_tool_executions_input_digest", "tool_executions", ["input_digest"])
    op.create_index("ix_tool_executions_updated_at", "tool_executions", ["updated_at"])

    # 2. event_ledger table
    op.create_table(
        "event_ledger",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("execution_id", sa.String(length=64), sa.ForeignKey("tool_executions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("task_id", sa.String(length=64), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=True),
        sa.Column("capability_version", sa.String(length=32), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("data_classification", sa.String(length=32), nullable=False, server_default="INTERNAL"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_ledger_execution_id", "event_ledger", ["execution_id"])
    op.create_index("ix_event_ledger_task_id", "event_ledger", ["task_id"])
    op.create_index("ix_event_ledger_correlation_id", "event_ledger", ["correlation_id"])
    op.create_index("ix_event_ledger_event_type", "event_ledger", ["event_type"])
    op.create_index("ix_event_ledger_capability_id", "event_ledger", ["capability_id"])
    op.create_index("ix_event_ledger_occurred_at", "event_ledger", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_event_ledger_occurred_at", table_name="event_ledger")
    op.drop_index("ix_event_ledger_capability_id", table_name="event_ledger")
    op.drop_index("ix_event_ledger_event_type", table_name="event_ledger")
    op.drop_index("ix_event_ledger_correlation_id", table_name="event_ledger")
    op.drop_index("ix_event_ledger_task_id", table_name="event_ledger")
    op.drop_index("ix_event_ledger_execution_id", table_name="event_ledger")
    op.drop_table("event_ledger")

    op.drop_index("ix_tool_executions_updated_at", table_name="tool_executions")
    op.drop_index("ix_tool_executions_input_digest", table_name="tool_executions")
    op.drop_index("ix_tool_executions_idempotency_key", table_name="tool_executions")
    op.drop_index("ix_tool_executions_status", table_name="tool_executions")
    op.drop_index("ix_tool_executions_capability_id", table_name="tool_executions")
    op.drop_index("ix_tool_executions_session_id", table_name="tool_executions")
    op.drop_index("ix_tool_executions_task_id", table_name="tool_executions")
    op.drop_table("tool_executions")
