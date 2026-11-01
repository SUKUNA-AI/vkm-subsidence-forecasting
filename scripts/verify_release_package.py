"""Verify an immutable local artifact manifest and declared ZIP member payloads.

This checks delivered bytes, not scientific validity or human acceptance.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


def digest(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(block)
    return h.hexdigest()


def safe_relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError(f"non-relative manifest path: {value!r}")
    return path


def verify(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = set()
    for record in manifest["files"]:
        relative = safe_relative(record["path"])
        if str(relative) in paths:
            raise ValueError(f"duplicate manifest path: {relative}")
        paths.add(str(relative))
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError(f"path escapes artifact root: {relative}")
        if target.stat().st_size != record["size"]:
            raise ValueError(f"payload size mismatch: {relative}")
        with target.open("rb") as stream:
            if digest(stream) != record["sha256"]:
                raise ValueError(f"payload hash mismatch: {relative}")
    members_checked = 0
    for archive in manifest.get("archives", []):
        archive_path = str(safe_relative(archive["path"]))
        index_path = str(safe_relative(archive["index"]))
        if archive_path not in paths or index_path not in paths:
            raise ValueError("archive and member index must be covered by manifest")
        index = json.loads((root / index_path).read_text(encoding="utf-8"))
        declared = {str(safe_relative(row["path"])): row for row in index["members"]}
        if len(declared) != len(index["members"]):
            raise ValueError("duplicate declared ZIP member")
        with zipfile.ZipFile(root / archive_path) as z:
            names = z.namelist()
            if len(set(names)) != len(names) or set(names) != set(declared):
                raise ValueError(f"ZIP inventory mismatch: {archive_path}")
            for name, row in declared.items():
                if z.getinfo(name).file_size != row["size"]:
                    raise ValueError(f"ZIP member size mismatch: {name}")
                with z.open(name) as stream:
                    if digest(stream) != row["sha256"]:
                        raise ValueError(f"ZIP member hash mismatch: {name}")
                members_checked += 1
    return {"status": "PASS_FOR_PAYLOAD_INTEGRITY_ONLY", "files": len(paths),
            "archive_members": members_checked, "schema": manifest["schema"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.manifest), ensure_ascii=True))
