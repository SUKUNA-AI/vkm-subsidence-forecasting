"""Read-only discovery of the Ansys installation (plan §4.5 ``ansys_status``).

Nothing here starts a product, checks a licence out or changes a setting. Sources: environment variable names
(``VKM_ANSYS_ROOT``, ``AWP_ROOT<ver>``), files of the installation (``builddate.txt``, ``package.id``, executables,
``DPFBuildDate.txt``, ``optiSLang/build_info.txt``), package metadata of the current interpreter, the process table and
its listening TCP sockets (``psutil`` when importable), and a plain TCP connect to the licence server port (no FlexNet
handshake, no checkout). Reports use logical names only: ``<ANSYS_ROOT>/…``; host names, user names and the licence
string never appear.
"""
from __future__ import annotations

import importlib.metadata as md
import ipaddress
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, Mapping

RELEASES = {"261": "2026 R1", "252": "2025 R2", "251": "2025 R1", "242": "2024 R2", "241": "2024 R1"}
# product -> path relative to the version root (<ANSYS_ROOT> = ...\v261); {v} = version digits
EXECUTABLES: dict[str, str] = {
    "mapdl": "ansys/bin/winx64/ANSYS{v}.exe",
    "mapdl_launcher": "ansys/bin/winx64/MAPDL.exe",
    "lsdyna": "ansys/bin/winx64/LSDYNA{v}.exe",
    "mechanical": "aisol/bin/winx64/AnsysWBU.exe",
    "workbench": "Framework/bin/Win64/RunWB2.exe",
    "optislang": "optiSLang/optislang.com",
    "lmutil": "licensingclient/winx64/lmutil.exe",
}
DIRECTORIES: dict[str, str] = {"dpf": "dpf/bin/winx64", "dpf_python": "dpf/python", "cpython_internal": "commonfiles/CPython"}
PYANSYS = ("ansys-mapdl-core", "ansys-dpf-core", "ansys-mechanical-core", "ansys-workbench-core", "ansys-optislang-core",
           "ansys-tools-common", "ansys-mapdl-reader", "mcp")
# process names of Ansys products (lower case); licence daemons are reported only as running / not running
PRODUCT_PROCESSES = {"ansys261.exe", "ansys.exe", "mapdl.exe", "mapdl261.exe", "ansyswbu.exe", "runwb2.exe",
                     "ansysfww.exe", "ansysfwh.exe", "optislang.exe", "oslpp.exe", "lsdyna261.exe", "lsdyna.exe"}
LICENSE_PROCESSES = {"lmgrd.exe", "ansyslmd.exe", "ansysli_server.exe"}
_UNSET = re.compile(r"^\s*$|\$\{[^}]*\}")


def env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return None if value is None or _UNSET.search(value) else value.strip()


def find_root(env: Mapping[str, str]) -> tuple[Path | None, str | None, str]:
    """(version root, version digits, how it was found). ``VKM_ANSYS_ROOT`` wins, then the newest ``AWP_ROOT<ver>``."""
    explicit = env_value(env, "VKM_ANSYS_ROOT")
    if explicit:
        root = Path(explicit)
        m = re.search(r"v(\d{3})$", root.name, re.IGNORECASE)
        return root, (m.group(1) if m else None), "VKM_ANSYS_ROOT"
    found = sorted(((k[len("AWP_ROOT"):], v) for k, v in env.items() if re.fullmatch(r"AWP_ROOT\d{3}", k)
                    and env_value(env, k)), reverse=True)
    if found:
        ver, value = found[0]
        return Path(value.strip()), ver, f"AWP_ROOT{ver}"
    return None, None, "not found (set VKM_ANSYS_ROOT or AWP_ROOT<ver>)"


def _read_text(path: Path, limit: int = 200_000) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit)
    except OSError:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def build_info(root: Path) -> dict[str, Any]:
    """Unified package name and creation stamp, solver release line, DPF and optiSLang build ids."""
    info: dict[str, Any] = {}
    text = _read_text(root / "builddate.txt") or _read_text(root / "package.id") or ""
    if m := re.search(r"Unified Package Name:\s*(\S+)", text):
        info["package"] = m.group(1)
    if m := re.search(r"Unified Package Created:\s*(\S+)", text):
        info["package_created"] = m.group(1)
    if m := re.search(r"ANS_ADMIN\s+Release\s+(\d{4}\s+R\d)\s+(\d{8})", text):
        info["solver_release"], info["solver_build_date"] = m.group(1), m.group(2)
    if m := re.search(r"(?ms)^optiSLang\s*\n\s*version\s+(\S+)\s*\n\s*revision\s+(\S+)", text):
        info["optislang"] = {"version": m.group(1), "revision": m.group(2)}
    dpf = _read_text(root / "dpf" / "DPFBuildDate.txt") or ""
    if m := re.search(r"Merged\s+(Daily-\S+)", dpf):
        info["dpf_build"] = m.group(1)
    return info


def executables(root: Path, ver: str | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, rel in EXECUTABLES.items():
        rel = rel.format(v=ver or "")
        out[name] = {"path": f"<ANSYS_ROOT>/{rel}", "exists": (root / rel).is_file()}
    for name, rel in DIRECTORIES.items():
        out[name] = {"path": f"<ANSYS_ROOT>/{rel}", "exists": (root / rel).is_dir()}
    return out


def exe_path(root: Path, ver: str | None, product: str) -> Path:
    return root / EXECUTABLES[product].format(v=ver or "")


def package_versions(names: tuple[str, ...] = PYANSYS) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in names:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = None
    return out


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def processes() -> dict[str, Any]:
    """Ansys product processes (name, pid, listening addresses with a loopback flag) and licence daemons (running)."""
    try:
        import psutil
    except ImportError:
        return {"available": False, "reason": "psutil not importable"}
    products: list[dict[str, Any]] = []
    daemons: dict[str, bool] = {name: False for name in sorted(LICENSE_PROCESSES)}
    pids: dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name in PRODUCT_PROCESSES or name.startswith("ans.dpf"):
            pids[proc.info["pid"]] = proc.info["name"]
        elif name in daemons:
            daemons[name] = True
    listening: dict[int, list[dict[str, Any]]] = {}
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.pid in pids and conn.laddr:
                listening.setdefault(conn.pid, []).append({"port": conn.laddr.port,
                                                           "loopback": _is_loopback(conn.laddr.ip)})
    except (psutil.AccessDenied, OSError):
        pass
    for pid, name in sorted(pids.items()):
        products.append({"name": name, "pid": pid, "listening": sorted(listening.get(pid, []),
                                                                       key=lambda d: d["port"])})
    return {"available": True, "products": products, "licence_daemons_running": daemons,
            "non_loopback_listeners": sum(1 for p in products for s in p["listening"] if not s["loopback"])}


def listening_addresses(pid: int) -> list[dict[str, Any]]:
    """Listening TCP sockets of one process tree (``pid`` and its children): port and loopback flag."""
    import psutil
    try:
        root = psutil.Process(pid)
        tree = {root.pid} | {c.pid for c in root.children(recursive=True)}
    except psutil.Error:
        return []
    out = []
    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == psutil.CONN_LISTEN and conn.pid in tree and conn.laddr:
            out.append({"pid": conn.pid, "port": conn.laddr.port, "loopback": _is_loopback(conn.laddr.ip)})
    return sorted(out, key=lambda d: (d["pid"], d["port"]))


def licence_server(env: Mapping[str, str], timeout: float = 1.0) -> dict[str, Any]:
    """``ANSYSLMD_LICENSE_FILE`` entries (``port@host``; files are only counted): reachable by TCP and loopback flags.

    The value itself (host names, ports) is not returned."""
    value = env_value(env, "ANSYSLMD_LICENSE_FILE")
    if not value:
        return {"configured": False}
    entries = []
    for item in re.split(r"[;]", value):
        item = item.strip()
        m = re.fullmatch(r"(\d+)@([\w.\-\[\]:]+)", item)
        if not m:
            entries.append({"kind": "file_or_other"})
            continue
        port, host = int(m.group(1)), m.group(2)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                reachable = True
        except OSError:
            reachable = False
        entries.append({"kind": "port@host", "loopback": _is_loopback(host), "reachable": reachable})
    return {"configured": True, "entries": entries}


def detect(env: Mapping[str, str] | None = None, *, with_processes: bool = True,
           with_licence: bool = True) -> dict[str, Any]:
    """Full discovery report (no product is started)."""
    env = os.environ if env is None else env
    root, ver, how = find_root(env)
    report: dict[str, Any] = {"platform": sys.platform, "root_from": how,
                              "python": sys.version.split()[0], "pyansys": package_versions()}
    if root is None or not root.is_dir():
        report.update({"installed": False, "root": None})
    else:
        report.update({"installed": True, "root": "<ANSYS_ROOT>", "version": ver,
                       "release": RELEASES.get(ver or "", None), "build": build_info(root),
                       "executables": executables(root, ver)})
    if with_processes:
        report["processes"] = processes()
    if with_licence:
        report["licence_server"] = licence_server(env)
    return report
