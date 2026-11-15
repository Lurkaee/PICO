"""add unique partial index for voice perception events

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-14 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_perception_events_voice_session",
        "perception_events",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("event_type = 'voice_transcript_delivery'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_perception_events_voice_session",
        table_name="perception_events",
    )
