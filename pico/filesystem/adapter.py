"""Provider-neutral filesystem adapter and atomic file operation boundaries."""

from abc import ABC, abstractmethod
import os
import stat
import uuid
from typing import Any

from pico.core.errors import ExecutionError, ValidationError
from pico.filesystem.contracts import (
    compute_content_digest,
    is_symlink_or_reparse_point,
    validate_text_content,
)


def verify_boundary_and_reparse(
    path: str,
    root_path: str | None = None,
    must_exist: bool = False,
) -> tuple[str, str]:
    """
    Verify that path is within root_path and neither path nor any component in its ancestry
    is a symbolic link, junction, or reparse point.
    Returns (normalized_root, normalized_path).
    Fails closed if any reparse point or path escape is detected.
    """
    norm_path = os.path.normpath(os.path.abspath(path))

    if root_path is not None:
        norm_root = os.path.normpath(os.path.abspath(root_path))
        # Ensure root itself exists, is directory, and not a reparse point
        if not os.path.isdir(norm_root):
            raise ValidationError(f"Configured root directory does not exist: '{norm_root}'")
        if is_symlink_or_reparse_point(norm_root):
            raise ValidationError(f"Configured root directory is a symlink or reparse point: '{norm_root}'")

        # Check containment via commonpath
        try:
            common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(norm_path)])
        except ValueError:
            raise ValidationError(f"Target path escapes configured root: '{path}'")

        if common != os.path.normcase(norm_root) or os.path.normcase(norm_path) == os.path.normcase(norm_root):
            raise ValidationError(f"Target path escapes or equals configured root: '{path}'")

        # Traverse every path component between root and path
        rel = os.path.relpath(norm_path, norm_root)
        current = norm_root
        for part in rel.replace("\\", "/").split("/"):
            if not part or part == ".":
                continue
            current = os.path.join(current, part)
            if os.path.lexists(current):
                if is_symlink_or_reparse_point(current):
                    raise ValidationError(
                        f"Symlink or reparse point detected in path component: '{part}'"
                    )
                real_comp = os.path.realpath(current)
                try:
                    comp_common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(real_comp)])
                    if comp_common != os.path.normcase(norm_root):
                        raise ValidationError(
                            f"Path component '{part}' resolves outside configured root: '{real_comp}'"
                        )
                except ValueError:
                    raise ValidationError(
                        f"Path component '{part}' resolves outside configured root."
                    )
    else:
        norm_root = os.path.dirname(norm_path)
        if os.path.lexists(norm_path) and is_symlink_or_reparse_point(norm_path):
            raise ValidationError(f"Path is a symlink or reparse point: '{path}'")
        parent = os.path.dirname(norm_path)
        if os.path.lexists(parent) and is_symlink_or_reparse_point(parent):
            raise ValidationError(f"Parent directory is a symlink or reparse point: '{parent}'")

    if os.path.lexists(norm_path):
        if is_symlink_or_reparse_point(norm_path):
            raise ValidationError(f"Target path is a symlink or reparse point: '{path}'")

    if must_exist:
        if not os.path.exists(norm_path):
            raise ValidationError(f"Target file does not exist: '{path}'")
        if not os.path.isfile(norm_path):
            raise ValidationError(f"Target path is not a regular file: '{path}'")

    return norm_root, norm_path


class BaseFilesystemAdapter(ABC):
    """Abstract interface for bounded, verifiable filesystem access."""

    @abstractmethod
    def read_text(self, path: str, root_path: str | None = None) -> str:
        """Read and validate text file content under bounded UTF-8 rules."""
        pass

    @abstractmethod
    def write_text_atomic(
        self,
        path: str,
        content: str,
        root_path: str | None = None,
        *,
        exclusive: bool = False,
        expected_original_digest: str | None = None,
    ) -> None:
        """Safely and atomically write text content to the destination path."""
        pass

    @abstractmethod
    def delete_file(
        self,
        path: str,
        root_path: str | None = None,
        *,
        expected_digest: str | None = None,
    ) -> None:
        """Safely remove a file from the destination path."""
        pass

    @abstractmethod
    def file_exists(self, path: str, root_path: str | None = None) -> bool:
        """Check if file exists at path."""
        pass

    @abstractmethod
    def is_file(self, path: str, root_path: str | None = None) -> bool:
        """Check if path is a regular file."""
        pass

    @abstractmethod
    def get_content_digest(self, path: str, root_path: str | None = None) -> str:
        """Compute SHA-256 digest of file's canonical UTF-8 content."""
        pass


class WindowsFilesystemAdapter(BaseFilesystemAdapter):
    """
    Production filesystem adapter with Windows atomic replacement semantics,
    exclusive creation, reparse point checks, directory-local temporary staging,
    and flush/fsync durability.
    """

    def read_text(self, path: str, root_path: str | None = None) -> str:
        norm_root, norm_path = verify_boundary_and_reparse(path, root_path, must_exist=True)

        if not os.path.isfile(norm_path):
            raise ValidationError(f"Target is not a regular file: '{path}'")
        if is_symlink_or_reparse_point(norm_path):
            raise ValidationError(f"Cannot read symlink or reparse point: '{path}'")

        try:
            fd = os.open(norm_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError as exc:
            raise ExecutionError(f"Failed to open file '{path}': {exc}")

        try:
            st = os.fstat(fd)
            reparse_attr = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if hasattr(st, "st_file_attributes") and (st.st_file_attributes & reparse_attr):
                raise ValidationError(f"File descriptor refers to a reparse point: '{path}'")

            raw_chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw_chunks.append(chunk)
            raw_bytes = b"".join(raw_chunks)
        except OSError as exc:
            raise ExecutionError(f"Failed to read file '{path}': {exc}")
        finally:
            os.close(fd)

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"File '{path}' is not valid UTF-8: {exc}")

        return validate_text_content(text)

    def write_text_atomic(
        self,
        path: str,
        content: str,
        root_path: str | None = None,
        *,
        exclusive: bool = False,
        expected_original_digest: str | None = None,
    ) -> None:
        norm_root, norm_path = verify_boundary_and_reparse(path, root_path, must_exist=False)
        parent_dir = os.path.dirname(norm_path)

        if not os.path.isdir(parent_dir):
            raise ValidationError(f"Parent directory does not exist: '{parent_dir}'")
        if is_symlink_or_reparse_point(parent_dir):
            raise ValidationError(f"Parent directory cannot be a symlink or reparse point: '{parent_dir}'")

        validated = validate_text_content(content)
        raw_bytes = validated.encode("utf-8")

        if exclusive:
            # Exclusive file creation: O_CREAT | O_EXCL guarantees atomic non-overwrite
            if os.path.lexists(norm_path):
                raise ExecutionError(f"Target file already exists prior to exclusive creation: '{path}'")

            try:
                fd = os.open(
                    norm_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                    0o666,
                )
            except FileExistsError:
                raise ExecutionError(f"Target file already exists or was concurrently created: '{path}'")
            except OSError as exc:
                raise ExecutionError(f"Exclusive file creation failed for '{path}': {exc}")

            try:
                written = 0
                while written < len(raw_bytes):
                    n = os.write(fd, raw_bytes[written:])
                    if n == 0:
                        break
                    written += n
                os.fsync(fd)
            finally:
                os.close(fd)

            if is_symlink_or_reparse_point(norm_path):
                try:
                    os.unlink(norm_path)
                except OSError:
                    pass
                raise ValidationError(f"Created file is an unexpected symlink or reparse point: '{path}'")

        else:
            # Replacement: Verify target exists and is a regular file
            verify_boundary_and_reparse(norm_path, root_path, must_exist=True)

            if expected_original_digest:
                current_digest = self.get_content_digest(norm_path, root_path)
                if current_digest != expected_original_digest:
                    raise ExecutionError(
                        f"Target file content modified prior to atomic replacement. Expected '{expected_original_digest}', found '{current_digest}'."
                    )

            temp_filename = f".pico_tmp_{uuid.uuid4().hex}"
            temp_path = os.path.join(parent_dir, temp_filename)

            try:
                fd = os.open(
                    temp_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except OSError as exc:
                raise ExecutionError(f"Failed to create temporary file for atomic write: {exc}")

            try:
                written = 0
                while written < len(raw_bytes):
                    n = os.write(fd, raw_bytes[written:])
                    if n == 0:
                        break
                    written += n
                os.fsync(fd)
            finally:
                os.close(fd)

            try:
                # Pre-replacement boundary re-verification
                verify_boundary_and_reparse(norm_path, root_path, must_exist=True)
                verify_boundary_and_reparse(temp_path, root_path, must_exist=True)
                if is_symlink_or_reparse_point(norm_path):
                    raise ValidationError(f"Target cannot be a symlink or reparse point: '{path}'")
                if is_symlink_or_reparse_point(parent_dir):
                    raise ValidationError(f"Parent directory cannot be a symlink or reparse point: '{parent_dir}'")

                os.replace(temp_path, norm_path)
            except Exception as exc:
                if os.path.lexists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                if isinstance(exc, (ValidationError, ExecutionError)):
                    raise
                raise ExecutionError(f"Atomic file write failed for '{path}': {exc}")

    def delete_file(
        self,
        path: str,
        root_path: str | None = None,
        *,
        expected_digest: str | None = None,
    ) -> None:
        norm_root, norm_path = verify_boundary_and_reparse(path, root_path, must_exist=False)
        if not os.path.lexists(norm_path):
            return

        if is_symlink_or_reparse_point(norm_path):
            raise ValidationError(f"Cannot delete symlink or reparse point: '{path}'")

        if expected_digest:
            current_digest = self.get_content_digest(norm_path, root_path)
            if current_digest != expected_digest:
                raise ExecutionError(
                    f"Target file content modified prior to deletion. Expected '{expected_digest}', found '{current_digest}'."
                )

        # Immediate pre-removal reparse check
        if is_symlink_or_reparse_point(norm_path):
            raise ValidationError(f"Cannot delete symlink or reparse point: '{path}'")

        try:
            os.unlink(norm_path)
        except OSError as exc:
            raise ExecutionError(f"Failed to remove file '{path}': {exc}")

    def file_exists(self, path: str, root_path: str | None = None) -> bool:
        norm_root, norm_path = verify_boundary_and_reparse(path, root_path, must_exist=False)
        return os.path.exists(norm_path)

    def is_file(self, path: str, root_path: str | None = None) -> bool:
        norm_root, norm_path = verify_boundary_and_reparse(path, root_path, must_exist=False)
        return os.path.isfile(norm_path) and not is_symlink_or_reparse_point(norm_path)

    def get_content_digest(self, path: str, root_path: str | None = None) -> str:
        text = self.read_text(path, root_path)
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

    def read_text(self, path: str, root_path: str | None = None) -> str:
        self.read_call_count += 1
        canonical = os.path.normpath(path)
        if canonical not in self.files:
            raise ValidationError(f"File not found in fake filesystem: '{path}'")
        return validate_text_content(self.files[canonical])

    def write_text_atomic(
        self,
        path: str,
        content: str,
        root_path: str | None = None,
        *,
        exclusive: bool = False,
        expected_original_digest: str | None = None,
    ) -> None:
        self.write_call_count += 1
        canonical = os.path.normpath(path)
        if exclusive and canonical in self.files:
            raise ExecutionError(f"Target file already exists prior to exclusive creation: '{path}'")
        if not exclusive and expected_original_digest:
            if canonical not in self.files:
                raise ExecutionError(f"Target file does not exist for replacement: '{path}'")
            current_digest = compute_content_digest(self.files[canonical])
            if current_digest != expected_original_digest:
                raise ExecutionError(
                    f"Target file content modified prior to atomic replacement. Expected '{expected_original_digest}', found '{current_digest}'."
                )
        validated = validate_text_content(content)
        self.files[canonical] = validated

    def delete_file(
        self,
        path: str,
        root_path: str | None = None,
        *,
        expected_digest: str | None = None,
    ) -> None:
        self.delete_call_count += 1
        canonical = os.path.normpath(path)
        if expected_digest and canonical in self.files:
            current_digest = compute_content_digest(self.files[canonical])
            if current_digest != expected_digest:
                raise ExecutionError(
                    f"Target file content modified prior to deletion. Expected '{expected_digest}', found '{current_digest}'."
                )
        self.files.pop(canonical, None)

    def file_exists(self, path: str, root_path: str | None = None) -> bool:
        canonical = os.path.normpath(path)
        return canonical in self.files

    def is_file(self, path: str, root_path: str | None = None) -> bool:
        canonical = os.path.normpath(path)
        return canonical in self.files

    def get_content_digest(self, path: str, root_path: str | None = None) -> str:
        text = self.read_text(path, root_path)
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
