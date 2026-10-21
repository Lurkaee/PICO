"""Staging service for filesystem diff proposals."""

from typing import Any
from sqlalchemy.orm import Session

from pico.core.errors import EntityNotFoundError, ValidationError
from pico.filesystem.adapter import get_filesystem_adapter
from pico.filesystem.contracts import (
    OperationType,
    compute_content_digest,
    compute_diff_digest,
    validate_text_content,
)
from pico.filesystem.diff_utils import generate_unified_diff
from pico.filesystem.roots import get_roots_manager
from pico.models.staged_diff import StagedDiff
from pico.repositories.staged_diff_repo import StagedDiffRepository


def stage_diff_proposal(
    db: Session,
    root_id: str,
    relative_path: str,
    operation: str,
    proposed_content: str,
    metadata_json: dict[str, Any] | None = None,
) -> StagedDiff:
    """
    Stage a filesystem diff proposal.
    Validates root, path, bounded text content, original file state,
    generates unified diff, calculates all digests, and persists the proposal.
    """
    op = operation.strip().lower()
    if op not in (OperationType.CREATE.value, OperationType.REPLACE.value):
        raise ValidationError(f"Unsupported filesystem operation '{operation}'. Must be 'create' or 'replace'.")

    validated_proposed = validate_text_content(proposed_content)
    roots_mgr = get_roots_manager()
    adapter = get_filesystem_adapter()

    # Resolve safe path within allowlisted root
    canonical_root, target_path = roots_mgr.resolve_safe_path(
        root_id=root_id,
        relative_path=relative_path,
        must_exist=(op == OperationType.REPLACE.value),
        parent_must_exist=True,
    )

    if op == OperationType.CREATE.value:
        if adapter.file_exists(target_path):
            raise ValidationError(
                f"Cannot stage create proposal: Target file already exists at '{relative_path}'."
            )
        original_text: str | None = None
        original_state = "ABSENT"
    else:
        # op == replace
        if not adapter.file_exists(target_path):
            raise ValidationError(
                f"Cannot stage replace proposal: Target file does not exist at '{relative_path}'."
            )
        original_text = adapter.read_text(target_path)
        original_state = compute_content_digest(original_text)

    proposed_digest = compute_content_digest(validated_proposed)
    unified_diff = generate_unified_diff(
        original_text=original_text,
        proposed_text=validated_proposed,
        relative_path=relative_path,
    )
    diff_digest = compute_diff_digest(unified_diff)

    repo = StagedDiffRepository(db)
    proposal = repo.create(
        root_id=root_id,
        relative_path=relative_path,
        operation=op,
        original_state=original_state,
        proposed_digest=proposed_digest,
        diff_digest=diff_digest,
        unified_diff=unified_diff,
        proposed_content=validated_proposed,
        original_content=original_text,
        metadata_json=metadata_json or {},
    )
    return proposal


def get_staged_diff_proposal(db: Session, proposal_id: str) -> StagedDiff:
    """Retrieve staged proposal by proposal_id or raise EntityNotFoundError."""
    repo = StagedDiffRepository(db)
    proposal = repo.get_by_id(proposal_id)
    if not proposal:
        raise EntityNotFoundError("StagedDiff", proposal_id)
    return proposal


def list_staged_diff_proposals(
    db: Session,
    root_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[StagedDiff]:
    """List staged proposals with optional root_id and status filters."""
    repo = StagedDiffRepository(db)
    return repo.list_proposals(root_id=root_id, status=status, limit=limit)


def discard_staged_diff_proposal(db: Session, proposal_id: str) -> StagedDiff:
    """Mark a staged proposal as discarded."""
    repo = StagedDiffRepository(db)
    proposal = repo.get_by_id(proposal_id)
    if not proposal:
        raise EntityNotFoundError("StagedDiff", proposal_id)
    if proposal.status != "staged":
        raise ValidationError(
            f"Cannot discard proposal '{proposal_id}' in status '{proposal.status}'. Must be 'staged'."
        )
    return repo.mark_discarded(proposal_id)
