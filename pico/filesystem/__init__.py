"""Staged filesystem diff subsystem for Milestone P8.2."""

from pico.filesystem.adapter import (
    BaseFilesystemAdapter,
    FakeFilesystemAdapter,
    WindowsFilesystemAdapter,
    get_filesystem_adapter,
    set_filesystem_adapter,
)
from pico.filesystem.contracts import (
    OperationType,
    ProposalStatus,
    compute_content_digest,
    compute_diff_digest,
    is_symlink_or_reparse_point,
    validate_relative_path,
    validate_text_content,
)
from pico.filesystem.diff_utils import generate_unified_diff
from pico.filesystem.roots import (
    FilesystemRootsManager,
    get_roots_manager,
    set_roots_manager,
)
from pico.filesystem.staging import (
    discard_staged_diff_proposal,
    get_staged_diff_proposal,
    list_staged_diff_proposals,
    stage_diff_proposal,
)

__all__ = [
    "BaseFilesystemAdapter",
    "FakeFilesystemAdapter",
    "WindowsFilesystemAdapter",
    "get_filesystem_adapter",
    "set_filesystem_adapter",
    "FilesystemRootsManager",
    "get_roots_manager",
    "set_roots_manager",
    "OperationType",
    "ProposalStatus",
    "compute_content_digest",
    "compute_diff_digest",
    "is_symlink_or_reparse_point",
    "validate_relative_path",
    "validate_text_content",
    "generate_unified_diff",
    "stage_diff_proposal",
    "get_staged_diff_proposal",
    "list_staged_diff_proposals",
    "discard_staged_diff_proposal",
]
