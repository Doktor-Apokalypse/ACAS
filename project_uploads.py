"""Safe, non-executing ingestion of uploaded files, folders and ZIP archives."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO, Sequence


IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    ".vscode",
    "__pycache__",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
    "venv",
}
IGNORED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
READ_CHUNK_BYTES = 64 * 1024
MAX_PROJECT_PATH_CHARS = 500
MAX_PROJECT_COMPONENT_CHARS = 255


class ProjectUploadError(ValueError):
    """Raised when an uploaded project is invalid or exceeds a safety limit."""


class ProjectUploadTooLarge(ProjectUploadError):
    """Raised when an upload exceeds a configured resource limit."""


@dataclass(frozen=True)
class UploadLimits:
    max_archive_bytes: int
    max_expanded_bytes: int
    max_file_bytes: int
    max_files: int
    max_compression_ratio: float


@dataclass(frozen=True)
class ProjectFile:
    path: str
    content: bytes
    size_bytes: int
    sha256: str
    is_binary: bool


@dataclass(frozen=True)
class ProjectBundle:
    name: str
    source_kind: str
    files: tuple[ProjectFile, ...]
    total_bytes: int
    skipped_files: int


def safe_project_name(value: str, fallback: str = "Uploaded project") -> str:
    """Return a short display name without path or control characters."""
    leaf = re.split(r"[/\\]", value.strip())[-1]
    leaf = unicodedata.normalize("NFC", leaf)
    leaf = "".join(character for character in leaf if ord(character) >= 32)
    leaf = re.sub(r"\s+", " ", leaf).strip(" .")
    return (leaf or fallback)[:200]


def normalize_project_path(value: str) -> str:
    """Validate and normalize an untrusted archive/browser relative path."""
    if not isinstance(value, str):
        raise ProjectUploadError("Every uploaded file must have a relative path")
    normalized = unicodedata.normalize("NFC", value).replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ProjectUploadError("Uploaded file paths must be relative")
    if any(ord(character) < 32 for character in normalized):
        raise ProjectUploadError("Uploaded file paths cannot contain control characters")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ProjectUploadError("Uploaded file paths cannot contain . or .. components")
    if any(len(part) > MAX_PROJECT_COMPONENT_CHARS for part in parts):
        raise ProjectUploadError("An uploaded file path component is too long")
    result = "/".join(parts)
    if len(result) > MAX_PROJECT_PATH_CHARS:
        raise ProjectUploadError("An uploaded file path is too long")
    return result


def path_is_ignored(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(
        parts
        and (
            parts[-1] in IGNORED_FILE_NAMES
            or any(part.casefold() in IGNORED_DIRECTORY_NAMES for part in parts[:-1])
        )
    )


def content_is_binary(content: bytes) -> bool:
    sample = content[:8192]
    if b"\0" in sample:
        return True
    if not sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def read_limited(stream: BinaryIO, maximum: int, label: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = stream.read(min(READ_CHUNK_BYTES, maximum - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise ProjectUploadTooLarge(f"{label} exceeds the {maximum:,}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def project_file(path: str, content: bytes) -> ProjectFile:
    return ProjectFile(
        path=path,
        content=content,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        is_binary=content_is_binary(content),
    )


def ensure_unique_path(path: str, seen: set[str]) -> None:
    folded = path.casefold()
    if folded in seen:
        raise ProjectUploadError(f"The project contains a duplicate path: {path}")
    seen.add(folded)


def validate_zip_member_type(info: zipfile.ZipInfo) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type and not stat.S_ISREG(unix_mode) and not stat.S_ISDIR(unix_mode):
        raise ProjectUploadError(
            f"ZIP links and special files are not accepted: {info.filename}"
        )
    if info.flag_bits & 0x1:
        raise ProjectUploadError(f"Encrypted ZIP entries are not accepted: {info.filename}")


def ingest_zip(
    stream: BinaryIO,
    filename: str,
    limits: UploadLimits,
) -> ProjectBundle:
    archive_data = read_limited(stream, limits.max_archive_bytes, "ZIP upload")
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProjectUploadError("The selected file is not a valid ZIP archive") from exc

    files: list[ProjectFile] = []
    total_bytes = 0
    skipped = 0
    seen: set[str] = set()
    with archive:
        members = archive.infolist()
        regular_members = [member for member in members if not member.is_dir()]
        if len(regular_members) > limits.max_files:
            raise ProjectUploadTooLarge(
                f"The ZIP contains more than {limits.max_files:,} files"
            )
        for info in members:
            validate_zip_member_type(info)
            if info.is_dir():
                continue
            path = normalize_project_path(info.filename)
            ensure_unique_path(path, seen)
            if path_is_ignored(path):
                skipped += 1
                continue
            if info.file_size > limits.max_file_bytes:
                raise ProjectUploadTooLarge(
                    f"{path} exceeds the {limits.max_file_bytes:,}-byte per-file limit"
                )
            if info.file_size and info.file_size / max(1, info.compress_size) > limits.max_compression_ratio:
                raise ProjectUploadError(
                    f"{path} has an unsafe ZIP compression ratio"
                )
            total_bytes += info.file_size
            if total_bytes > limits.max_expanded_bytes:
                raise ProjectUploadTooLarge(
                    "The expanded project exceeds the "
                    f"{limits.max_expanded_bytes:,}-byte limit"
                )
            try:
                with archive.open(info, "r") as member_stream:
                    content = read_limited(
                        member_stream,
                        limits.max_file_bytes,
                        path,
                    )
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ProjectUploadError(f"Could not safely read {path} from the ZIP") from exc
            if len(content) != info.file_size:
                raise ProjectUploadError(f"ZIP size metadata did not match {path}")
            files.append(project_file(path, content))

    if not files:
        raise ProjectUploadError("The ZIP does not contain any usable project files")
    display_name = safe_project_name(filename)
    if display_name.casefold().endswith(".zip"):
        display_name = display_name[:-4].rstrip(" .") or "Uploaded project"
    return ProjectBundle(display_name, "zip", tuple(files), total_bytes, skipped)


def decode_relative_paths(value: str, expected_count: int) -> list[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProjectUploadError("Folder path metadata is invalid") from exc
    if not isinstance(decoded, list) or len(decoded) != expected_count:
        raise ProjectUploadError("Folder path metadata does not match the uploaded files")
    if not all(isinstance(item, str) for item in decoded):
        raise ProjectUploadError("Folder path metadata must contain only strings")
    return decoded


def ingest_folder(
    uploads: Sequence[tuple[str, BinaryIO]],
    limits: UploadLimits,
) -> ProjectBundle:
    if not uploads:
        raise ProjectUploadError("Select at least one project file")
    if len(uploads) > limits.max_files:
        raise ProjectUploadTooLarge(
            f"The upload contains more than {limits.max_files:,} files"
        )
    files: list[ProjectFile] = []
    total_bytes = 0
    skipped = 0
    seen: set[str] = set()
    first_path: str | None = None
    for raw_path, stream in uploads:
        path = normalize_project_path(raw_path)
        first_path = first_path or path
        ensure_unique_path(path, seen)
        if path_is_ignored(path):
            skipped += 1
            continue
        remaining_total = limits.max_expanded_bytes - total_bytes
        if remaining_total < 0:
            raise ProjectUploadTooLarge(
                f"The project exceeds the {limits.max_expanded_bytes:,}-byte limit"
            )
        content = read_limited(
            stream,
            min(limits.max_file_bytes, remaining_total),
            path,
        )
        total_bytes += len(content)
        files.append(project_file(path, content))
    if not files:
        raise ProjectUploadError("The upload does not contain any usable project files")
    first_parts = PurePosixPath(first_path or "").parts
    name = safe_project_name(first_parts[0] if len(first_parts) > 1 else "Uploaded folder")
    return ProjectBundle(name, "folder", tuple(files), total_bytes, skipped)


def ingest_files(
    uploads: Sequence[tuple[str, BinaryIO]],
    limits: UploadLimits,
) -> ProjectBundle:
    """Ingest a flat file selection using the existing unarchived-project storage.

    The database's 'folder' kind represents unarchived files; both pickers share
    its storage, ownership, quotas and analysis path without a schema migration.
    """
    for filename, _stream in uploads:
        path = normalize_project_path(filename)
        if "/" in path:
            raise ProjectUploadError("Select files by filename; use folder upload to preserve directories")
    bundle = ingest_folder(uploads, limits)
    name = safe_project_name(bundle.files[0].path) if len(bundle.files) == 1 else f"Uploaded files ({len(bundle.files)})"
    return ProjectBundle(name, bundle.source_kind, bundle.files, bundle.total_bytes, bundle.skipped_files)
