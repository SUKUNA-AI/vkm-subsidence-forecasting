"""The .NET side of the bridge: compile AutoCAD / Civil 3D plugins without installing an SDK.

AutoCAD 2026 loads ``net8.0`` assemblies. The workstation has only the .NET 6 SDK (it cannot target net8.0 through
MSBuild), but a Roslyn compiler (Visual Studio's ``csc.exe`` or the SDK's ``csc.dll``) compiles C# against any set
of reference assemblies: here the installed .NET 8 runtime (``Microsoft.NETCore.App 8.x``, managed DLLs only) plus
``AcCoreMgd``/``AcDbMgd`` and, for Civil 3D, ``C3D/AeccDbMgd`` and ``ACA/AecBaseMgd``. Nothing is installed; the
compiler, the runtime and the AutoCAD assemblies are discovered (``VKM_CSC`` overrides the compiler).

Plugins are cached by content: ``<jobs root>/_plugins/<name>-<key>/<name>.dll`` where the key hashes the sources,
the compiler identity, the runtime version and the reference files (name, size, mtime).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from vkm_cad.errors import ToolFailure

ACAD_REFERENCES = ("AcCoreMgd.dll", "AcDbMgd.dll")
CIVIL_REFERENCES = ("C3D/AeccDbMgd.dll", "ACA/AecBaseMgd.dll")
HOST_SOURCE = Path(__file__).with_name("plugin") / "VkmCadHost.cs"
COMPILE_TIMEOUT_S = 180


def is_managed(path: Path) -> bool:
    """True for a PE file with a CLI (.NET) header."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(4096)
    except OSError:
        return False
    if data[:2] != b"MZ" or len(data) < 0x40:
        return False
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 + 2 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        return False
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    directory = opt + (96 if magic == 0x10B else 112) + 14 * 8
    if directory + 8 > len(data):
        return False
    rva, size = struct.unpack_from("<II", data, directory)
    return rva != 0 and size != 0


@dataclass
class Toolchain:
    compiler: list[str]                  # command prefix: [csc.exe] or [dotnet, csc.dll]
    compiler_kind: str                   # VS_ROSLYN | SDK_ROSLYN | OVERRIDE
    runtime_dir: Path
    runtime_version: str
    acad_dir: Path
    civil: bool
    references: list[Path] = field(default_factory=list)

    def identity(self) -> dict[str, str]:
        return {"compiler_kind": self.compiler_kind, "compiler": Path(self.compiler[-1]).name,
                "runtime": f"Microsoft.NETCore.App {self.runtime_version}", "target": "net8.0"}


def _run(cmd: list[str], timeout: float = 20.0) -> str | None:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                              stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def target_major(acad_dir: Path) -> int:
    try:
        tfm = json.loads((acad_dir / "acdbmgd.runtimeconfig.json").read_text(encoding="utf-8"))["runtimeOptions"]["tfm"]
        return int(re.match(r"net(\d+)", tfm).group(1))  # type: ignore[union-attr]
    except (OSError, ValueError, KeyError, AttributeError):
        return 8


def find_runtime(major: int, run: Callable[[list[str]], str | None] = _run) -> tuple[Path, str] | None:
    out = run(["dotnet", "--list-runtimes"]) or ""
    best: tuple[tuple[int, ...], Path, str] | None = None
    for line in out.splitlines():
        m = re.match(r"^Microsoft\.NETCore\.App (\d+)\.(\d+)\.(\d+)\S* \[(.+)\]\s*$", line.strip())
        if m and int(m.group(1)) == major:
            version = tuple(int(g) for g in m.groups()[:3])
            path = Path(m.group(4)) / ".".join(str(v) for v in version)
            if best is None or version > best[0]:
                best = (version, path, ".".join(str(v) for v in version))
    return (best[1], best[2]) if best else None


def _env_get(env: dict[str, str], name: str) -> str | None:
    """Case-insensitive lookup (a copy of ``os.environ`` on Windows has upper-case keys)."""
    if name in env:
        return env[name]
    lowered = name.lower()
    return next((v for k, v in env.items() if k.lower() == lowered), None)


def find_compilers(env: dict[str, str] | None = None, run: Callable[[list[str]], str | None] = _run,
                   exists: Callable[[Path], bool] = lambda p: p.is_file()) -> list[tuple[str, list[str]]]:
    env = dict(os.environ if env is None else env)
    found: list[tuple[str, list[str]]] = []
    override = _env_get(env, "VKM_CSC")
    if override and exists(Path(override)):
        found.append(("OVERRIDE", (["dotnet", override] if override.lower().endswith(".dll") else [override])))
    program_files_x86 = _env_get(env, "ProgramFiles(x86)")
    if program_files_x86:
        vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if exists(vswhere):
            for inst in (run([str(vswhere), "-all", "-products", "*", "-property", "installationPath"]) or ""
                         ).splitlines():
                csc = Path(inst.strip()) / "MSBuild" / "Current" / "Bin" / "Roslyn" / "csc.exe"
                if inst.strip() and exists(csc):
                    found.append(("VS_ROSLYN", [str(csc)]))
    for line in (run(["dotnet", "--list-sdks"]) or "").splitlines():
        m = re.match(r"^(\d+\.\d+\.\d+\S*) \[(.+)\]\s*$", line.strip())
        if m:
            csc = Path(m.group(2)) / m.group(1) / "Roslyn" / "bincore" / "csc.dll"
            if exists(csc):
                found.append(("SDK_ROSLYN", ["dotnet", str(csc)]))
    return found


def discover(acad_dir: Path | None, *, civil: bool = True, env: dict[str, str] | None = None,
             run: Callable[[list[str]], str | None] = _run) -> tuple[Toolchain | None, list[str]]:
    """The toolchain, or ``None`` with the reasons (never raises)."""
    reasons: list[str] = []
    if acad_dir is None or not (acad_dir / "AcDbMgd.dll").is_file():
        return None, ["AutoCAD managed API (AcDbMgd.dll) not found"]
    compilers = find_compilers(env, run)
    if not compilers:
        reasons.append("no Roslyn compiler (Visual Studio csc.exe or a .NET SDK csc.dll); set VKM_CSC")
    major = target_major(acad_dir)
    runtime = find_runtime(major, run)
    if runtime is None:
        reasons.append(f"no .NET {major} runtime (Microsoft.NETCore.App {major}.x) for reference assemblies")
    refs = [acad_dir / r for r in ACAD_REFERENCES]
    civil_refs = [acad_dir / r for r in CIVIL_REFERENCES]
    has_civil = civil and all(p.is_file() for p in civil_refs)
    if reasons:
        return None, reasons
    assert runtime is not None
    runtime_refs = sorted(p for p in runtime[0].glob("*.dll") if is_managed(p))
    kind, prefix = compilers[0]
    return Toolchain(prefix, kind, runtime[0], runtime[1], acad_dir, has_civil,
                     runtime_refs + refs + (civil_refs if has_civil else [])), []


@dataclass
class CompileResult:
    ok: bool
    dll: Path
    duration_s: float
    errors: list[str]
    warnings: list[str]
    cached: bool = False


def cache_key(sources: list[tuple[str, bytes]], chain: Toolchain) -> str:
    h = hashlib.sha256()
    for name, data in sources:
        h.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    h.update(json.dumps(chain.identity(), sort_keys=True).encode("utf-8"))
    for ref in chain.references:
        st = ref.stat()
        h.update(f"{ref.name}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))
    return h.hexdigest()[:16]


def compile_library(chain: Toolchain, sources: list[Path], out: Path,
                    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> CompileResult:
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["/target:library", f"/out:{out}", "/nostdlib+", "/langversion:latest", "/optimize+", "/deterministic+",
            "/nologo", "/utf8output", "/nowarn:CS1701,CS1702,CS8632"]
    args += [f"/reference:{p}" for p in chain.references]
    args += [str(s) for s in sources]
    rsp = out.with_suffix(".rsp")
    rsp.write_text("\n".join(f'"{a}"' for a in args) + "\n", encoding="utf-8")
    started = time.monotonic()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = runner([*chain.compiler, "/noconfig", f"@{rsp}"], capture_output=True, timeout=COMPILE_TIMEOUT_S,
                      stdin=subprocess.DEVNULL, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolFailure("DOTNET_UNAVAILABLE", f"the C# compiler did not run: {type(exc).__name__}") from exc
    text = (proc.stdout or b"").decode("utf-8", errors="replace") + (proc.stderr or b"").decode("utf-8",
                                                                                                 errors="replace")
    errors = [_strip_paths(line) for line in text.splitlines() if re.search(r"\berror CS\d+", line)]
    warnings = [_strip_paths(line) for line in text.splitlines() if re.search(r"\bwarning CS\d+", line)]
    (out.parent / "compile.log").write_text(text, encoding="utf-8")
    return CompileResult(proc.returncode == 0 and out.is_file(), out, time.monotonic() - started, errors, warnings)


def _strip_paths(line: str) -> str:
    """``C:\\…\\User.cs(3,5): error CS…`` → ``User.cs(3,5): error CS…`` (no machine paths in results)."""
    return re.sub(r"^.*[\\/]([^\\/()]+\.cs\()", r"\1", line.strip())


def build_cached(chain: Toolchain, cache_root: Path, name: str, sources: list[tuple[str, bytes]]) -> CompileResult:
    """Compile ``sources`` (file name, bytes) into ``<cache_root>/<name>-<key>/<name>.dll`` unless already there."""
    key = cache_key(sources, chain)
    folder = cache_root / f"{name}-{key}"
    dll = folder / f"{name}.dll"
    if dll.is_file() and dll.stat().st_size > 0:
        return CompileResult(True, dll, 0.0, [], [], cached=True)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for file_name, data in sources:
        path = folder / file_name
        path.write_bytes(data)
        paths.append(path)
    result = compile_library(chain, paths, dll)
    if not result.ok:
        dll.unlink(missing_ok=True)
        raise ToolFailure("DOTNET_COMPILE_FAILED", f"{name}: {len(result.errors)} compile error(s)",
                          details={"errors": result.errors[:30], "warnings": result.warnings[:10]})
    return result


def host_source() -> bytes:
    return HOST_SOURCE.read_bytes()


USER_TEMPLATE = """// generated by vkm-cad: user code wrapped in a command (C# 10+, net8.0)
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using CoreApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;
{civil_usings}
[assembly: CommandClass(typeof(VkmUser.UserCommand))]

namespace VkmUser
{{
    public static class UserJob
    {{
        public static object Run(Vkm.Cad.JobContext ctx)
        {{
            var doc = ctx.Doc; var db = ctx.Db; var ed = ctx.Ed; var tr = ctx.Tr;
{civil_locals}
{body}
        }}
    }}

    public static class UserCommand
    {{
        [CommandMethod("VKMUSER", CommandFlags.Modal)]
        public static void Run() {{ Vkm.Cad.JobContext.Execute(UserJob.Run); }}
    }}
}}
"""
CIVIL_USINGS = ("using Autodesk.Civil;\nusing Autodesk.Civil.ApplicationServices;\n"
                "using Autodesk.Civil.DatabaseServices;\nusing Autodesk.Civil.DatabaseServices.Styles;\n")
CIVIL_LOCALS = "            var civil = Autodesk.Civil.ApplicationServices.CivilDocument.GetCivilDocument(db);\n"


def user_source(code: str, *, civil: bool, expression: bool = False) -> bytes:
    """Wrap user C# statements (``return …;`` gives the result) or one expression into the command ``VKMUSER``."""
    body = f"            return (object)({code.strip().rstrip(';')});" if expression else code
    body = "\n".join("            " + line if line.strip() else line for line in body.splitlines()) \
        if not expression else body
    text = USER_TEMPLATE.format(civil_usings=CIVIL_USINGS if civil else "", civil_locals=CIVIL_LOCALS if civil else "",
                                body=body)
    return text.encode("utf-8")
