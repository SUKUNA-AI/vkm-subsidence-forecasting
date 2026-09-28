"""Capability detector: what Autodesk software is installed and what the bridge can do — without starting AutoCAD.

Sources: ``HKLM\\SOFTWARE\\Autodesk\\AutoCAD\\R*\\ACAD-*`` (only the values in :data:`ALLOWED_VALUES`), ``HKCU``
``CurVer``, ProgID → CLSID → server registration in ``HKCR``, file presence and version resources,
``acdbmgd.runtimeconfig.json``, ``dotnet --list-sdks`` and the process list. No COM object is created or
attached here. Licence and serial values are never read: :class:`SafeRegistry` refuses them before the registry is
asked.

Paths never leave the detector: the install directory is reported as ``<ACAD_INSTALL_DIR>``.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Protocol

from vkm_cad import __version__

HKLM, HKCU, HKCR = "HKLM", "HKCU", "HKCR"
AUTOCAD_KEY = r"SOFTWARE\Autodesk\AutoCAD"
ALLOWED_VALUES = frozenset({"ProductName", "Release", "AcadLocation", "LangAbbrev", "LocaleID", "UPIRELEASE",
                            "AeccXVersion", "ProductNameShort", "CurVer", ""})
DENIED_FRAGMENTS = ("serial", "licen", "adlm", "netsupport", "networktype", "activation", "password")
LOCALES = {"409": "en-US", "419": "ru-RU", "407": "de-DE", "40C": "fr-FR", "804": "zh-CN"}
DOTNET_API = ("AcCoreMgd.dll", "AcDbMgd.dll", "AcMgd.dll")
CIVIL_API = ("C3D/AeccDbMgd.dll",)
PROCESS_NAMES = ("acad.exe", "accoreconsole.exe")


class RegistryReader(Protocol):
    def subkeys(self, hive: str, path: str) -> list[str]: ...

    def value(self, hive: str, path: str, name: str) -> str | None: ...


class SafeRegistry:
    """Allow-list in front of the registry: licence/serial values cannot be read even by mistake (CAD-02)."""

    def __init__(self, reader: RegistryReader) -> None:
        self._reader = reader

    def subkeys(self, hive: str, path: str) -> list[str]:
        return self._reader.subkeys(hive, path)

    def value(self, hive: str, path: str, name: str) -> str | None:
        lowered = name.lower()
        if name not in ALLOWED_VALUES or any(f in lowered for f in DENIED_FRAGMENTS):
            raise PermissionError(f"registry value {name!r} is not readable by the detector")
        return self._reader.value(hive, path, name)


class WinRegistry:
    """``winreg`` reader (64-bit view)."""

    def __init__(self) -> None:
        import winreg

        self._winreg = winreg
        self._hives = {HKLM: winreg.HKEY_LOCAL_MACHINE, HKCU: winreg.HKEY_CURRENT_USER,
                       HKCR: winreg.HKEY_CLASSES_ROOT}

    def _open(self, hive: str, path: str):
        w = self._winreg
        return w.OpenKey(self._hives[hive], path, 0, w.KEY_READ | getattr(w, "KEY_WOW64_64KEY", 0))

    def subkeys(self, hive: str, path: str) -> list[str]:
        try:
            with self._open(hive, path) as key:
                out, i = [], 0
                while True:
                    try:
                        out.append(self._winreg.EnumKey(key, i))
                    except OSError:
                        return out
                    i += 1
        except OSError:
            return []

    def value(self, hive: str, path: str, name: str) -> str | None:
        try:
            with self._open(hive, path) as key:
                data, _kind = self._winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return None if data is None else str(data)


class EmptyRegistry:
    def subkeys(self, hive: str, path: str) -> list[str]:
        return []

    def value(self, hive: str, path: str, name: str) -> str | None:
        return None


def win_file_version(path: Path) -> str | None:
    try:
        import win32api  # type: ignore[import-not-found]

        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001 - version resources are best effort
        return None


def run_command(cmd: list[str], timeout: float = 15.0) -> str | None:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                              stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


@dataclass
class DetectEnv:
    """Everything the detector touches, injectable for tests (CAD-01)."""

    registry: RegistryReader
    exists: Callable[[Path], bool] = lambda p: p.exists()
    file_version: Callable[[Path], str | None] = win_file_version
    read_text: Callable[[Path], str | None] = lambda p: p.read_text(encoding="utf-8") if p.is_file() else None
    run: Callable[[list[str]], str | None] = run_command
    program_files: str | None = field(default_factory=lambda: os.environ.get("ProgramFiles"))
    platform: str = sys.platform
    module_version: Callable[[str], str | None] = lambda name: _dist_version(name)

    @classmethod
    def system(cls) -> "DetectEnv":
        if sys.platform != "win32":
            return cls(registry=EmptyRegistry())
        return cls(registry=WinRegistry())


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _server_path(command: str | None) -> Path | None:
    if not command:
        return None
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return Path(command[1:end]) if end > 1 else None
    match = re.match(r"^(.+?\.(?:exe|dll))\b", command, re.IGNORECASE)
    return Path(match.group(1)) if match else Path(command.split(" ")[0])


def _com(reg: SafeRegistry, env: DetectEnv, progid: str, server_key: str) -> dict[str, Any]:
    clsid = reg.value(HKCR, rf"{progid}\CLSID", "")
    info: dict[str, Any] = {"progid": progid, "registered": bool(clsid),
                            "server_kind": "LOCAL_SERVER" if server_key == "LocalServer32" else "INPROC_IN_ACAD",
                            "server_exists": False}
    if clsid:
        server = _server_path(reg.value(HKCR, rf"CLSID\{clsid}\{server_key}", ""))
        info["server_exists"] = bool(server and env.exists(server))
    if server_key == "LocalServer32":
        info["create_starts_process"] = True      # why the bridge only ever attaches (GetActiveObject)
    return info


def _tfm(env: DetectEnv, install: Path) -> str | None:
    text = env.read_text(install / "acdbmgd.runtimeconfig.json")
    if not text:
        return None
    try:
        return json.loads(text).get("runtimeOptions", {}).get("tfm")
    except (ValueError, AttributeError):
        return None


def _product(reg: SafeRegistry, env: DetectEnv, release: str, key: str, current: tuple[str | None, str | None],
             warnings: list[str]) -> dict[str, Any]:
    path = rf"{AUTOCAD_KEY}\{release}\{key}"
    val = {name: reg.value(HKLM, path, name) for name in ("ProductName", "Release", "AcadLocation", "LangAbbrev",
                                                          "LocaleID", "UPIRELEASE", "AeccXVersion",
                                                          "ProductNameShort")}
    civil = bool(val["AeccXVersion"]) or "C3D" in (val["ProductNameShort"] or "")
    install = Path(val["AcadLocation"]) if val["AcadLocation"] else None
    locale_id = (val["LocaleID"] or (key.split(":")[1] if ":" in key else "")).upper()
    year = int(val["UPIRELEASE"]) if (val["UPIRELEASE"] or "").isdigit() else None
    product: dict[str, Any] = {
        "product": "CIVIL3D" if civil else "AUTOCAD", "product_name": val["ProductName"], "year": year,
        "release_key": release, "product_key": key, "version": val["Release"], "lang": val["LangAbbrev"],
        "locale": LOCALES.get(locale_id, locale_id or None),
        "current": current == (release, key),
        "install_dir": "<ACAD_INSTALL_DIR>" if install else None,
        "install_dir_present": bool(install and env.exists(install)),
    }
    if install and env.program_files and not str(install).lower().startswith(env.program_files.lower()):
        if "INSTALL_DIR_NON_DEFAULT" not in warnings:
            warnings.append("INSTALL_DIR_NON_DEFAULT")
    major_minor = release[1:] if release.startswith("R") else release
    if civil:
        aecc = val["AeccXVersion"] or ""
        aecc_ver = f"{aecc[:-1]}.{aecc[-1]}" if aecc.isdigit() and len(aecc) >= 2 else aecc
        product["dotnet_api"] = {"assemblies": {a: env.file_version(install / a) if install else None
                                                for a in CIVIL_API if install and env.exists(install / a)}}
        product["com"] = _com(reg, env, f"AeccXUiLand.AeccApplication.{aecc_ver}", "InprocServer32")
    else:
        exes = {}
        for exe in PROCESS_NAMES:
            present = bool(install and env.exists(install / exe))
            exes[exe] = {"present": present, "file_version": env.file_version(install / exe) if present else None}
        product["executables"] = exes
        product["dotnet_api"] = {
            "target_framework": _tfm(env, install) if install else None,
            "assemblies": {a: env.file_version(install / a) for a in DOTNET_API if install and env.exists(install / a)}}
        product["com"] = _com(reg, env, f"AutoCAD.Application.{major_minor}", "LocalServer32")
    return product


def _dotnet_sdks(env: DetectEnv) -> list[str]:
    out = env.run(["dotnet", "--list-sdks"]) or ""
    return [m.group(1) for line in out.splitlines() if (m := re.match(r"^\s*(\d+\.\d+\.\d+\S*)", line))]


def _running(env: DetectEnv) -> dict[str, int]:
    counts = {name.split(".")[0]: 0 for name in PROCESS_NAMES}
    if env.platform != "win32":
        return counts
    out = env.run(["tasklist", "/FO", "CSV", "/NH"]) or ""
    for row in csv.reader(io.StringIO(out)):
        if row and row[0].lower() in PROCESS_NAMES:
            counts[row[0].lower().split(".")[0]] += 1
    return counts


def detect(env: DetectEnv | None = None) -> dict[str, Any]:
    """Capability report (no side effects; safe to call at every ``cad_status``)."""
    env = env or DetectEnv.system()
    reg = SafeRegistry(env.registry)
    warnings: list[str] = []
    cur_release = reg.value(HKCU, AUTOCAD_KEY, "CurVer")
    cur_key = reg.value(HKCU, rf"{AUTOCAD_KEY}\{cur_release}", "CurVer") if cur_release else None
    products, stubs = [], []
    for release in sorted(reg.subkeys(HKLM, AUTOCAD_KEY)):
        if not re.match(r"^R\d+\.\d+$", release):
            continue
        for key in sorted(reg.subkeys(HKLM, rf"{AUTOCAD_KEY}\{release}")):
            if not re.match(r"^ACAD-[0-9A-F]{4}:[0-9A-F]+$", key, re.IGNORECASE):
                continue
            product = _product(reg, env, release, key, (cur_release, cur_key), warnings)
            if product["product_name"] is None and product["install_dir"] is None:
                stubs.append(f"{release}/{key}")         # add-on registration without a product of its own
            else:
                products.append(product)
    sdks = _dotnet_sdks(env)
    running = _running(env)
    ezdxf_version = env.module_version("ezdxf")
    pywin32 = env.module_version("pywin32")
    autocad = [p for p in products if p["product"] == "AUTOCAD"]
    civil = [p for p in products if p["product"] == "CIVIL3D"]
    com_ok = any(p["com"]["registered"] and p["com"]["server_exists"] for p in autocad)
    broken = any(p["com"]["registered"] and not p["com"]["server_exists"] for p in products)
    if env.platform != "win32" or not autocad:
        read_cap = "UNAVAILABLE:AUTOCAD_NOT_INSTALLED" if env.platform == "win32" else "UNAVAILABLE:NOT_WINDOWS"
    elif not pywin32:
        read_cap = "UNAVAILABLE:PYWIN32_MISSING"
    elif not com_ok:
        read_cap = "UNAVAILABLE:COM_NOT_REGISTERED"
    else:
        read_cap = "AVAILABLE" if running["acad"] else "AVAILABLE_WHEN_USER_STARTS_AUTOCAD"
    scratch_cap = "AVAILABLE" if ezdxf_version else "UNAVAILABLE:EZDXF_MISSING"
    if not products:
        overall = "NOT_INSTALLED"
    elif broken:
        overall = "BROKEN_REGISTRATION"
    elif read_cap.startswith("AVAILABLE") and scratch_cap == "AVAILABLE":
        overall = "AVAILABLE"
    else:
        overall = "PARTIAL"
    return {
        "bridge_version": __version__,
        "host_role": "WORKSTATION",
        "overall": overall,
        "products": products,
        "running": running,
        "toolchain": {"dotnet_sdks": sdks, "net8_sdk": any(int(v.split(".")[0]) >= 8 for v in sdks),
                      "pywin32": pywin32, "ezdxf": ezdxf_version},
        "capabilities": {
            "detect": "AVAILABLE",
            "read_open_documents": read_cap,
            "scratch_ezdxf": scratch_cap,
            "pdf_vector_native_to_dxf": scratch_cap,
            "accoreconsole": "NOT_IN_V0",
            "dotnet_plugin": "NOT_IN_V0",
            "civil3d_api": "AVAILABLE_WHEN_CIVIL3D_RUNNING" if civil and com_ok else "UNAVAILABLE",
        },
        "warnings": warnings,
        "stub_product_keys": stubs,
    }


def progids(report: dict[str, Any]) -> list[str]:
    """ProgIDs to attach to, newest registered AutoCAD first, then the version-independent one."""
    ids = [p["com"]["progid"] for p in sorted(report.get("products", []), key=lambda p: p["release_key"],
                                              reverse=True)
           if p["product"] == "AUTOCAD" and p["com"]["registered"]]
    return [*dict.fromkeys(ids), "AutoCAD.Application"]
