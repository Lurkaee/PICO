"""Provider-neutral filesystem adapter and atomic file operation boundaries."""

from abc import ABC, abstractmethod
import contextlib
import os
import stat
import sys
import uuid
from typing import Any

from pico.core.errors import ExecutionError, ValidationError
from pico.filesystem.contracts import (
    compute_content_digest,
    is_symlink_or_reparse_point,
    validate_text_content,
)

# Windows Win32 API setup for handle-based safety and directory hierarchy pinning
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes, Structure, byref, sizeof, c_char

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    DELETE = 0x00010000
    FILE_READ_DATA = 0x0001
    FILE_LIST_DIRECTORY = 0x0001

    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004

    CREATE_NEW = 1
    CREATE_ALWAYS = 2
    OPEN_EXISTING = 3
    OPEN_ALWAYS = 4
    TRUNCATE_EXISTING = 5

    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

    VOLUME_NAME_DOS = 0x0
    FileRenameInfo = 3
    FileDispositionInfo = 4

    class FILE_DISPOSITION_INFO(Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    class FILE_RENAME_INFO(Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1024),
        ]

    class BY_HANDLE_FILE_INFORMATION(Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL

    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL

    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL

    kernel32.MoveFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    kernel32.MoveFileW.restype = wintypes.BOOL


def is_invalid_handle(h: Any) -> bool:
    """Check if Win32 handle is null or INVALID_HANDLE_VALUE."""
    if h is None:
        return True
    val = h if isinstance(h, int) else getattr(h, "value", -1)
    return val in (-1, 0, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF)


def get_final_path_from_handle(h: Any) -> str:
    """Retrieve canonical DOS path from an open Windows handle."""
    if sys.platform != "win32":
        raise NotImplementedError("Handle path resolution is only supported on Windows.")
    buf = ctypes.create_unicode_buffer(1024)
    res = kernel32.GetFinalPathNameByHandleW(h, buf, 1024, VOLUME_NAME_DOS)
    if res == 0:
        err = ctypes.get_last_error()
        raise OSError(f"GetFinalPathNameByHandleW failed with error {err}")
    path = buf.value
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normpath(path)


def read_all_bytes_from_handle(h: Any) -> bytes:
    """Read all bytes from an open Windows file handle until EOF."""
    if sys.platform != "win32":
        raise NotImplementedError("Handle reading is only supported on Windows.")
    chunks: list[bytes] = []
    buf = (c_char * 65536)()
    read_bytes = wintypes.DWORD(0)
    while True:
        res = kernel32.ReadFile(h, buf, 65536, byref(read_bytes), None)
        if not res:
            err = ctypes.get_last_error()
            if err == 109:  # ERROR_BROKEN_PIPE or EOF
                break
            raise OSError(f"ReadFile failed with error {err}")
        if read_bytes.value == 0:
            break
        chunks.append(buf.raw[: read_bytes.value])
    return b"".join(chunks)


def write_all_bytes_to_handle(h: Any, data: bytes) -> None:
    """Write all bytes to an open Windows file handle and flush buffers to disk."""
    if sys.platform != "win32":
        raise NotImplementedError("Handle writing is only supported on Windows.")
    offset = 0
    total = len(data)
    written = wintypes.DWORD(0)
    while offset < total:
        chunk = data[offset:]
        chunk_len = len(chunk)
        res = kernel32.WriteFile(h, chunk, chunk_len, byref(written), None)
        if not res or written.value == 0:
            err = ctypes.get_last_error()
            raise OSError(f"WriteFile failed with error {err}")
        offset += written.value
    res = kernel32.FlushFileBuffers(h)
    if not res:
        err = ctypes.get_last_error()
        raise OSError(f"FlushFileBuffers failed with error {err}")


def safe_unlink(path: str, root_path: str | None = None) -> None:
    """
    Safely delete a temporary file without following symlinks or reparse points
    and without touching anything outside root_path.
    Uses handle-based deletion on Windows to bind deletion to the verified object.
    """
    if not os.path.lexists(path):
        return
    norm_root = os.path.normpath(os.path.abspath(root_path)) if root_path else os.path.dirname(os.path.abspath(path))
    norm_path = os.path.normpath(os.path.abspath(path))

    # Containment check
    try:
        common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(norm_path)])
    except ValueError:
        return
    if common != os.path.normcase(norm_root) or os.path.normcase(norm_path) == os.path.normcase(norm_root):
        return

    if sys.platform == "win32":
        h = kernel32.CreateFileW(
            norm_path,
            DELETE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if is_invalid_handle(h):
            return
        try:
            info = BY_HANDLE_FILE_INFORMATION()
            if kernel32.GetFileInformationByHandle(h, byref(info)):
                # If target is a reparse point, refuse to delete through handle
                if not (info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT):
                    final_p = get_final_path_from_handle(h)
                    final_common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_p)])
                    if final_common == os.path.normcase(norm_root):
                        disp = FILE_DISPOSITION_INFO(True)
                        kernel32.SetFileInformationByHandle(
                            h, FileDispositionInfo, byref(disp), sizeof(disp)
                        )
        finally:
            kernel32.CloseHandle(h)
    else:
        try:
            if not is_symlink_or_reparse_point(norm_path):
                os.unlink(norm_path)
        except OSError:
            pass


@contextlib.contextmanager
def pin_directory_hierarchy(target_dir: str, root_dir: str | None = None):
    """
    Lock every directory component in the path hierarchy on Windows from root down to target_dir.
    Opening each directory with FILE_LIST_DIRECTORY and omitting FILE_SHARE_DELETE
    ensures that no concurrent external process can rename, unlink, or swap any
    directory component in the path with a junction/reparse point while the handles
    remain open (the Windows NT kernel rejects such mutations with WinError 32: ERROR_SHARING_VIOLATION).
    Validates that each opened directory handle refers to a genuine, non-reparse
    directory contained strictly within root_dir.
    Yields list of opened directory handles and closes them in reverse order upon exit.
    """
    if sys.platform != "win32":
        yield []
        return

    norm_target = os.path.normpath(os.path.abspath(target_dir))
    norm_root = os.path.normpath(os.path.abspath(root_dir)) if root_dir else os.path.dirname(norm_target)

    # Determine directories to pin from root down to target
    try:
        common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(norm_target)])
    except ValueError:
        raise ValidationError(f"Target directory escapes configured root: '{target_dir}'")
    if common != os.path.normcase(norm_root):
        raise ValidationError(f"Target directory escapes configured root: '{target_dir}'")

    rel = os.path.relpath(norm_target, norm_root)
    dirs_to_pin = [norm_root]
    if rel != ".":
        curr = norm_root
        for part in rel.replace("\\", "/").split("/"):
            if not part or part == ".":
                continue
            curr = os.path.join(curr, part)
            dirs_to_pin.append(curr)

    opened_handles: list[Any] = []
    try:
        for d in dirs_to_pin:
            if not os.path.exists(d):
                raise ValidationError(f"Directory in path hierarchy does not exist: '{d}'")
            if not os.path.isdir(d):
                raise ValidationError(f"Path component is not a directory: '{d}'")

            h = kernel32.CreateFileW(
                d,
                FILE_LIST_DIRECTORY,
                FILE_SHARE_READ | FILE_SHARE_WRITE,  # NOTE: NO FILE_SHARE_DELETE!
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if is_invalid_handle(h):
                err = ctypes.get_last_error()
                raise ValidationError(f"Failed to open handle to directory '{d}': WinError {err}")

            opened_handles.append(h)

            info = BY_HANDLE_FILE_INFORMATION()
            if not kernel32.GetFileInformationByHandle(h, byref(info)):
                err = ctypes.get_last_error()
                raise ValidationError(f"Failed to inspect directory handle for '{d}': WinError {err}")

            if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValidationError(f"Reparse point detected in pinned directory handle: '{d}'")

            final_p = get_final_path_from_handle(h)
            try:
                final_common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_p)])
            except ValueError:
                raise ValidationError(f"Pinned directory handle '{d}' resolves outside root: '{final_p}'")
            if final_common != os.path.normcase(norm_root):
                raise ValidationError(f"Pinned directory handle '{d}' resolves outside root: '{final_p}'")

        yield opened_handles
    finally:
        for h in reversed(opened_handles):
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass


def verify_boundary_and_reparse(
    path: str,
    root_path: str | None = None,
    must_exist: bool = False,
) -> tuple[str, str]:
    """
    Verify that path is within root_path and neither path nor any component in its ancestry
    is a symbolic link, junction, or reparse point.
    Returns (normalized_root, normalized_path).
    Fails closed if any reparse point, path escape, or inspection error is detected.
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
        expected_proposed_digest: str | None = None,
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
    Production filesystem adapter with Windows handle boundaries, directory hierarchy pinning,
    exclusive creation, reparse point checks, directory-local temporary staging,
    handle-locked target relocation, non-overwriting MoveFileW, and post-write digest verification.

    Operational Limitation & Security Semantics:
    Atomic target-bound replacement on Windows NT is guaranteed by:
    1. Locking the parent directory hierarchy via pin_directory_hierarchy to block
       parent substitution and junction hijacking at the Windows kernel level.
    2. Opening the target file exclusively with GENERIC_READ | DELETE and FILE_SHARE_READ
       (omitting FILE_SHARE_WRITE and FILE_SHARE_DELETE) so no concurrent process can modify,
       rename, delete, or replace the approved target while attributes and expected digest are validated.
    3. Staging replacement content into a directory-local temporary file via CREATE_NEW in the pinned parent.
    4. Relocating the verified target file to a backup path via SetFileInformationByHandle(FileRenameInfo)
       while holding the handle.
    5. Moving the staged content to the target path via MoveFileW WITHOUT replacement. If a competing
       process created or substituted a file at the target path in the relocation window, MoveFileW
       fails closed (ERROR_ALREADY_EXISTS), leaving the competitor file 100% untouched and preserving
       the displaced original file in the backup path with zero overwrite.
    6. Performing independent post-write verification against the approved proposed digest.
    7. On non-Windows platforms, where Windows NT kernel handle sharing and directory pinning semantics
       are unavailable, atomic filesystem mutations fail closed with ExecutionError.
    """

    def __init__(self) -> None:
        self._on_pre_replace_move: Any = None


    def read_text(self, path: str, root_path: str | None = None) -> str:
        norm_root, norm_path = verify_boundary_and_reparse(path, root_path, must_exist=True)
        parent_dir = os.path.dirname(norm_path)

        with pin_directory_hierarchy(parent_dir, norm_root):
            if sys.platform == "win32":
                h = kernel32.CreateFileW(
                    norm_path,
                    GENERIC_READ,
                    FILE_SHARE_READ,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
                if is_invalid_handle(h):
                    err = ctypes.get_last_error()
                    raise ExecutionError(f"Failed to open file '{path}': WinError {err}")
                try:
                    info = BY_HANDLE_FILE_INFORMATION()
                    if not kernel32.GetFileInformationByHandle(h, byref(info)):
                        err = ctypes.get_last_error()
                        raise ExecutionError(f"Failed to inspect file handle '{path}': WinError {err}")
                    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                        raise ValidationError(f"Cannot read symlink or reparse point: '{path}'")
                    final_p = get_final_path_from_handle(h)
                    try:
                        common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_p)])
                    except ValueError:
                        raise ValidationError(f"Target file resolves outside configured root: '{final_p}'")
                    if common != os.path.normcase(norm_root):
                        raise ValidationError(f"Target file resolves outside configured root: '{final_p}'")
                    raw_bytes = read_all_bytes_from_handle(h)
                finally:
                    kernel32.CloseHandle(h)
            else:
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
        expected_proposed_digest: str | None = None,
    ) -> None:
        norm_root, norm_path = verify_boundary_and_reparse(path, root_path, must_exist=False)
        parent_dir = os.path.dirname(norm_path)

        if not os.path.isdir(parent_dir):
            raise ValidationError(f"Parent directory does not exist: '{parent_dir}'")
        if is_symlink_or_reparse_point(parent_dir):
            raise ValidationError(f"Parent directory cannot be a symlink or reparse point: '{parent_dir}'")

        validated = validate_text_content(content)
        raw_bytes = validated.encode("utf-8")
        target_proposed_digest = expected_proposed_digest or compute_content_digest(validated)

        if exclusive:
            # Exclusive file creation: CREATE_NEW guarantees atomic non-overwrite
            if os.path.lexists(norm_path):
                raise ExecutionError(f"Target file already exists prior to exclusive creation: '{path}'")

            if sys.platform != "win32":
                raise ExecutionError(
                    "Handle-bound atomic filesystem mutations require Windows NT kernel handle sharing and directory pinning semantics and are unsupported on non-Windows platforms."
                )

            with pin_directory_hierarchy(parent_dir, norm_root):
                h = kernel32.CreateFileW(
                    norm_path,
                    GENERIC_READ | GENERIC_WRITE,
                    FILE_SHARE_READ,
                    None,
                    CREATE_NEW,
                    FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
                if is_invalid_handle(h):
                    err = ctypes.get_last_error()
                    if err in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
                        raise ExecutionError(
                            f"Target file already exists or was concurrently created: '{path}'"
                        )
                    raise ExecutionError(f"Exclusive file creation failed for '{path}': WinError {err}")

                try:
                    info = BY_HANDLE_FILE_INFORMATION()
                    if not kernel32.GetFileInformationByHandle(h, byref(info)):
                        err = ctypes.get_last_error()
                        raise ExecutionError(f"Failed to inspect created file handle '{path}': WinError {err}")
                    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                        raise ValidationError(f"Created file is an unexpected symlink or reparse point: '{path}'")
                    final_p = get_final_path_from_handle(h)
                    try:
                        common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_p)])
                    except ValueError:
                        raise ValidationError(f"Created file resolves outside configured root: '{final_p}'")
                    if common != os.path.normcase(norm_root):
                        raise ValidationError(f"Created file resolves outside configured root: '{final_p}'")

                    write_all_bytes_to_handle(h, raw_bytes)
                finally:
                    kernel32.CloseHandle(h)

                if is_symlink_or_reparse_point(norm_path):
                    safe_unlink(norm_path, norm_root)
                    raise ValidationError(f"Created file is an unexpected symlink or reparse point: '{path}'")

        else:
            # Replacement: Verify target exists and is a regular file
            verify_boundary_and_reparse(norm_path, root_path, must_exist=True)

            if sys.platform != "win32":
                raise ExecutionError(
                    "Atomic target-bound file replacement requires Windows NT kernel handle sharing and directory pinning semantics and are unsupported on non-Windows platforms."
                )

            with pin_directory_hierarchy(parent_dir, norm_root):
                temp_filename = f".pico_tmp_{uuid.uuid4().hex}"
                temp_path = os.path.join(parent_dir, temp_filename)
                backup_filename = f".pico_backup_{uuid.uuid4().hex}"
                backup_path = os.path.join(parent_dir, backup_filename)

                backup_created = False
                h_temp = None
                try:
                    # 1. Target verification via handle with EXCLUSIVE access (no write, no delete sharing)
                    h_target = kernel32.CreateFileW(
                        norm_path,
                        GENERIC_READ | DELETE,
                        FILE_SHARE_READ,
                        None,
                        OPEN_EXISTING,
                        FILE_FLAG_OPEN_REPARSE_POINT,
                        None,
                    )
                    if is_invalid_handle(h_target):
                        err = ctypes.get_last_error()
                        raise ExecutionError(f"Failed to open target file for replacement '{path}': WinError {err}")
                    try:
                        info_tgt = BY_HANDLE_FILE_INFORMATION()
                        if not kernel32.GetFileInformationByHandle(h_target, byref(info_tgt)):
                            err = ctypes.get_last_error()
                            raise ExecutionError(f"Failed to inspect target handle '{path}': WinError {err}")
                        if info_tgt.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                            raise ValidationError(f"Target cannot be a symlink or reparse point: '{path}'")
                        final_target_p = get_final_path_from_handle(h_target)
                        try:
                            common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_target_p)])
                        except ValueError:
                            raise ValidationError(f"Target resolves outside root: '{final_target_p}'")
                        if common != os.path.normcase(norm_root):
                            raise ValidationError(f"Target resolves outside root: '{final_target_p}'")

                        if expected_original_digest:
                            target_bytes = read_all_bytes_from_handle(h_target)
                            target_text = target_bytes.decode("utf-8", errors="replace")
                            current_digest = compute_content_digest(target_text)
                            if current_digest != expected_original_digest:
                                raise ExecutionError(
                                    f"Target file content modified prior to atomic replacement. Expected '{expected_original_digest}', found '{current_digest}'."
                                )

                        # 2. Stage new content exclusively in pinned parent (with DELETE for handle rename/disposition)
                        h_temp = kernel32.CreateFileW(
                            temp_path,
                            GENERIC_READ | GENERIC_WRITE | DELETE,
                            FILE_SHARE_READ,
                            None,
                            CREATE_NEW,
                            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                            None,
                        )
                        if is_invalid_handle(h_temp):
                            err = ctypes.get_last_error()
                            raise ExecutionError(f"Failed to create temporary file for atomic write: WinError {err}")

                        info_tmp = BY_HANDLE_FILE_INFORMATION()
                        if not kernel32.GetFileInformationByHandle(h_temp, byref(info_tmp)):
                            err = ctypes.get_last_error()
                            raise ExecutionError(f"Failed to inspect temp file handle: WinError {err}")
                        if info_tmp.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                            raise ValidationError("Temporary file created as unexpected reparse point.")
                        final_temp_p = get_final_path_from_handle(h_temp)
                        try:
                            common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_temp_p)])
                        except ValueError:
                            raise ValidationError(f"Temporary file resolves outside root: '{final_temp_p}'")
                        if common != os.path.normcase(norm_root):
                            raise ValidationError(f"Temporary file resolves outside root: '{final_temp_p}'")

                        write_all_bytes_to_handle(h_temp, raw_bytes)
                        kernel32.FlushFileBuffers(h_temp)

                        # 3. Vacate target path by renaming verified target to backup via open handle
                        fri_target = FILE_RENAME_INFO()
                        fri_target.ReplaceIfExists = False
                        fri_target.RootDirectory = None
                        fri_target.FileNameLength = len(backup_path) * 2
                        fri_target.FileName = backup_path
                        res_target = kernel32.SetFileInformationByHandle(
                            h_target, FileRenameInfo, byref(fri_target), sizeof(fri_target)
                        )
                        if res_target == 0:
                            err = ctypes.get_last_error()
                            raise ExecutionError(f"Failed to relocate target to backup for atomic write: WinError {err}")
                        backup_created = True
                    finally:
                        kernel32.CloseHandle(h_target)

                    # Target handle is now closed and target is vacated.
                    # Hooks for deterministic adversarial testing of race condition
                    if getattr(self, "_on_pre_replace_move", None) is not None:
                        self._on_pre_replace_move(norm_path)
                    if getattr(self, "_on_pre_temp_rename", None) is not None:
                        self._on_pre_temp_rename(temp_path, norm_path)

                    # 4. Rename h_temp directly to norm_path WITHOUT replacing existing file
                    fri_temp = FILE_RENAME_INFO()
                    fri_temp.ReplaceIfExists = False
                    fri_temp.RootDirectory = None
                    fri_temp.FileNameLength = len(norm_path) * 2
                    fri_temp.FileName = norm_path
                    res_temp = kernel32.SetFileInformationByHandle(
                        h_temp, FileRenameInfo, byref(fri_temp), sizeof(fri_temp)
                    )
                    if res_temp == 0:
                        err = ctypes.get_last_error()
                        # Safe handle-bound cleanup of h_temp: mark for disposition and close
                        disp = FILE_DISPOSITION_INFO(True)
                        kernel32.SetFileInformationByHandle(
                            h_temp, FileDispositionInfo, byref(disp), sizeof(disp)
                        )
                        kernel32.CloseHandle(h_temp)
                        h_temp = None
                        safe_unlink(temp_path, norm_root)
                        raise ExecutionError(
                            f"Target file was concurrently created or substituted prior to replacement (WinError {err}). "
                            f"Displaced original preserved in '{backup_path}'; mutation aborted with zero overwrite."
                        )

                    # Successfully renamed h_temp to norm_path. Close handle before post-verification.
                    kernel32.CloseHandle(h_temp)
                    h_temp = None

                    # Hook for deterministic adversarial testing after move but before post-write verification
                    if getattr(self, "_on_post_replace_move", None) is not None:
                        self._on_post_replace_move(norm_path)

                    # 5. Independent post-write verification via handle BEFORE backup cleanup
                    h_post = kernel32.CreateFileW(
                        norm_path,
                        GENERIC_READ,
                        FILE_SHARE_READ,
                        None,
                        OPEN_EXISTING,
                        FILE_FLAG_OPEN_REPARSE_POINT,
                        None,
                    )
                    if is_invalid_handle(h_post):
                        err = ctypes.get_last_error()
                        raise ExecutionError(
                            f"Post-write verification failed to open file '{path}': WinError {err}. "
                            f"Displaced original preserved in '{backup_path}'."
                        )
                    try:
                        info_post = BY_HANDLE_FILE_INFORMATION()
                        if not kernel32.GetFileInformationByHandle(h_post, byref(info_post)):
                            err = ctypes.get_last_error()
                            raise ExecutionError(
                                f"Post-write verification failed to inspect handle: WinError {err}. "
                                f"Displaced original preserved in '{backup_path}'."
                            )
                        if info_post.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                            raise ValidationError(
                                f"Post-write file is an unexpected symlink or reparse point: '{path}'. "
                                f"Displaced original preserved in '{backup_path}'."
                            )
                        final_post_p = get_final_path_from_handle(h_post)
                        try:
                            common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_post_p)])
                        except ValueError:
                            raise ValidationError(
                                f"Post-write file resolves outside root: '{final_post_p}'. "
                                f"Displaced original preserved in '{backup_path}'."
                            )
                        if common != os.path.normcase(norm_root):
                            raise ValidationError(
                                f"Post-write file resolves outside root: '{final_post_p}'. "
                                f"Displaced original preserved in '{backup_path}'."
                            )
                        written_bytes = read_all_bytes_from_handle(h_post)
                        written_digest = compute_content_digest(written_bytes.decode("utf-8", errors="replace"))
                        if written_digest != target_proposed_digest:
                            raise ExecutionError(
                                f"Post-write verification failed: Resulting file digest '{written_digest}' does not match expected '{target_proposed_digest}'. "
                                f"Displaced original preserved in '{backup_path}'."
                            )
                    finally:
                        kernel32.CloseHandle(h_post)

                    # 6. ONLY on successful post-write verification, safely clean up backup file
                    safe_unlink(backup_path, norm_root)
                    if os.path.lexists(backup_path):
                        raise ExecutionError(
                            f"Post-write verification succeeded, but backup file '{backup_path}' could not be safely removed. "
                            f"Displaced original preserved in '{backup_path}'; cleanup conflict."
                        )

                except Exception:
                    # Clean up h_temp if still open
                    if h_temp is not None and not is_invalid_handle(h_temp):
                        try:
                            disp = FILE_DISPOSITION_INFO(True)
                            kernel32.SetFileInformationByHandle(
                                h_temp, FileDispositionInfo, byref(disp), sizeof(disp)
                            )
                        except Exception:
                            pass
                        try:
                            kernel32.CloseHandle(h_temp)
                        except Exception:
                            pass
                        h_temp = None
                        safe_unlink(temp_path, norm_root)
                    raise

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

        parent_dir = os.path.dirname(norm_path)

        if sys.platform != "win32":
            raise ExecutionError(
                "Handle-bound atomic file deletion requires Windows NT kernel handle sharing semantics and is unsupported on non-Windows platforms."
            )

        with pin_directory_hierarchy(parent_dir, norm_root):
            h = kernel32.CreateFileW(
                norm_path,
                DELETE | FILE_READ_DATA,
                FILE_SHARE_READ,  # Exclusive: NO FILE_SHARE_WRITE, NO FILE_SHARE_DELETE
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if is_invalid_handle(h):
                err = ctypes.get_last_error()
                if err in (2, 3):  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
                    return
                raise ExecutionError(f"Failed to open file for deletion '{path}': WinError {err}")

            try:
                info = BY_HANDLE_FILE_INFORMATION()
                if not kernel32.GetFileInformationByHandle(h, byref(info)):
                    err = ctypes.get_last_error()
                    raise ExecutionError(f"Failed to inspect file handle '{path}': WinError {err}")

                if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValidationError(f"Cannot delete symlink or reparse point: '{path}'")

                final_p = get_final_path_from_handle(h)
                try:
                    common = os.path.commonpath([os.path.normcase(norm_root), os.path.normcase(final_p)])
                except ValueError:
                    raise ValidationError(f"Target path resolves outside configured root: '{final_p}'")
                if common != os.path.normcase(norm_root):
                    raise ValidationError(f"Target path resolves outside configured root: '{final_p}'")

                if expected_digest:
                    target_bytes = read_all_bytes_from_handle(h)
                    current_digest = compute_content_digest(target_bytes.decode("utf-8", errors="replace"))
                    if current_digest != expected_digest:
                        raise ExecutionError(
                            f"Target file content modified prior to deletion. Expected '{expected_digest}', found '{current_digest}'."
                        )

                disp = FILE_DISPOSITION_INFO(True)
                res = kernel32.SetFileInformationByHandle(
                    h, FileDispositionInfo, byref(disp), sizeof(disp)
                )
                if res == 0:
                    err = ctypes.get_last_error()
                    raise ExecutionError(f"Failed to mark file for deletion '{path}': WinError {err}")
            finally:
                kernel32.CloseHandle(h)

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
        expected_proposed_digest: str | None = None,
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
