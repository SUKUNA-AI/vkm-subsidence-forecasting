"""Offline index of the Mechanical scripting API from ``ansys-mechanical-stubs`` (``mechanical_api_search``).

The stubs package ships one generated Python file per .NET namespace (``v261/Ansys/ACT/Automation/Mechanical/…``)
with classes, properties (``@property`` + optional setter), methods (``@typing.overload`` variants) and enums. Importing
it would load ~38 MB of Python per release, so the files are scanned line by line instead; the index is built once
per process and release. Nothing here starts Mechanical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

_CLASS = re.compile(r"^class (\w+)\((.*)\):\s*$")
_DEF = re.compile(r"^    def (\w+)\((.*)\)\s*(?:->\s*(.+?))?\s*:\s*$")
_ENUM_MEMBER = re.compile(r"^    (\w+) = (-?\d+)\s*$")
_WORD = re.compile(r"[A-Za-z0-9]+")
KINDS = ("class", "enum", "enum_member", "property", "method")


@dataclass(frozen=True)
class ApiEntry:
    kind: str           # class | enum | enum_member | property | method
    name: str
    qualname: str       # Ansys.ACT.Automation.Mechanical.Body.AddCommandSnippet
    signature: str      # "(self) -> Ansys.ACT.Automation.Mechanical.CommandSnippet", "= 3", "(settable) -> T"
    doc: str            # first docstring line

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "qualname": self.qualname, "signature": self.signature, "doc": self.doc}


def stubs_root() -> Path | None:
    """``…/site-packages/ansys/mechanical/stubs`` located through package metadata (the package is not imported)."""
    import importlib.metadata as md

    try:
        dist = md.distribution("ansys-mechanical-stubs")
    except md.PackageNotFoundError:
        return None
    root = Path(str(dist.locate_file("ansys/mechanical/stubs")))
    return root if root.is_dir() else None


def releases(root: Path | None = None) -> list[str]:
    root = root or stubs_root()
    return sorted(p.name[1:] for p in root.iterdir() if p.is_dir() and re.fullmatch(r"v\d{3}", p.name)) if root else []


def _clean_sig(args: str, ret: str | None) -> str:
    args = re.sub(r"\s+", " ", args.strip())
    return f"({args})" + (f" -> {ret.strip()}" if ret else "")


def _doc_after(lines: list[str], i: int) -> str:
    """First text line of the docstring that starts at line i (or i+1)."""
    for j in range(i, min(i + 3, len(lines))):
        s = lines[j].strip()
        if s.startswith('"""'):
            body = s[3:]
            if body.endswith('"""') and len(body) >= 3:
                return body[:-3].strip()
            if body:
                return body.strip()
            for k in range(j + 1, min(j + 12, len(lines))):
                t = lines[k].strip()
                if t.startswith('"""'):
                    return ""
                if t:
                    return t.rstrip('"').strip()
            return ""
    return ""


def scan_file(path: Path, module: str) -> Iterable[ApiEntry]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    cls: str | None = None
    cls_is_enum = False
    decorators: list[str] = []
    seen_overload: set[str] = set()
    for i, line in enumerate(lines):
        m = _CLASS.match(line)
        if m:
            cls, bases = m.group(1), m.group(2)
            cls_is_enum = "Enum" in bases
            seen_overload = set()
            yield ApiEntry("enum" if cls_is_enum else "class", cls, f"{module}.{cls}", f"({bases})",
                           _doc_after(lines, i + 1))
            decorators = []
            continue
        if cls is None:
            continue
        if line.startswith("    @"):
            decorators.append(line.strip())
            continue
        if cls_is_enum:
            m = _ENUM_MEMBER.match(line)
            if m:
                yield ApiEntry("enum_member", m.group(1), f"{module}.{cls}.{m.group(1)}", f"= {m.group(2)}", "")
            continue
        m = _DEF.match(line)
        if m:
            name, args, ret = m.group(1), m.group(2), m.group(3)
            decos, decorators = decorators, []
            if name.startswith("_") and name != "__init__":
                continue
            if any(d.endswith(".setter") for d in decos):
                continue
            if "@property" in decos:
                settable = any(f"@{name}.setter" == lines[k].strip() for k in range(i + 1, min(i + 12, len(lines))))
                yield ApiEntry("property", name, f"{module}.{cls}.{name}",
                               f"{'(settable) ' if settable else ''}-> {ret or 'Any'}", _doc_after(lines, i + 1))
                continue
            overload = any(d in ("@typing.overload", "@overload") for d in decos)
            if not overload and name in seen_overload:
                continue      # the generic implementation after its overloads
            if overload:
                seen_overload.add(name)
            yield ApiEntry("method", name, f"{module}.{cls}.{name}", _clean_sig(args, ret), _doc_after(lines, i + 1))
        elif line and not line.startswith(" "):
            cls = None


@lru_cache(maxsize=4)
def build_index(root_text: str, release: str) -> tuple[ApiEntry, ...]:
    base = Path(root_text) / f"v{release}"
    entries: list[ApiEntry] = []
    for path in sorted(base.rglob("__init__.py")):
        rel = path.parent.relative_to(base)
        module = ".".join(rel.parts)
        if not module:
            continue
        entries.extend(scan_file(path, module))
    return tuple(entries)


def search(query: str, *, release: str = "261", limit: int = 20, kinds: Iterable[str] | None = None,
           root: Path | None = None) -> dict:
    root = root or stubs_root()
    if root is None:
        return {"available": False, "reason": "ansys-mechanical-stubs is not installed in this interpreter"}
    if not (root / f"v{release}").is_dir():
        return {"available": False, "reason": f"no stubs for release {release}", "releases": releases(root)}
    index = build_index(str(root), release)
    words = [w.lower() for w in _WORD.findall(query)]
    if not words:
        return {"available": True, "release": release, "entries": len(index), "matches": 0, "results": []}
    wanted = set(kinds or KINDS)
    scored = []
    for e in index:
        if e.kind not in wanted:
            continue
        name, qual, doc = e.name.lower(), e.qualname.lower(), e.doc.lower()
        score = 0
        for w in words:
            if name == w:
                s = 100
            elif name.startswith(w):
                s = 60
            elif w in name:
                s = 40
            elif w in qual:
                s = 15
            elif w in doc:
                s = 5
            else:
                score = -1
                break
            score += s
        if score > 0:
            if " ".join(words) == name or "".join(words) == name:
                score += 50
            scored.append((-score, KINDS.index(e.kind), e.qualname, e))
    scored.sort(key=lambda t: t[:3])
    return {"available": True, "release": release, "entries": len(index), "matches": len(scored),
            "results": [t[3].as_dict() for t in scored[:limit]]}
