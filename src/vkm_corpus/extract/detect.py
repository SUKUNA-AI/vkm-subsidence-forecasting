"""File inspection: existence, LFS pointer, size, sha256, format by signature (never by extension).

Signatures: ``%PDF-`` within the first 1024 bytes (bytes before it → ``SIGNATURE_AFTER_BOM`` for a UTF-8 BOM, otherwise
``LEADING_BYTES_BEFORE_HEADER``); ``AT&TFORM`` → DjVu; ZIP with ``mimetype`` = ``application/epub+zip`` → EPUB; ZIP
with ``[Content_Types].xml`` and ``word/document.xml`` → DOCX; other ZIP → ZIP; PNG/JPEG/TIFF → IMAGE.
"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

LFS_PREFIX = b"version https://git-lfs.github.com/spec/"
BOM = b"\xef\xbb\xbf"


@dataclass
class FileInspection:
    path_exists: bool
    size_bytes: int | None = None
    sha256: str | None = None
    is_lfs_pointer: bool = False
    file_format: str = "UNKNOWN"          # PDF, DJVU, EPUB, DOCX, ZIP, IMAGE, UNKNOWN
    format_version: str | None = None
    container_detail: str | None = None   # DjVu: bundled/single; ZIP: members count
    leading_bytes: int = 0
    flags: list[str] = field(default_factory=list)


def sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sniff(head: bytes, path: Path | None = None) -> tuple[str, str | None, str | None, int, list[str]]:
    """Format from the first bytes (and the ZIP directory when needed)."""
    flags: list[str] = []
    if head.startswith(LFS_PREFIX):
        return "UNKNOWN", None, "LFS_POINTER", 0, ["LFS_POINTER_ONLY"]
    pos = head.find(b"%PDF-", 0, 1024 + 5)
    if pos >= 0:
        version = head[pos + 5:pos + 8].decode("latin-1", "replace")
        if pos > 0:
            flags.append("SIGNATURE_AFTER_BOM" if head[:pos] == BOM else "LEADING_BYTES_BEFORE_HEADER")
        return "PDF", version, None, pos, flags
    if head[:8] == b"AT&TFORM":
        kind = head[12:16].decode("latin-1", "replace")
        detail = {"DJVM": "bundled", "DJVU": "single_page", "DJVI": "shared_only"}.get(kind, kind)
        return "DJVU", None, detail, 0, flags
    if head[:4] == b"PK\x03\x04" and path is not None:
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "mimetype" in names and z.read("mimetype").strip() == b"application/epub+zip":
                    return "EPUB", None, None, 0, flags
                if "[Content_Types].xml" in names and "word/document.xml" in names:
                    return "DOCX", None, None, 0, flags
                return "ZIP", None, f"members={len(names)}", 0, flags
        except zipfile.BadZipFile:
            return "UNKNOWN", None, "BAD_ZIP", 0, ["UNREADABLE"]
    if head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"II*\x00", b"MM\x00*"):
        return "IMAGE", None, None, 0, flags
    return "UNKNOWN", None, None, 0, flags


def inspect_file(path: Path, expected_sha256: str | None = None, *, hash_file: bool = True) -> FileInspection:
    path = Path(path)
    if not path.exists():
        return FileInspection(path_exists=False, flags=["MISSING"])
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(2048)
    fmt, version, detail, lead, flags = sniff(head, path)
    info = FileInspection(path_exists=True, size_bytes=size, file_format=fmt, format_version=version,
                          container_detail=detail, leading_bytes=lead, flags=list(flags))
    if "LFS_POINTER_ONLY" in flags:
        info.is_lfs_pointer = True
        return info
    if hash_file:
        info.sha256 = sha256_file(path)
        if expected_sha256 and info.sha256 != expected_sha256.lower():
            info.flags.append("SHA256_MISMATCH")
    return info
