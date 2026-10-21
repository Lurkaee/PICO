"""Provider-neutral filesystem adapter and atomic file operation boundaries."""

from abc import ABC, abstractmethod
import os
import uuid
from typing import Any

from pico.core.errors import ExecutionError, ValidationError
from pico.filesystem.contracts import (
    compute_content_digest,
    is_symlink_or_reparse_point,
    validate_text_content,
)


class BaseFilesystemAdapter(ABC):
    """Abstract interface for bounded, verifiable filesystem access."""

    @abstractmethod
    def read_text(self, path: str) -> str:
        """Read and validate text file content under bounded UTF-8 rules."""
        pass

    @abstractmethod
    def write_text_atomic(self, path: str, content: str) -> None:
        """Safely and atomically write text content to the destination path."""
        pass

    @abstractmethod
    def delete_file(self, path: str) -> None:
        """Safely remove a file from the destination path."""
        pass

    @abstractmethod
    def file_exists(self, path: str) -> bool:
        """Check if file exists at path."""
        pass

    @abstractmethod
    def is_file(self, path: str) -> bool:
        """Check if path is a regular file."""
        pass

    @abstractmethod
    def get_content_digest(self, path: str) -> str:
        """Compute SHA-256 digest of file's canonical UTF-8 content."""
        pass


class WindowsFilesystemAdapter(BaseFilesystemAdapter):
    """
    Production filesystem adapter with Windows atomic replacement semantics,
    reparse point checks, directory-local temporary staging, and flush/fsync durability.
    """

    def read_text(self, path: str) -> str:
        canonical = os.path.realpath(os.path.abspath(path))
        if is_symlink_or_reparse_point(canonical):
            raise ValidationError(f"Cannot read symlink or reparse point: '{path}'")
        if not os.path.isfile(canonical):
            raise ValidationError(f"Target is not a regular file: '{path}'")

        try:
            with open(canonical, "rb") as f:
                raw_bytes = f.read()
        except OSError as exc:
            raise ExecutionError(f"Failed to read file '{path}': {exc}")

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"File '{path}' is not valid UTF-8: {exc}")

        return validate_text_content(text)

    def write_text_atomic(self, path: str, content: str) -> None:
        canonical = os.path.realpath(os.path.abspath(path))
        parent_dir = os.path.dirname(canonical)

        if not os.path.isdir(parent_dir):
            raise ValidationError(f"Parent directory does not exist: '{parent_dir}'")
        if is_symlink_or_reparse_point(parent_dir):
            raise ValidationError(f"Parent directory cannot be a symlink or reparse point: '{parent_dir}'")
        if os.path.exists(canonical) and is_symlink_or_reparse_point(canonical):
            raise ValidationError(f"Target cannot be a symlink or reparse point: '{path}'")

        validated = validate_text_content(content)
        temp_filename = f".pico_tmp_{uuid.uuid4().hex}"
        temp_path = os.path.join(parent_dir, temp_filename)

        try:
            # Write to temporary file in the same directory for atomic replace
            with open(temp_path, "wb") as f:
                f.write(validated.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())

            # Atomic replace (replaces destination if it exists or creates if it does not)
            os.replace(temp_path, canonical)
        except OSError as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise ExecutionError(f"Atomic file write failed for '{path}': {exc}")

    def delete_file(self, path: str) -> None:
        canonical = os.path.realpath(os.path.abspath(path))
        if is_symlink_or_reparse_point(canonical):
            raise ValidationError(f"Cannot delete symlink or reparse point: '{path}'")
        if not os.path.exists(canonical):
            return
        try:
            os.remove(canonical)
        except OSError as exc:
            raise ExecutionError(f"Failed to remove file '{path}': {exc}")

    def file_exists(self, path: str) -> bool:
        canonical = os.path.realpath(os.path.abspath(path))
        return os.path.exists(canonical)

    def is_file(self, path: str) -> bool:
        canonical = os.path.realpath(os.path.abspath(path))
        return os.path.isfile(canonical)

    def get_content_digest(self, path: str) -> str:
        text = self.read_text(path)
        return compute_content_digest(text)


class FakeFilesystemAdapter(BaseFilesystemAdapter):
    """
    In-memory or mockable filesystem adapter for unit and adversarial testing.
    Tracks write, read, and delete call counts.
    """

    def __init__(self, initial_files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(initial_files or {})
        self.read_call_count = 0
        self.write_call_count = 0
        self.delete_call_count = 0

    def read_text(self, path: str) -> str:
        self.read_call_count += 1
        canonical = os.path.normpath(path)
        if canonical not in self.files:
            raise ValidationError(f"File not found in fake filesystem: '{path}'")
        return validate_text_content(self.files[canonical])

    def write_text_atomic(self, path: str, content: str) -> None:
        self.write_call_count += 1
        canonical = os.path.normpath(path)
        validated = validate_text_content(content)
        self.files[canonical] = validated

    def delete_file(self, path: str) -> None:
        self.delete_call_count += 1
        canonical = os.path.normpath(path)
        self.files.pop(canonical, None)

    def file_exists(self, path: str) -> bool:
        canonical = os.path.normpath(path)
        return canonical in self.files

    def is_file(self, path: str) -> bool:
        canonical = os.path.normpath(path)
        return canonical in self.files

    def get_content_digest(self, path: str) -> str:
        text = self.read_text(path)
        return compute_content_digest(text)


_filesystem_adapter: BaseFilesystemAdapter | None = None


def get_filesystem_adapter() -> BaseFilesystemAdapter:
    """Get the active global BaseFilesystemAdapter singleton."""
    global _filesystem_adapter
    if _filesystem_adapter is None:
        _filesystem_adapter = WindowsFilesystemAdapter()
    return _filesystem_adapter


def set_filesystem_adapter(adapter: BaseFilesystemAdapter | None) -> None:
    """Override or reset the active BaseFilesystemAdapter singleton."""
    global _filesystem_adapter
    _filesystem_adapter = adapter
