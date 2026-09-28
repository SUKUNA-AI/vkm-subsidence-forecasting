"""The eight ``vkm-drawio`` operations as plain Python (the MCP layer only wraps them).

Every method returns a JSON-ready dict or raises :class:`~vkm_drawio.errors.ToolFailure`. Paths in results are
root-relative (``public:…`` / ``work:…``); machine paths never leave the process.
"""
from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from vkm_drawio import __version__
from vkm_drawio import drawio_cli
from vkm_drawio.build import build_diagram
from vkm_drawio.errors import ToolFailure
from vkm_drawio.locate import DrawioLocation, locate
from vkm_drawio.model import DiagramSpec
from vkm_drawio.ops import Operation, apply_ops
from vkm_drawio.policy import policy_problems
from vkm_drawio.styles import is_html, value_to_text
from vkm_drawio.workspace import READABLE_SUFFIXES, Workspace, atomic_write, suffix_of
from vkm_drawio.xmlio import (Cell, Diagram, DiagramParseError, content_bbox, page_summary, read_diagram_bytes,
                              write_diagram)

EXPORT_FORMATS = ("png", "svg", "pdf", "jpg")
PREVIEW_MAX_SIDE = 1568
PREVIEW_MAX_BYTES = 3 * 1024 * 1024
WRAPPER_META = ("tooltip", "link", "placeholders")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def _stem(path: Path) -> str:
    name = path.name
    for suffix in (".drawio.svg", ".drawio.png", ".drawio", ".xml", ".svg", ".png"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


class DrawioService:
    def __init__(self, workspace: Workspace, locator: Callable[[], DrawioLocation] = locate,
                 cli: Any = drawio_cli) -> None:
        self.ws = workspace
        self._locator = locator
        self._location: DrawioLocation | None = None
        self.cli = cli

    # -------------------------------------------------------------------------------------------- draw.io binary
    def location(self) -> DrawioLocation:
        if self._location is None:
            self._location = self._locator()
        return self._location

    def require_exe(self) -> Path:
        loc = self.location()
        if not loc.found or loc.exe is None:
            raise ToolFailure("DRAWIO_UNAVAILABLE", loc.reason or "draw.io Desktop not found")
        return loc.exe

    def drawio_version(self) -> str | None:
        loc = self.location()
        return self.cli.version(loc.exe) if loc.found and loc.exe else None

    # -------------------------------------------------------------------------------------------- common
    def _load(self, root: str, path: str) -> tuple[Path, bytes, Diagram]:
        target = self.ws.resolve(root, path)
        if suffix_of(target) not in READABLE_SUFFIXES:
            raise ToolFailure("FORMAT_NOT_SUPPORTED", f"cannot read '{suffix_of(target)}' as a diagram",
                              details={"readable": sorted(READABLE_SUFFIXES)})
        if not target.is_file():
            raise ToolFailure("NOT_FOUND", f"{root}:{path} does not exist")
        data = target.read_bytes()
        try:
            return target, data, read_diagram_bytes(data)
        except DiagramParseError as exc:
            raise ToolFailure("DIAGRAM_PARSE_ERROR", str(exc)) from exc

    def _policy(self, root: str, diagram: Diagram) -> None:
        if root != "public":
            return
        problems = policy_problems(diagram)
        if problems:
            raise ToolFailure("LEAKAGE_POLICY_VIOLATION",
                              f"{len(problems)} policy violation(s) for the public root (machine paths, private "
                              "addresses, secrets, embedded images or unsafe links)",
                              details={"problems": problems[:20]})

    @staticmethod
    def _page_info(diagram: Diagram) -> list[dict[str, Any]]:
        return [{"index": n, "id": p.id, "name": p.name, **page_summary(p)} for n, p in enumerate(diagram.pages, 1)]

    # -------------------------------------------------------------------------------------------- tools
    def status(self) -> dict[str, Any]:
        loc = self.location()
        roots = {}
        for name, logical in (("public", "<PUBLIC>/docs/diagrams"), ("work", "$VKM_WORK/diagrams")):
            try:
                self.ws.root(name)
                roots[name] = {"available": True, "logical": logical, "reason": None}
            except ToolFailure as exc:
                roots[name] = {"available": False, "logical": logical, "reason": exc.message}
        return {
            "server_version": __version__,
            "drawio": {**loc.public(), "version": self.drawio_version()},
            "roots": roots,
            "capabilities": {"create": True, "read": True, "update": True, "list": True,
                             "export": loc.found, "preview": loc.found, "open": loc.found,
                             "drawio_layout": loc.found},
            "formats": {"public": [".drawio", ".svg", ".png"], "work": [".drawio", ".svg", ".png", ".pdf", ".jpg",
                                                                        ".xml"]},
        }

    def create(self, root: str, path: str, spec: DiagramSpec, overwrite: bool = False) -> dict[str, Any]:
        target = self.ws.resolve(root, path)
        if suffix_of(target) != ".drawio":
            raise ToolFailure("FORMAT_NOT_SUPPORTED", "drawio_create_diagram writes .drawio files; export the "
                                                      "result with drawio_export")
        self.ws.check_format(root, target)
        if target.exists() and not overwrite:
            raise ToolFailure("WOULD_OVERWRITE", f"{root}:{path} exists; pass overwrite=true to replace it")
        diagram = build_diagram(spec)
        engines: dict[str, str] = {}
        for page_spec, page in zip(spec.pages, diagram.pages):
            if page_spec.layout.startswith("drawio:"):
                exe = self.require_exe()
                fixed = {n.id: (n.x, n.y) for n in page_spec.nodes if n.x is not None and n.y is not None}
                self.cli.layout_page(exe, page, page_spec.layout, fixed=fixed)
                engines[page.id] = f"drawio {self.drawio_version() or 'unknown'}:{page_spec.layout[7:]}"
            else:
                engines[page.id] = "explicit" if page_spec.layout == "none" else "grid"
        self._policy(root, diagram)
        data = write_diagram(diagram).encode("utf-8")
        atomic_write(target, data, overwrite=overwrite)
        pages = self._page_info(diagram)
        for p in pages:
            p["layout_engine"] = engines[p["id"]]
        return {"root": root, "path": self.ws.relative(root, target), "logical": self.ws.logical(root, target),
                "sha256": sha256_bytes(data), "bytes": len(data), "pages": pages}

    def read(self, root: str, path: str, include_geometry: bool = True, page: str | None = None,
             max_cells: int = 500) -> dict[str, Any]:
        target, data, diagram = self._load(root, path)
        pages = []
        for n, p in enumerate(diagram.pages, 1):
            if page is not None and page not in (p.id, p.name, str(n)):
                continue
            nodes, edges, layers = [], [], []
            for c in p.cells:
                if c.kind == "vertex":
                    nodes.append(self._node_dict(c, include_geometry))
                elif c.kind == "edge":
                    edges.append(self._edge_dict(c, include_geometry))
                elif c.kind == "layer":
                    layers.append({"id": c.id, "name": value_to_text(c.value, False) or None})
            truncated = len(nodes) + len(edges) > max_cells
            if truncated:
                nodes = nodes[:max_cells]
                edges = edges[: max(0, max_cells - len(nodes))]
            pages.append({"index": n, "id": p.id, "name": p.name, "compressed_in_file": p.compressed_in_file,
                          "page_width": p.model_attrs.get("pageWidth"), "page_height": p.model_attrs.get("pageHeight"),
                          "layers": layers, "nodes": nodes, "edges": edges, "truncated": truncated,
                          **{f"n_{k}": v for k, v in page_summary(p).items()}})
        if page is not None and not pages:
            raise ToolFailure("NOT_FOUND", f"page {page!r} not found")
        return {"root": root, "path": self.ws.relative(root, target), "format": suffix_of(target),
                "sha256": sha256_bytes(data), "bytes": len(data), "n_pages": len(diagram.pages), "pages": pages}

    @staticmethod
    def _node_dict(c: Cell, geometry: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"id": c.id, "label": value_to_text(c.value, is_html(c.style)),
                               "label_html": is_html(c.style), "style": c.style or "", "parent": c.parent}
        if c.wrapper_attrs.get("tooltip"):
            out["tooltip"] = c.wrapper_attrs["tooltip"]
        if c.wrapper_attrs.get("link"):
            out["link"] = c.wrapper_attrs["link"]
        props = {k: v for k, v in c.wrapper_attrs.items() if k not in WRAPPER_META}
        if props:
            out["props"] = props
        if geometry and c.geometry is not None:
            g = c.geometry
            out.update({"x": g.x, "y": g.y, "w": g.width, "h": g.height})
        return out

    @staticmethod
    def _edge_dict(c: Cell, geometry: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"id": c.id, "label": value_to_text(c.value, is_html(c.style)), "style": c.style or "",
                               "source": c.source, "target": c.target, "parent": c.parent}
        if geometry and c.geometry is not None and c.geometry.points:
            out["waypoints"] = [list(p) for p in c.geometry.points]
        return out

    def update(self, root: str, path: str, expected_sha256: str, ops: list[Operation]) -> dict[str, Any]:
        target, data, diagram = self._load(root, path)
        if suffix_of(target) != ".drawio":
            raise ToolFailure("FORMAT_NOT_SUPPORTED", "only .drawio files are edited; re-export images afterwards")
        before = sha256_bytes(data)
        if before != expected_sha256.lower():
            raise ToolFailure("DIAGRAM_CONFLICT", "the file changed since it was read (for example in the draw.io "
                                                  "window); read it again", details={"actual_sha256": before})
        new, applied = apply_ops(diagram, ops)
        self._policy(root, new)
        out = write_diagram(new).encode("utf-8")
        if sha256_bytes(target.read_bytes()) != before:          # re-check right before replacing
            raise ToolFailure("DIAGRAM_CONFLICT", "the file changed during the update; nothing written")
        atomic_write(target, out, overwrite=True)
        after = sha256_bytes(out)
        return {"root": root, "path": self.ws.relative(root, target), "sha256_before": before,
                "sha256_after": after, "changed": before != after, "applied": applied,
                "pages": self._page_info(new)}

    def export(self, root: str, path: str, fmt: str, page: int | None = 1, all_pages: bool = False,
               scale: float | None = None, border: int = 10, transparent: bool = False,
               embed_diagram: bool | None = None, out_root: str | None = None, out_path: str | None = None,
               overwrite: bool = False, embed_fonts: bool = False) -> dict[str, Any]:
        if fmt not in EXPORT_FORMATS:
            raise ToolFailure("FORMAT_NOT_SUPPORTED", f"export format must be one of {EXPORT_FORMATS}")
        if all_pages and fmt != "pdf":
            raise ToolFailure("INVALID_ARGUMENT", "all_pages is available for pdf only")
        src, _data, diagram = self._load(root, path)
        if not all_pages and page is not None and not 1 <= page <= len(diagram.pages):
            raise ToolFailure("INVALID_ARGUMENT", f"page must be 1…{len(diagram.pages)} (1-based)")
        out_root = out_root or root
        if out_path is None:
            suffix = "" if all_pages or page in (None, 1) else f".p{page}"
            rel_dir = Path(self.ws.relative(root, src)).parent
            out_path = (rel_dir / f"{_stem(src)}{suffix}.{fmt}").as_posix()
        target = self.ws.resolve(out_root, out_path)
        if suffix_of(target) != f".{fmt}":
            raise ToolFailure("INVALID_ARGUMENT", f"out_path must end with .{fmt}")
        self.ws.check_format(out_root, target)
        self._policy(out_root, diagram)
        if target.exists() and not overwrite:
            raise ToolFailure("WOULD_OVERWRITE", f"{out_root}:{out_path} exists; pass overwrite=true")
        exe = self.require_exe()
        embed = (fmt in ("png", "svg")) if embed_diagram is None else embed_diagram
        blob, run = self.cli.export_file(exe, src, f".{fmt}", page=None if all_pages else page, all_pages=all_pages,
                                         scale=scale, border=border, transparent=transparent, embed_diagram=embed,
                                         embed_svg_fonts=embed_fonts)
        atomic_write(target, blob, overwrite=overwrite)
        return {"out_root": out_root, "out_path": self.ws.relative(out_root, target),
                "logical": self.ws.logical(out_root, target), "format": fmt, "page": None if all_pages else page,
                "all_pages": all_pages, "embed_diagram": embed, "embed_fonts": embed_fonts and fmt == "svg",
                "sha256": sha256_bytes(blob), "bytes": len(blob),
                "seconds": run.seconds, "drawio_version": self.drawio_version()}

    def preview(self, root: str, path: str, page: int = 1, max_side: int = 1024) -> tuple[bytes, dict[str, Any]]:
        if not 128 <= max_side <= PREVIEW_MAX_SIDE:
            raise ToolFailure("INVALID_ARGUMENT", f"max_side must be 128…{PREVIEW_MAX_SIDE}")
        src, data, diagram = self._load(root, path)
        if not 1 <= page <= len(diagram.pages):
            raise ToolFailure("INVALID_ARGUMENT", f"page must be 1…{len(diagram.pages)} (1-based)")
        exe = self.require_exe()
        # draw.io does not fit --width and --height together into a box: limit the longer side of the content
        box = content_bbox(diagram.pages[page - 1])
        wide = box is None or (box[2] - box[0]) >= (box[3] - box[1])
        size_args = {"width": max_side} if wide else {"height": max_side}
        blob, run = self.cli.export_file(exe, src, ".png", page=page, all_pages=False, scale=None, border=10,
                                         transparent=False, embed_diagram=False, **size_args)
        size = png_size(blob)
        if size is not None and max(size) > max_side * 1.1:     # bbox misjudged (labels, rotation): other side
            size_args = {"height": max_side} if wide else {"width": max_side}
            blob, run = self.cli.export_file(exe, src, ".png", page=page, all_pages=False, scale=None, border=10,
                                             transparent=False, embed_diagram=False, **size_args)
            size = png_size(blob)
        if size is None:
            raise ToolFailure("DRAWIO_EXPORT_FAILED", "preview is not a PNG")
        if len(blob) > PREVIEW_MAX_BYTES:
            raise ToolFailure("DRAWIO_EXPORT_FAILED", "preview is larger than the size limit; lower max_side")
        return blob, {"root": root, "path": self.ws.relative(root, src), "page": page, "px": list(size),
                      "sha256": sha256_bytes(blob), "bytes": len(blob), "source_sha256": sha256_bytes(data),
                      "seconds": run.seconds, "mime_type": "image/png"}

    def open(self, root: str, path: str) -> dict[str, Any]:
        src, data, _diagram = self._load(root, path)
        exe = self.require_exe()
        pid = self.cli.open_gui(exe, src)
        return {"launched": True, "pid": pid, "root": root, "path": self.ws.relative(root, src),
                "sha256": sha256_bytes(data),
                "note": "edits made in the window change the file; read it again before drawio_update_diagram"}

    def list(self, root: str, pattern: str = "**/*.drawio") -> dict[str, Any]:
        files = []
        for p in self.ws.list_files(root, pattern):
            data = p.read_bytes()
            entry: dict[str, Any] = {"path": self.ws.relative(root, p), "format": suffix_of(p), "bytes": len(data),
                                     "sha256": sha256_bytes(data),
                                     "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
                                     .isoformat(timespec="seconds")}
            if suffix_of(p) in (".drawio", ".xml"):
                try:
                    entry["pages"] = len(read_diagram_bytes(data).pages)
                except DiagramParseError:
                    entry["pages"] = None
                    entry["parse_error"] = True
            files.append(entry)
        return {"root": root, "pattern": pattern, "count": len(files), "files": files}
