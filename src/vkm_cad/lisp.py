"""AutoLISP / script generation for headless runs (``accoreconsole``).

Every run script has the same frame::

    prelude   run id and directory, helpers (vkm:mark, vkm:fail, vkm:result, vkm:idle, vkm:finish), BEGIN marker,
              FILEDIA=0, CMDDIA=0, SECURELOAD=0 (bridge-owned isolated profile), NETLOAD of the .NET host if needed
    body      bridge-generated steps or the user's script / LISP / .NET command
    epilogue  cancel leftovers (vkm:idle), SAVEAS to the run directory (only when nothing failed), END marker, QUIT —
              never an empty line: at the command prompt it repeats the last command

Markers are written by LISP to ``<run>/markers.txt`` (UTF-8): ``BEGIN``, ``NETLOAD``, ``SAVED``, ``FAIL <msg>``, ``END <run>
OK|FAILED``. No END marker means the script stopped early (a command left waiting for input, an aborted LISP) — the
run failed and its drawing is not promoted. Scripts are UTF-8 with BOM and CRLF (verified: Unicode paths and names).
Command names are global (``_.``/``_`` prefix): the installed AutoCAD is Russian.
"""
from __future__ import annotations

from pathlib import Path

BOM = b"\xef\xbb\xbf"


def lisp_str(value: str) -> str:
    """An AutoLISP string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")
    return f'"{escaped}"'


def lisp_path(path: Path, trailing_slash: bool = False) -> str:
    text = str(path).replace("\\", "/")
    if trailing_slash and not text.endswith("/"):
        text += "/"
    return lisp_str(text)


HELPERS = r"""(defun vkm:mark (s / f) (if (setq f (open (strcat vkm:run-dir "markers.txt") "a" "utf8")) (progn (write-line s f) (close f))) (princ))
(defun vkm:fail (msg / f) (if (setq f (open (strcat vkm:run-dir "FAILED") "a" "utf8")) (progn (write-line msg f) (close f))) (vkm:mark (strcat "FAIL " msg)))
(defun vkm:result (v / f) (if (setq f (open (strcat vkm:run-dir "result.txt") "w" "utf8")) (progn (write-line (vl-prin1-to-string v) f) (close f))) (princ))
(defun vkm:idle () (while (> (logand (getvar "CMDACTIVE") 1) 0) (command)) (princ))
(defun vkm:finish (target / ok) (vkm:idle) (setq ok (not (findfile (strcat vkm:run-dir "FAILED")))) (if (and ok target) (progn (command "_.SAVEAS" "2018" target) (vkm:idle) (if (findfile target) (vkm:mark "SAVED") (progn (setq ok nil) (vkm:fail "SAVEAS did not write the drawing"))))) (vkm:mark (strcat "END " vkm:run-id (if ok " OK" " FAILED"))) (princ))"""


def prelude(run_id: str, run_dir: Path, netload: list[Path] | None = None) -> str:
    lines = [f"(setq vkm:run-id {lisp_str(run_id)})",
             f"(setq vkm:run-dir {lisp_path(run_dir, trailing_slash=True)})",
             HELPERS,
             '(vkm:mark (strcat "BEGIN " vkm:run-id))',
             '(setvar "FILEDIA" 0)',
             '(setvar "CMDDIA" 0)',
             '(setvar "SECURELOAD" 0)',
             '(vkm:mark (strcat "ENV " (getvar "ACADVER") "|" (getvar "DWGNAME") "|" (itoa (getvar "INSUNITS"))))']
    for dll in netload or []:
        lines.append(f'(command "_.NETLOAD" {lisp_path(dll)})')
        lines.append(f'(vkm:mark "NETLOAD {dll.name}")')
    return "\n".join(lines) + "\n"


def epilogue(save_to: Path | None) -> str:
    """No empty lines: an empty line at the command prompt REPEATS the last command (verified: a .NET command ran
    three times). A command the body left waiting for input consumes the epilogue → no END marker → run FAILED."""
    target = lisp_path(save_to) if save_to is not None else "nil"
    return f"(vkm:finish {target})\n_.QUIT _Y\n"


def script(run_id: str, run_dir: Path, body: str, save_to: Path | None, netload: list[Path] | None = None) -> str:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if not body.endswith("\n"):
        body += "\n"
    return prelude(run_id, run_dir, netload) + body + epilogue(save_to)


def encode(text: str) -> bytes:
    """UTF-8 with BOM and CRLF line ends."""
    return BOM + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def load_user_lisp(path: Path, capture_result: bool = True) -> str:
    """Load a LISP file inside ``vl-catch-all-apply``; an error becomes ``vkm:fail``, the value ``result.txt``."""
    ok_branch = "(vkm:result vkm:r)" if capture_result else "(princ)"
    return (f"(setq vkm:r (vl-catch-all-apply 'load (list {lisp_path(path)})))\n"
            f'(if (vl-catch-all-error-p vkm:r) (vkm:fail (strcat "LISP " (vl-catch-all-error-message vkm:r))) '
            f"{ok_branch})\n")


def plot_layout(layout: str, pdf: Path) -> str:
    """-PLOT of a layout with its own page setup (device, canonical media, scale set by the host)."""
    return (f'(command "_.-PLOT" "_N" {lisp_str(layout)} "" "" {lisp_path(pdf)} "_N" "_Y")\n'
            "(vkm:idle)\n"
            f'(if (findfile {lisp_path(pdf)}) (vkm:mark {lisp_str("PLOTTED " + layout)}) '
            f'(vkm:fail {lisp_str("PLOT " + layout)}))\n')


def plot_model(pdf: Path, media: str, style_table: str = "monochrome.ctb", landscape: bool = True) -> str:
    """-PLOT of model space: extents, fit to the canonical media of "DWG To PDF.pc3", centered."""
    orient = "_L" if landscape else "_P"
    return (f'(command "_.-PLOT" "_Y" "Model" "DWG To PDF.pc3" {lisp_str(media)} "_M" {lisp_str(orient)} "_N" "_E" '
            f'"_F" "_C" "_Y" {lisp_str(style_table)} "_Y" "_A" {lisp_path(pdf)} "_N" "_Y")\n'
            "(vkm:idle)\n"
            f'(if (findfile {lisp_path(pdf)}) (vkm:mark "PLOTTED Model") (vkm:fail "PLOT Model"))\n')


def pdf_import(pdf: Path, page: int, insert: tuple[float, float] = (0.0, 0.0), scale: float = 1.0) -> str:
    """-PDFIMPORT of one page as objects (verified headless in Core Console)."""
    x, y = insert
    return (f'(command "_.-PDFIMPORT" "_F" {lisp_path(pdf)} {lisp_str(str(int(page)))} {lisp_str(f"{x:g},{y:g}")} '
            f'{lisp_str(f"{scale:g}")} "0")\n'
            "(vkm:idle)\n"
            f'(vkm:mark {lisp_str(f"PDFIMPORT {int(page)}")})\n')


def dxfout(path: Path, version: str = "2018") -> str:
    return (f'(command "_.DXFOUT" {lisp_path(path)} "_V" {lisp_str(version)} "16")\n'
            "(vkm:idle)\n"
            f'(if (findfile {lisp_path(path)}) (vkm:mark "DXFOUT") (vkm:fail "DXFOUT"))\n')


def extents_marker() -> str:
    return ('(command "_.ZOOM" "_E")\n(vkm:idle)\n'
            '(vkm:mark (strcat "EXTENTS " (vl-prin1-to-string (getvar "EXTMIN")) " " '
            '(vl-prin1-to-string (getvar "EXTMAX"))))\n')
