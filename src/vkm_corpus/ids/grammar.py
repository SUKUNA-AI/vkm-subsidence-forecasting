"""ID grammar of the VKM document layer (CP-16 p. 3): regular expressions only, no imports from the platform.

IDs never become file names (``:`` is invalid on Windows): files are addressed by content hash or by ``key=value``
partitions. New prefixes do not collide with the ones already taken in Phase 1 (``VKM-SRC-``, ``EXT-SRC-``,
``EXTWEB-``, ``EV-…``, ``PAR-``, ``CW-``, ``WG-``, ``CG-``, ``LIN-``, ``FAM-``, ``PWL-``, ``MM-``, ``MGEO-``, ``MON-``,
``PCF-``, ``QA-C-``, ``XF-``, ``PC-``, ``F-…``).
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

SHA256_HEX = r"^[0-9a-f]{64}$"
SOURCE_ID = r"^VKM-SRC-[0-9]{3}$"
DOCUMENT_ID = r"^VKM-SRC-[0-9]{3}:doc$"
PAGE_ID = r"^VKM-SRC-[0-9]{3}:[prs][0-9]{4}$"
# page-scoped objects: <page_id>:<kind><12 hex>; document-scoped DOCX objects (H-50): <source_id>:doc:<kind><12 hex>
OBJECT_ID = r"^VKM-SRC-[0-9]{3}:(?:[prs][0-9]{4}|doc):[bftmc][0-9a-f]{12}$"
WORK_ID = r"^VKM-WRK-[0-9]{3}$"
AUTHOR_ID = r"^AUT-[0-9a-f]{12}$"
VENUE_ID = r"^VEN-[0-9a-f]{12}$"
ARTIFACT_ID = r"^sha256:[0-9a-f]{64}$"
RUN_ID = r"^RUN-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
STEP_ID = r"^STP-[0-9a-f]{16}$"
ERROR_ID = r"^ERR-[0-9a-f]{16}$"
COMMIT_ID = r"^CMT-[0-9a-f]{16}$"
SNAPSHOT_ID = r"^snap-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
LINK_ID = r"^(?:SWL|WRL|SRL|WAU|BML|OLN)-[0-9a-f]{16}$"
COMMIT_KEY = r"^(?:VKM-SRC-[0-9]{3}|REGISTRY)$"
SEMVER = r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"

# reserved (invalid in v0; adding them is a MINOR change that only widens a regular expression)
RESERVED = {
    "source_4_digits": r"^VKM-SRC-[0-9]{4}$",
    "container_child_work": r"^VKM-WRK-[0-9]{3}\.[0-9]{2}$",
    "work_without_source": r"^VKM-WRK-X[0-9]{5}$",
    "zip_member": r"^VKM-SRC-[0-9]{3}/m[0-9]{3}$",
    "page_region": r"^VKM-SRC-[0-9]{3}:[prs][0-9]{4}#bbox=[0-9.]+,[0-9.]+,[0-9.]+,[0-9.]+$",
    "text_span": r"^VKM-SRC-[0-9]{3}:(?:[prs][0-9]{4}|doc):[bftmc][0-9a-f]{12}#chars=[0-9]+-[0-9]+@[0-9a-f]{12}$",
}

PATTERNS: dict[str, str] = {
    "sha256": SHA256_HEX, "source": SOURCE_ID, "document": DOCUMENT_ID, "page": PAGE_ID, "object": OBJECT_ID,
    "work": WORK_ID, "author": AUTHOR_ID, "venue": VENUE_ID, "artifact": ARTIFACT_ID, "run": RUN_ID,
    "step": STEP_ID, "error": ERROR_ID, "commit": COMMIT_ID, "snapshot": SNAPSHOT_ID, "link": LINK_ID,
    "commit_key": COMMIT_KEY, "semver": SEMVER,
}
COMPILED: dict[str, re.Pattern[str]] = {k: re.compile(v) for k, v in PATTERNS.items()}

LOGICAL_ROOTS = ("PRIVATE", "PUBLIC", "DATA", "WORK", "EDGE")


def matches(kind: str, value: object) -> bool:
    """True if ``value`` is a string of the given ID kind (keys of ``PATTERNS``)."""
    return isinstance(value, str) and COMPILED[kind].fullmatch(value) is not None


def check_relative_path(value: str) -> str:
    """POSIX path relative to a logical root: no drive, no leading slash, no backslash, no ``..``."""
    if "\\" in value or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute():
        raise ValueError(f"path must be relative POSIX, got {value!r}")
    if any(part == ".." for part in PurePosixPath(value).parts):
        raise ValueError(f"path must not escape its root: {value!r}")
    return value


def check_logical_ref(value: str) -> str:
    """``<ROOT>:<relative posix path>`` with ROOT in ``LOGICAL_ROOTS`` (e.g. ``PRIVATE:00_registry/SOURCE_REGISTER.csv``)."""
    root, sep, rest = value.partition(":")
    if not sep or root not in LOGICAL_ROOTS or not rest:
        raise ValueError(f"logical reference must look like ROOT:relative/path with ROOT in {LOGICAL_ROOTS}: {value!r}")
    check_relative_path(rest.split("#", 1)[0])
    return value
