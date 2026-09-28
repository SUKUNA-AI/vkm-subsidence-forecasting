"""MATLAB detection without starting MATLAB: root, version and build, installed products, executables, licence
feature names known to the installation, the official MATLAB MCP Server binary, and running MATLAB processes.

Nothing here reads licence files or licence values, and no process command lines, owners or window titles.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from vkm_jobs.procs import list_processes
from vkm_jobs.roots import env_value

# official MATLAB MCP Server pinned by this project (engineering-tools plan §3.5; digest of the GitHub release asset,
# Authenticode signer checked at install time)
OFFICIAL_SERVER = {
    "name": "matlab-mcp-server",
    "version": "0.14.0",
    "asset": "matlab-mcp-server-windows-x64.exe",
    "url": "https://github.com/matlab/matlab-mcp-server/releases/download/v0.14.0/matlab-mcp-server-windows-x64.exe",
    "sha256": "6697a8962f148628f11d93b36960235871de5614905d6a27c55341c2b83f4118",
    "bytes": 19502944,
    "published_at": "2026-09-25T15:52:17Z",
    "signer": "The MathWorks, Inc.",
    "license": "MathWorks licence (LICENSE.md of the repository): use only with MathWorks products, one user",
}
MATLAB_IMAGES = ("MATLAB.exe", "matlab.exe", "MATLAB", "matlab")
_XML_FIELD = r"<{0}>([^<]*)</{0}>"


def matlab_root(env: Mapping[str, str] | None = None) -> Path | None:
    """``VKM_MATLAB_ROOT``, else the MATLAB found on PATH (``<root>/bin/matlab[.exe]``)."""
    explicit = env_value(env, "VKM_MATLAB_ROOT")
    if explicit:
        root = Path(explicit)
        return root if (root / "VersionInfo.xml").is_file() or (root / "bin").is_dir() else None
    exe = shutil.which("matlab")
    if exe:
        root = Path(exe).resolve().parent.parent
        if (root / "VersionInfo.xml").is_file():
            return root
    return None


def matlab_executable(root: Path) -> Path | None:
    for name in ("matlab.exe", "matlab"):
        exe = root / "bin" / name
        if exe.is_file():
            return exe
    return None


def version_info(root: Path) -> dict[str, Any]:
    path = root / "VersionInfo.xml"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    out = {}
    for key in ("version", "release", "date"):
        m = re.search(_XML_FIELD.format(key), text)
        if m:
            out[key] = m.group(1).strip()
    return out


def products(root: Path) -> list[dict[str, Any]]:
    """Installed products from ``appdata/products/*.xml`` (name, version, base code), one entry per product."""
    folder = root / "appdata" / "products"
    found: dict[str, dict[str, Any]] = {}
    if not folder.is_dir():
        return []
    for path in sorted(folder.glob("*.xml")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = re.search(_XML_FIELD.format("productName"), text)
        if not name:
            continue
        entry = found.setdefault(name.group(1).strip(), {"name": name.group(1).strip()})
        for tag, key in (("productVersion", "version"), ("productBaseCode", "base_code")):
            m = re.search(_XML_FIELD.format(tag), text)
            if m and key not in entry:
                entry[key] = m.group(1).strip()
    return sorted(found.values(), key=lambda p: p["name"].lower())


def licence_feature_names(root: Path) -> list[str]:
    """Licence feature names listed by the installation itself (product-support tables of the MATLAB tree)."""
    names: set[str] = {"MATLAB"}
    for rel in ("toolbox/matlab/connector2/worker/supportedProducts.json",
                "toolbox/matlab/addons_product_support/matlab/resources/remoteClientSupportedProducts.json"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            names.update(k for k in data if isinstance(k, str) and k not in ("schemaVersion", "full", "partial"))
            for key in ("full",):
                names.update(v for v in data.get(key, []) if isinstance(v, str))
            if isinstance(data.get("partial"), dict):
                names.update(k for k in data["partial"] if isinstance(k, str))
    return sorted(n for n in names if re.match(r"^[A-Za-z][A-Za-z0-9_]{1,63}$", n))


def official_server_path(sim_root: Path | None, env: Mapping[str, str] | None = None) -> Path | None:
    explicit = env_value(env, "VKM_MATLAB_MCP_EXE")
    if explicit:
        return Path(explicit)
    if sim_root is None:
        return None
    return sim_root / "tools" / "matlab-mcp-server" / OFFICIAL_SERVER["version"] / OFFICIAL_SERVER["asset"]


def sha256_cached(path: Path, cache_dir: Path | None) -> str:
    """SHA-256 of a large file, cached by (size, mtime) in ``cache_dir``."""
    stat = path.stat()
    key = f"{stat.st_size}-{stat.st_mtime_ns}"
    cache = cache_dir / "sha256_official_server.json" if cache_dir else None
    if cache is not None and cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key:
                return data["sha256"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "sha256": digest}), encoding="utf-8")
    return digest


def official_server(sim_root: Path | None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    path = official_server_path(sim_root, env)
    out: dict[str, Any] = {"pinned_version": OFFICIAL_SERVER["version"], "pinned_sha256": OFFICIAL_SERVER["sha256"],
                           "installed": bool(path and path.is_file())}
    if path and path.is_file():
        digest = sha256_cached(path, sim_root / "cache" / "matlab" if sim_root else None)
        out.update(sha256=digest, sha256_matches_pin=digest == OFFICIAL_SERVER["sha256"],
                   bytes=path.stat().st_size, location="<VKM_SIM_ROOT>/tools/matlab-mcp-server/"
                   f"{OFFICIAL_SERVER['version']}/{OFFICIAL_SERVER['asset']}"
                   if sim_root and str(path).startswith(str(sim_root)) else "$VKM_MATLAB_MCP_EXE")
    return out


def matlab_processes() -> list[dict[str, Any]]:
    """Running MATLAB and MATLAB-MCP-server processes: pid, parent pid, image name (nothing else)."""
    procs = list_processes()
    names = {n.lower() for n in MATLAB_IMAGES} | {OFFICIAL_SERVER["asset"].lower()}
    return [p for p in procs if p["name"].lower() in names]


def detect(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = matlab_root(env)
    if root is None:
        return {"installed": False, "reason": "MATLAB not found (set VKM_MATLAB_ROOT or put <MATLAB_ROOT>/bin on PATH)"}
    exe = matlab_executable(root)
    prods = products(root)
    return {"installed": exe is not None, "root": "<MATLAB_ROOT>", **version_info(root),
            "executable": "<MATLAB_ROOT>/bin/" + exe.name if exe else None,
            "engine_exe": (root / "bin" / "win64" / "MATLAB.exe").is_file() if os.name == "nt" else None,
            "products_count": len(prods), "products": prods}
