"""Common helpers for the OGS quick checks (ladder S1-S8).

Verification toys only: nothing here is a SKRU-1 model, a forecast or an observation.
Runs go to <RUN_ROOT> (outside git); receipts use logical path names only.

Logical names written into receipts (never machine paths):
  <OGS_BUILD>  OpenGeoSys source build (release, MFront ON)
  <OGS_SRC>    OpenGeoSys source tree (benchmarks for S1)
  <RUN_ROOT>   run directory outside the repository
  <REPO>       this repository
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

HOME = Path(os.path.expanduser("~"))
OGS_BUILD = Path(os.environ.get("OQC_OGS_BUILD", HOME / "vkm-tools" / "build" / "release"))
OGS_SRC = Path(os.environ.get("OQC_OGS_SRC", HOME / "vkm-tools" / "ogs"))
RUN_ROOT = Path(os.environ.get("OQC_RUN_ROOT", HOME / "vkm" / "sim" / "ogs"))
INPUTS = Path(__file__).resolve().parent
CHECKS_DIR = INPUTS.parent
REPO = CHECKS_DIR.parents[2]
RECEIPTS = CHECKS_DIR / "receipts"
FIGURES = CHECKS_DIR / "figures"
PRJ_DIR = INPUTS / "prj"
OGS_BIN = OGS_BUILD / "bin" / "ogs"

YEAR = 365.25 * 86400.0  # s (Julian year)
G = 9.81  # m/s2, used with normative unit weights: rho = gamma / G


# ----------------------------------------------------------------------------- paths, hashes
def logical(text: str) -> str:
    """Replace machine paths by logical names (receipts must not contain machine paths)."""
    s = str(text)
    for real, name in (
        (OGS_BUILD, "<OGS_BUILD>"),
        (OGS_SRC, "<OGS_SRC>"),
        (RUN_ROOT, "<RUN_ROOT>"),
        (REPO, "<REPO>"),
        (HOME, "<HOME>"),
    ):
        s = s.replace(str(real), name)
    s = re.sub(r"/mnt/[a-z]/[^\s\"']*", "<HOST_PATH>", s)
    s = re.sub(r"[A-Za-z]:\\\\[^\s\"']*", "<HOST_PATH>", s)
    return s


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- versions
_VERSIONS_CACHE: dict | None = None


def _git_head(repo: Path) -> str | None:
    """Read HEAD commit of a git checkout by plain file reads (no git command)."""
    try:
        head = (repo / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            p = repo / ".git" / ref
            if p.exists():
                return p.read_text().strip()
            packed = repo / ".git" / "packed-refs"
            if packed.exists():
                for line in packed.read_text().splitlines():
                    if line.endswith(ref):
                        return line.split()[0]
            return None
        return head
    except OSError:
        return None


def versions() -> dict:
    global _VERSIONS_CACHE
    if _VERSIONS_CACHE is not None:
        return _VERSIONS_CACHE
    out = subprocess.run([str(OGS_BIN), "--version"], capture_output=True, text=True)
    m = re.search(r"ogs\s+version:\s*(\S+)", out.stdout)
    cm = re.search(r"CMake arguments:\s*(.*)", out.stdout)
    tfel = subprocess.run(
        [str(OGS_BUILD / "_ext" / "TFEL" / "bin" / "tfel-config"), "--version"],
        capture_output=True,
        text=True,
    )
    import meshio  # noqa: PLC0415
    import scipy  # noqa: PLC0415

    _VERSIONS_CACHE = {
        "ogs_version": m.group(1) if m else out.stdout.strip()[:200],
        "ogs_cmake_arguments": cm.group(1).strip() if cm else None,
        "ogs_binary": "<OGS_BUILD>/bin/ogs",
        "ogs_source_commit": _git_head(OGS_SRC),
        "tfel_version": tfel.stdout.strip(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "meshio": meshio.__version__,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
    }
    return _VERSIONS_CACHE


# ----------------------------------------------------------------------------- running OGS
def run_ogs(prj: Path, outdir: Path, log: Path, extra: list[str] | None = None,
            timeout: float | None = None) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [str(OGS_BIN), "-o", str(outdir)] + list(extra or []) + [str(prj)]
    # Shared workstation limits (user instruction 28.09): one OGS run at a time, memory capped per run
    # (systemd user scope, no swap), OMP threads <= 8. OQC_MEMCAP="" disables the cap (not recommended).
    memcap = os.environ.get("OQC_MEMCAP", "12G")
    if memcap:
        cmd = ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={memcap}",
               "-p", "MemorySwapMax=0"] + cmd
    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = str(min(8, int(env.get("OMP_NUM_THREADS", "1") or 1)))
    env["PATH"] = f"{OGS_BUILD / 'bin'}:{env.get('PATH', '')}"  # vtkdiff for '-r' comparisons
    t0 = time.time()
    with open(log, "w") as lf:
        try:
            p = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(prj.parent),
                               env=env, timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = -999
    wall = time.time() - t0
    return {
        "command": logical(" ".join(cmd)),
        "exit_code": rc,
        "wall_time_s": round(wall, 2),
        "log": logical(str(log)),
        "log_sha256": sha256_file(log),
    }


def read_pvd(pvd: Path) -> list[tuple[float, Path]]:
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    root = ET.parse(pvd).getroot()
    items = []
    for ds in root.iter("DataSet"):
        items.append((float(ds.get("timestep")), pvd.parent / ds.get("file")))
    items.sort(key=lambda x: x[0])
    return items


def read_vtu(path: Path):
    import meshio  # noqa: PLC0415

    return meshio.read(str(path))


# ----------------------------------------------------------------------------- meshes, geometry
def write_mesh(path: Path, points: np.ndarray, cell_type: str, conn: np.ndarray,
               material_ids: np.ndarray | None = None) -> str:
    import meshio  # noqa: PLC0415

    pts = np.zeros((points.shape[0], 3))
    pts[:, : points.shape[1]] = points
    cell_data = {}
    if material_ids is not None:
        cell_data["MaterialIDs"] = [np.asarray(material_ids, dtype=np.int32)]
    m = meshio.Mesh(pts, [(cell_type, np.asarray(conn, dtype=np.int64))], cell_data=cell_data)
    path.parent.mkdir(parents=True, exist_ok=True)
    meshio.write(str(path), m, file_format="vtu", binary=False)
    return sha256_file(path)


def write_gml(path: Path, name: str, points: list[tuple[float, float, float]],
              polylines: dict[str, list[int]]) -> str:
    lines = [
        '<?xml version="1.0" encoding="ISO-8859-1"?>',
        '<OpenGeoSysGLI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:ogs="http://www.opengeosys.org">',
        f" <name>{name}</name>",
        " <points>",
    ]
    for i, (x, y, z) in enumerate(points):
        lines.append(f'  <point id="{i}" x="{x!r}" y="{y!r}" z="{z!r}"/>')
    lines.append(" </points>")
    lines.append(" <polylines>")
    for k, (pname, ids) in enumerate(polylines.items()):
        lines.append(f'  <polyline id="{k}" name="{pname}">')
        lines += [f"   <pnt>{i}</pnt>" for i in ids]
        lines.append("  </polyline>")
    lines.append(" </polylines>")
    lines.append("</OpenGeoSysGLI>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return sha256_file(path)


def rect_gml_2d(path: Path, name: str, x0, x1, y0, y1) -> str:
    pts = [(x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)]
    return write_gml(path, name, pts, {"BOTTOM": [0, 1], "RIGHT": [1, 2], "TOP": [2, 3], "LEFT": [3, 0]})


def quad8_rectilinear(xs: np.ndarray, ys: np.ndarray):
    """QUAD8 (serendipity) mesh on a rectilinear grid; returns points, conn, (xc, yc)."""
    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)
    nx, ny = len(xs) - 1, len(ys) - 1
    xh = np.empty(2 * nx + 1)
    xh[0::2] = xs
    xh[1::2] = 0.5 * (xs[:-1] + xs[1:])
    yh = np.empty(2 * ny + 1)
    yh[0::2] = ys
    yh[1::2] = 0.5 * (ys[:-1] + ys[1:])
    nid = -np.ones((2 * nx + 1, 2 * ny + 1), dtype=np.int64)
    pts = []
    for J in range(2 * ny + 1):
        for I in range(2 * nx + 1):
            if I % 2 == 1 and J % 2 == 1:
                continue
            nid[I, J] = len(pts)
            pts.append((xh[I], yh[J]))
    conn = []
    cent = []
    for j in range(ny):
        for i in range(nx):
            I, J = 2 * i, 2 * j
            conn.append([
                nid[I, J], nid[I + 2, J], nid[I + 2, J + 2], nid[I, J + 2],
                nid[I + 1, J], nid[I + 2, J + 1], nid[I + 1, J + 2], nid[I, J + 1],
            ])
            cent.append((0.5 * (xs[i] + xs[i + 1]), 0.5 * (ys[j] + ys[j + 1])))
    return np.array(pts), np.array(conn, dtype=np.int64), np.array(cent)


def graded_segment(a: float, b: float, size_at, min_n: int = 1) -> list[float]:
    """Nodes from a to b (a -> b may be decreasing) with element size ~ size_at(position)."""
    length = abs(b - a)
    sgn = 1.0 if b >= a else -1.0
    pos = [0.0]
    while True:
        h = size_at(a + sgn * pos[-1])
        if pos[-1] + h >= length - 0.35 * h:
            break
        pos.append(pos[-1] + h)
    pos.append(length)
    if len(pos) - 1 < min_n:
        pos = list(np.linspace(0.0, length, min_n + 1))
    # stretch so that the last node lands exactly at the end
    scale = length / pos[-1]
    return [a + sgn * p * scale for p in pos]


# ----------------------------------------------------------------------------- prj building
def xml_param_constant(name: str, value) -> str:
    if isinstance(value, (list, tuple)):
        vals = " ".join(repr(float(v)) for v in value)
        return f"<parameter><name>{name}</name><type>Constant</type><values>{vals}</values></parameter>"
    return f"<parameter><name>{name}</name><type>Constant</type><value>{float(value)!r}</value></parameter>"


def xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_param_function(name: str, expressions: list[str]) -> str:
    ex = "".join(f"<expression>{xml_escape(e)}</expression>" for e in expressions)
    return f"<parameter><name>{name}</name><type>Function</type>{ex}</parameter>"


def xml_bc(geometrical_set: str, geometry: str, bctype: str, component: int, parameter: str) -> str:
    return (
        "<boundary_condition>"
        f"<geometrical_set>{geometrical_set}</geometrical_set><geometry>{geometry}</geometry>"
        f"<type>{bctype}</type><component>{component}</component><parameter>{parameter}</parameter>"
        "</boundary_condition>"
    )


def xml_linear_elastic(mat_id, E: str, nu: str) -> str:
    idattr = f' id="{mat_id}"' if mat_id is not None else ""
    return (f"<constitutive_relation{idattr}><type>LinearElasticIsotropic</type>"
            f"<youngs_modulus>{E}</youngs_modulus><poissons_ratio>{nu}</poissons_ratio>"
            "</constitutive_relation>")


def xml_creep_bgra(mat_id, E: str, nu: str, A: str, n: str, sigma0: str, Q: str) -> str:
    idattr = f' id="{mat_id}"' if mat_id is not None else ""
    return (f"<constitutive_relation{idattr}><type>CreepBGRa</type>"
            f"<youngs_modulus>{E}</youngs_modulus><poissons_ratio>{nu}</poissons_ratio>"
            f"<a>{A}</a><n>{n}</n><sigma0>{sigma0}</sigma0><q>{Q}</q>"
            "<nonlinear_solver><maximum_iterations>200</maximum_iterations>"
            "<residuum_tolerance>1e-6</residuum_tolerance>"
            "<increment_tolerance>1e-9</increment_tolerance></nonlinear_solver>"
            "</constitutive_relation>")


def xml_mfront_pllc(mat_id, E, nu, A, Q, n, sigma0, A2, Q2, D) -> str:
    idattr = f' id="{mat_id}"' if mat_id is not None else ""
    props = {
        "YoungModulus": E, "PoissonRatio": nu, "PowerLawFactor": A, "PowerLawEnergy": Q,
        "PowerLawExponent": n, "ReferenceStress": sigma0, "LinearLawFactor": A2,
        "LinearLawEnergy": Q2, "SaltGrainSize": D,
    }
    mp = "".join(f'<material_property name="{k}" parameter="{v}"/>' for k, v in props.items())
    return (f"<constitutive_relation{idattr}><type>MFront</type><behaviour>PowerLawLinearCreep</behaviour>"
            f"<material_properties>{mp}</material_properties></constitutive_relation>")


def xml_media(densities: dict) -> str:
    """densities: {material_id or '*': rho}"""
    out = ["<media>"]
    for mid, rho in densities.items():
        idattr = f' id="{mid}"' if mid is not None else ""
        out.append(
            f"<medium{idattr}><phases><phase><type>Solid</type><properties><property>"
            f"<name>density</name><type>Constant</type><value>{float(rho)!r}</value>"
            "</property></properties></phase></phases></medium>")
    out.append("</media>")
    return "".join(out)


def xml_fixed_timestepping(t0: float, dts: list[tuple[int, float]]) -> tuple[str, float]:
    t_end = t0 + sum(r * d for r, d in dts)
    pairs = "".join(f"<pair><repeat>{r}</repeat><delta_t>{d!r}</delta_t></pair>" for r, d in dts)
    return (f"<time_stepping><type>FixedTimeStepping</type><t_initial>{t0!r}</t_initial>"
            f"<t_end>{t_end!r}</t_end><timesteps>{pairs}</timesteps></time_stepping>"), t_end


def xml_iter_timestepping(t0: float, t_end: float, dt0: float, dtmin: float, dtmax: float,
                          iters=(1, 3, 5, 8, 15), mults=(4.0, 2.5, 1.5, 1.0, 0.5)) -> str:
    return ("<time_stepping><type>IterationNumberBasedTimeStepping</type>"
            f"<t_initial>{t0!r}</t_initial><t_end>{t_end!r}</t_end><initial_dt>{dt0!r}</initial_dt>"
            f"<minimum_dt>{dtmin!r}</minimum_dt><maximum_dt>{dtmax!r}</maximum_dt>"
            f"<number_iterations>{' '.join(str(i) for i in iters)}</number_iterations>"
            f"<multiplier>{' '.join(repr(float(m)) for m in mults)}</multiplier></time_stepping>")


def xml_output(prefix: str, variables: list[str], pairs: list[tuple[int, int]] | None = None,
               fixed_times: list[float] | None = None) -> str:
    v = "".join(f"<variable>{x}</variable>" for x in variables)
    ts = ""
    if pairs:
        ts = "<timesteps>" + "".join(
            f"<pair><repeat>{r}</repeat><each_steps>{e}</each_steps></pair>" for r, e in pairs) + "</timesteps>"
    ft = ""
    if fixed_times:
        ft = "<fixed_output_times>" + " ".join(repr(float(t)) for t in fixed_times) + "</fixed_output_times>"
    return (f"<output><type>VTK</type><prefix>{prefix}</prefix><data_mode>Ascii</data_mode>"
            f"<compress_output>false</compress_output>{ts}{ft}<variables>{v}</variables>"
            "<suffix>_ts_{:timestep}_t_{:time}</suffix></output>")


def build_prj(*, mesh: str, geometry: str | None, dim: int, integration_order: int,
              constitutive: list[str], body_force: list[float], parameters: list[str],
              densities: dict, pv_order: int, ic_param: str, bcs: list[str],
              time_stepping: str, output: str, initial_stress: str | None = None,
              reference_temperature: str | None = None, deactivated: str = "",
              secondary: tuple[str, ...] = ("sigma", "epsilon"), max_iter: int = 30,
              conv_abstol: float = 1e-10, conv_reltol: float | None = 1e-12,
              use_b_bar: bool = False, extra_top: str = "") -> str:
    bf = " ".join(repr(float(b)) for b in body_force)
    sec = "".join(f'<secondary_variable name="{s}"/>' for s in secondary)
    ist = f"<initial_stress>{initial_stress}</initial_stress>" if initial_stress else ""
    rt = f"<reference_temperature>{reference_temperature}</reference_temperature>" if reference_temperature else ""
    bbar = "<use_b_bar>true</use_b_bar>" if use_b_bar else ""
    geo = f"<geometry>{geometry}</geometry>" if geometry else ""
    rel = f"<reltol>{conv_reltol!r}</reltol>" if conv_reltol is not None else ""
    xml = f"""<?xml version="1.0" encoding="ISO-8859-1"?>
<OpenGeoSysProject>
    <mesh>{mesh}</mesh>
    {geo}
    <search_length_algorithm><type>fixed</type><value>1e-6</value></search_length_algorithm>
    {extra_top}
    <processes>
        <process>
            <name>SD</name>
            <type>SMALL_DEFORMATION</type>
            <integration_order>{integration_order}</integration_order>
            {''.join(constitutive)}
            <specific_body_force>{bf}</specific_body_force>
            {ist}
            {rt}
            {bbar}
            <process_variables><process_variable>displacement</process_variable></process_variables>
            <secondary_variables>{sec}</secondary_variables>
        </process>
    </processes>
    <time_loop>
        <processes>
            <process ref="SD">
                <nonlinear_solver>basic_newton</nonlinear_solver>
                <convergence_criterion><type>DeltaX</type><norm_type>NORM2</norm_type><abstol>{conv_abstol!r}</abstol>{rel}</convergence_criterion>
                <time_discretization><type>BackwardEuler</type></time_discretization>
                {time_stepping}
            </process>
        </processes>
        {output}
    </time_loop>
    {xml_media(densities)}
    <parameters>
        {''.join(parameters)}
    </parameters>
    <process_variables>
        <process_variable>
            <name>displacement</name>
            <components>{dim}</components>
            <order>{pv_order}</order>
            <initial_condition>{ic_param}</initial_condition>
            {deactivated}
            <boundary_conditions>{''.join(bcs)}</boundary_conditions>
        </process_variable>
    </process_variables>
    <nonlinear_solvers>
        <nonlinear_solver><name>basic_newton</name><type>Newton</type><max_iter>{max_iter}</max_iter>
        <linear_solver>general_linear_solver</linear_solver></nonlinear_solver>
    </nonlinear_solvers>
    <linear_solvers>
        <linear_solver><name>general_linear_solver</name>
        <eigen><solver_type>SparseLU</solver_type><scaling>true</scaling></eigen></linear_solver>
    </linear_solvers>
</OpenGeoSysProject>
"""
    # pretty-ish: one tag per line for readability of committed inputs
    xml = re.sub(r"><(?!/?(expression|value|values|name|type|pnt)\b)", ">\n<", xml)
    return xml


# ----------------------------------------------------------------------------- receipts
def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, indent=2, ensure_ascii=False, default=_json_default)
    txt = logical(txt)
    path.write_text(txt + "\n", encoding="utf-8")


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return logical(str(o))
    raise TypeError(type(o))


def receipt_base(step: str, title: str) -> dict:
    return {
        "step": step,
        "title": title,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "status_note": "MODEL result of a verification toy; not SKRU-1, not a forecast, not an observation, "
                       "not validated against field data.",
        "versions": versions(),
    }


def copy_prj_to_repo(prj_path: Path, sub: str) -> Path:
    dst = PRJ_DIR / sub / prj_path.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(prj_path, dst)
    return dst


def rel_err(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    den = np.maximum(np.abs(b), 1e-300)
    return np.abs(a - b) / den


def fmt(x, n=4):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "nan"
    return f"{x:.{n}g}"
