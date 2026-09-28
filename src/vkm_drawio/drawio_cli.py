"""Calls of the draw.io Desktop CLI: version, export (png/svg/pdf/jpg/xml), per-page ``--layout`` and GUI open.

Every export runs into a temporary file with the right extension (draw.io picks the format from it), checks the exit
code and a non-empty result, then moves the result into place atomically. Calls are serialised in this process: the
Electron app is heavy and one export at a time is enough for interactive use. No window is created on Windows
(``CREATE_NO_WINDOW``); ``drawio_open`` is the only call that starts the GUI, detached and without waiting.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from vkm_drawio.errors import ToolFailure
from vkm_drawio.model import drawio_layout_argument
from vkm_drawio.styles import parse_style, set_style_keys
from vkm_drawio.xmlio import Diagram, Page, read_diagram_bytes, write_diagram

EXPORT_TIMEOUT_S = 120
DRAWIO_INTERNAL_TIMEOUT_S = 60
_LOCK = threading.Lock()
_VERSION_CACHE: dict[str, str | None] = {}
EDGE_ROUTING_KEYS = ("edgeStyle", "curved", "exitX", "exitY", "exitDx", "exitDy", "exitPerimeter", "entryX", "entryY",
                     "entryDx", "entryDy", "entryPerimeter")


@dataclass(frozen=True)
class CliRun:
    returncode: int
    seconds: float
    stderr_tail: str


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run_cli(exe: Path, args: list[str], timeout: float = EXPORT_TIMEOUT_S) -> CliRun:
    started = time.perf_counter()
    with _LOCK:
        try:
            proc = subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, creationflags=_flags(),
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise ToolFailure("DRAWIO_TIMEOUT", f"draw.io CLI did not finish in {timeout:.0f} s", retryable=True) \
                from exc
        except OSError as exc:
            raise ToolFailure("DRAWIO_UNAVAILABLE", f"cannot start draw.io: {exc.strerror or exc}") from exc
    tail = "\n".join((proc.stderr or "").strip().splitlines()[-5:])
    return CliRun(proc.returncode, round(time.perf_counter() - started, 3), tail)


def version(exe: Path) -> str | None:
    key = str(exe)
    if key not in _VERSION_CACHE:
        try:
            proc = subprocess.run([key, "--version"], capture_output=True, text=True, timeout=30,
                                  creationflags=_flags(), stdin=subprocess.DEVNULL)
            text = (proc.stdout or "").strip().splitlines()
            _VERSION_CACHE[key] = text[-1].strip() if proc.returncode == 0 and text else None
        except (OSError, subprocess.TimeoutExpired):
            _VERSION_CACHE[key] = None
    return _VERSION_CACHE[key]


def export_file(exe: Path, src: Path, out_suffix: str, *, page: int | None, all_pages: bool, scale: float | None,
                border: int, transparent: bool, embed_diagram: bool, width: int | None = None,
                height: int | None = None, uncompressed: bool = False, layout: str | None = None,
                embed_svg_fonts: bool = False, work_dir: Path | None = None) -> tuple[bytes, CliRun]:
    """Export ``src`` and return the produced bytes (the caller decides where they go)."""
    fmt = out_suffix.lstrip(".").lower()
    fmt = "jpg" if fmt == "jpeg" else ("xml" if fmt == "drawio" else fmt)
    args = ["--export", "--format", fmt, "--disable-update", "--timeout", str(DRAWIO_INTERNAL_TIMEOUT_S)]
    if fmt in ("png", "svg", "pdf", "jpg"):
        args += ["--theme", "light", "--border", str(border)]
    if all_pages:
        args.append("--all-pages")
    elif page is not None:
        args += ["--page-index", str(page)]
    if scale is not None:
        args += ["--scale", f"{scale:g}"]
    if width is not None:
        args += ["--width", str(width)]
    if height is not None:
        args += ["--height", str(height)]
    if transparent and fmt in ("png", "svg"):
        args.append("--transparent")
    if embed_diagram and fmt in ("png", "svg", "pdf"):
        args.append("--embed-diagram")
    if fmt == "svg":
        args += ["--embed-svg-fonts", "true" if embed_svg_fonts else "false"]
    if uncompressed and fmt in ("xml", "svg"):
        args.append("--uncompressed")
    if layout:
        args += ["--layout", layout]
    with tempfile.TemporaryDirectory(prefix="vkm-drawio-", dir=work_dir) as tmp:
        out = Path(tmp) / ("out.drawio" if fmt == "xml" else f"out.{fmt}")
        run = run_cli(exe, [*args, "--output", str(out), str(src)])
        if run.returncode != 0:
            raise ToolFailure("DRAWIO_EXPORT_FAILED", f"draw.io exited with code {run.returncode}",
                              details={"stderr_tail": run.stderr_tail})
        if not out.is_file() or out.stat().st_size == 0:
            raise ToolFailure("DRAWIO_EXPORT_FAILED", "draw.io produced no output (a running draw.io window can "
                                                      "intercept CLI calls; close it and retry)",
                              retryable=True, details={"stderr_tail": run.stderr_tail})
        return out.read_bytes(), run


def layout_page(exe: Path, page: Page, layout: str, *, fixed: dict[str, tuple[float, float]]) -> Page:
    """Run ``--layout`` on a one-page copy and take the resulting geometry back into ``page``.

    Our cells, styles and page settings stay; vertices take the new position and size, edges the new route (points
    and routing style keys). Nodes listed in ``fixed`` keep their explicit coordinates afterwards."""
    single = write_diagram(Diagram(pages=[page]))
    with tempfile.TemporaryDirectory(prefix="vkm-drawio-layout-") as tmp:
        src = Path(tmp) / "in.drawio"
        src.write_text(single, encoding="utf-8", newline="\n")
        data, _run = export_file(exe, src, ".drawio", page=None, all_pages=False, scale=None, border=0,
                                 transparent=False, embed_diagram=False, uncompressed=True,
                                 layout=drawio_layout_argument(layout))
    laid = read_diagram_bytes(data).pages[0]
    new = {c.id: c for c in laid.cells}
    for cell in page.cells:
        other = new.get(cell.id)
        if other is None or other.geometry is None or cell.geometry is None:
            continue
        if cell.vertex:
            g = cell.geometry
            g.x, g.y, g.width, g.height = other.geometry.x, other.geometry.y, other.geometry.width, \
                other.geometry.height
            if cell.id in fixed:
                g.x, g.y = fixed[cell.id]
        elif cell.edge:
            cell.geometry.points = list(other.geometry.points)
            routing = {k: v for k, v in parse_style(other.style)[1].items() if k in EDGE_ROUTING_KEYS}
            if routing:
                cell.style = set_style_keys(cell.style, routing)
    return page


def open_gui(exe: Path, path: Path) -> int:
    """Start the draw.io GUI on ``path`` detached; return the pid without waiting."""
    kwargs: dict[str, object] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                 "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(exe), str(path)], **kwargs)  # noqa: S603 - fixed executable, jailed path
    return proc.pid
