"""add perception events table

Revision ID: 4e195bc031da
Revises: 3d849fa021bc
Create Date: 2026-10-20 16:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4e195bc031da"
down_revision: Union[str, None] = "3d849fa021bc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "perception_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("modality", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("source_device_id", sa.String(length=128), nullable=False),
        sa.Column("data_classification", sa.String(length=32), nullable=False, server_default="INTERNAL"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("raw_payload_scrubbed", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("structured_data", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.String(length=255), nullable=False),
        sa.Column("retention_policy", sa.String(length=32), nullable=False, server_default="short_term_audit"),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("parent_event_ids", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index("ix_perception_events_correlation_id", "perception_events", ["correlation_id"])
    op.create_index("ix_perception_events_modality", "perception_events", ["modality"])
    op.create_index("ix_perception_events_event_type", "perception_events", ["event_type"])
    op.create_index("ix_perception_events_source_device_id", "perception_events", ["source_device_id"])
    op.create_index("ix_perception_events_data_classification", "perception_events", ["data_classification"])
    op.create_index("ix_perception_events_session_id", "perception_events", ["session_id"])
    op.create_index("ix_perception_events_created_at", "perception_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_perception_events_created_at", table_name="perception_events")
    op.drop_index("ix_perception_events_session_id", table_name="perception_events")
    op.drop_index("ix_perception_events_data_classification", table_name="perception_events")
    op.drop_index("ix_perception_events_source_device_id", table_name="perception_events")
    op.drop_index("ix_perception_events_event_type", table_name="perception_events")
    op.drop_index("ix_perception_events_modality", table_name="perception_events")
    op.drop_index("ix_perception_events_correlation_id", table_name="perception_events")
    op.drop_table("perception_events")
