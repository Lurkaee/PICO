"""add_memory_subsystem_tables

Revision ID: 2a021f70a1f2
Revises: 849ac33a5a7a
Create Date: 2026-10-20 14:42:28.786582

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a021f70a1f2'
down_revision: Union[str, Sequence[str], None] = '849ac33a5a7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to include the four memory subsystem tables and FTS5 search index."""
    # 1. working_memory_items
    op.create_table(
        'working_memory_items',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('item_type', sa.String(length=32), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('project_id', sa.String(length=64), nullable=True),
        sa.Column('data_classification', sa.String(length=32), nullable=False),
        sa.Column('source_provenance', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('working_memory_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_working_memory_items_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_working_memory_items_key'), ['key'], unique=False)
        batch_op.create_index(batch_op.f('ix_working_memory_items_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_working_memory_items_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_working_memory_items_expires_at'), ['expires_at'], unique=False)

    # 2. episodic_memory_items
    op.create_table(
        'episodic_memory_items',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('project_id', sa.String(length=64), nullable=True),
        sa.Column('session_id', sa.String(length=64), nullable=True),
        sa.Column('data_classification', sa.String(length=32), nullable=False),
        sa.Column('source_provenance', sa.String(length=255), nullable=False),
        sa.Column('is_inferred', sa.Boolean(), nullable=False),
        sa.Column('deduplication_key', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('episodic_memory_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_episodic_memory_items_event_type'), ['event_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_episodic_memory_items_occurred_at'), ['occurred_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_episodic_memory_items_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_episodic_memory_items_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_episodic_memory_items_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_episodic_memory_items_deduplication_key'), ['deduplication_key'], unique=False)

    # 3. semantic_memories
    op.create_table(
        'semantic_memories',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('entity_type', sa.String(length=64), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('predicate', sa.String(length=128), nullable=False),
        sa.Column('object', sa.String(length=255), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('is_verified', sa.Boolean(), nullable=False),
        sa.Column('conflict_status', sa.String(length=32), nullable=False),
        sa.Column('data_classification', sa.String(length=32), nullable=False),
        sa.Column('source_provenance', sa.String(length=255), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=True),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('semantic_memories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_semantic_memories_entity_type'), ['entity_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_semantic_memories_subject'), ['subject'], unique=False)
        batch_op.create_index(batch_op.f('ix_semantic_memories_predicate'), ['predicate'], unique=False)
        batch_op.create_index(batch_op.f('ix_semantic_memories_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_semantic_memories_task_id'), ['task_id'], unique=False)

    # 4. procedural_memories
    op.create_table(
        'procedural_memories',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('lifecycle_status', sa.String(length=32), nullable=False),
        sa.Column('preconditions_json', sa.JSON(), nullable=True),
        sa.Column('required_capabilities_json', sa.JSON(), nullable=True),
        sa.Column('steps_json', sa.JSON(), nullable=False),
        sa.Column('expected_effects_json', sa.JSON(), nullable=True),
        sa.Column('verification_criteria_json', sa.JSON(), nullable=True),
        sa.Column('approved_by', sa.String(length=128), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('data_classification', sa.String(length=32), nullable=False),
        sa.Column('source_provenance', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('procedural_memories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_procedural_memories_name'), ['name'], unique=False)
        batch_op.create_index(batch_op.f('ix_procedural_memories_lifecycle_status'), ['lifecycle_status'], unique=False)

    # 5. SQLite FTS5 table for semantic full-text search
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        conn.execute(sa.text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS semantic_memories_fts USING fts5(
                memory_id UNINDEXED,
                subject,
                predicate,
                object,
                content
            );
        """))


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        conn.execute(sa.text("DROP TABLE IF EXISTS semantic_memories_fts;"))

    with op.batch_alter_table('procedural_memories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_procedural_memories_lifecycle_status'))
        batch_op.drop_index(batch_op.f('ix_procedural_memories_name'))
    op.drop_table('procedural_memories')

    with op.batch_alter_table('semantic_memories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_semantic_memories_task_id'))
        batch_op.drop_index(batch_op.f('ix_semantic_memories_project_id'))
        batch_op.drop_index(batch_op.f('ix_semantic_memories_predicate'))
        batch_op.drop_index(batch_op.f('ix_semantic_memories_subject'))
        batch_op.drop_index(batch_op.f('ix_semantic_memories_entity_type'))
    op.drop_table('semantic_memories')

    with op.batch_alter_table('episodic_memory_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_episodic_memory_items_deduplication_key'))
        batch_op.drop_index(batch_op.f('ix_episodic_memory_items_session_id'))
        batch_op.drop_index(batch_op.f('ix_episodic_memory_items_project_id'))
        batch_op.drop_index(batch_op.f('ix_episodic_memory_items_task_id'))
        batch_op.drop_index(batch_op.f('ix_episodic_memory_items_occurred_at'))
        batch_op.drop_index(batch_op.f('ix_episodic_memory_items_event_type'))
    op.drop_table('episodic_memory_items')

    with op.batch_alter_table('working_memory_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_working_memory_items_expires_at'))
        batch_op.drop_index(batch_op.f('ix_working_memory_items_project_id'))
        batch_op.drop_index(batch_op.f('ix_working_memory_items_task_id'))
        batch_op.drop_index(batch_op.f('ix_working_memory_items_key'))
        batch_op.drop_index(batch_op.f('ix_working_memory_items_session_id'))
    op.drop_table('working_memory_items')
