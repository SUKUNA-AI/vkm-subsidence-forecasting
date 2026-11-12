"""Bounded MAPDL GPU solve of an externally assembled coupled tangent.

No constitutive update or quadrature transfer occurs here. The caller owns
trial/commit/revert, the exact geometry representation, and the host GPU lease.
The algebraic mixed solve is not an independent native FE assembly check.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import gzip
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

import numpy as np
from scipy import sparse
from scipy.io import mmwrite
from vkm_ansys import mapdl_batch

_NATIVE_LOCK = threading.Lock()


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def input_deck():
    # Exact APDL Math command sequence of the inherited native bridge.
    return """\
/BATCH
/GOPR
/TITLE,VKM wholemine frozen operator native equilibrium
*SMAT,KS,D,IMPORT,MMF,operator.mtx
*DMAT,BR,D,IMPORT,MMF,rhs.mtx
*DMAT,SX,D,COPY,BR
*DMAT,AX,D,COPY,BR
*LSENGINE,DSP,NATIVE,KS
*LSFACTOR,NATIVE
*LSBAC,NATIVE,BR,SX
*MULT,KS,,SX,,AX
*EXPORT,SX,APDL,XS
*EXPORT,AX,APDL,AS
*CFOPEN,native_solution,txt
*VWRITE,XS(1,1)
(E26.17)
*CFCLOS
*CFOPEN,native_action,txt
*VWRITE,AS(1,1)
(E26.17)
*CFCLOS
*FREE,ALL
FINISH
/EXIT,NOSAVE
"""


@dataclass(frozen=True)
class NativeConfig:
    """Private runtime configuration; never bake machine paths into this module."""
    executable: str
    executable_sha256: str
    lease_owner: str
    world_id: str
    representation_id: str
    state_id: str
    geometry_sha256: str
    timeout_s: float = 300.
    ram_bytes: int = 6 * 2**30
    max_unknowns: int = 120_000
    max_nnz: int = 25_000_000
    residual_relative: float = 2e-9
    symmetry_relative: float = 1e-13
    disk_headroom_bytes: int = 8 * 2**30
    attempt_cap_bytes: int = 64 * 2**30
    matrix_structure: str = 'symmetric'

    def validate(self):
        for name in ('lease_owner', 'world_id', 'representation_id', 'state_id'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f'{name} must be explicit')
        for name in ('executable_sha256', 'geometry_sha256'):
            if not re.fullmatch('[0-9a-f]{64}', getattr(self, name)):
                raise ValueError(f'invalid {name}')
        if not math.isfinite(self.timeout_s) or not 0 < self.timeout_s <= 600:
            raise ValueError('timeout outside bounded 600-second envelope')
        if type(self.ram_bytes) is not int or not 128*2**20 <= self.ram_bytes <= 12*2**30:
            raise ValueError('RAM outside bounded 128MiB..12GiB envelope')
        if not 0 < self.max_unknowns <= 200_000 or not 0 < self.max_nnz <= 40_000_000:
            raise ValueError('invalid matrix envelope')
        if not 0 < self.residual_relative <= 2e-9 or not 0 < self.symmetry_relative <= 1e-13:
            raise ValueError('tolerances may not relax inherited acceptance')
        if self.disk_headroom_bytes < 2**30 or self.attempt_cap_bytes < 2**20:
            raise ValueError('invalid disk headroom or attempt cap')
        if self.matrix_structure not in ('symmetric', 'general'):
            raise ValueError('matrix_structure must explicitly select symmetric or general')


def _vector(value, n, name):
    arr = np.asarray(value)
    if arr.shape != (n,) or np.iscomplexobj(arr) or not np.isfinite(arr).all():
        raise ValueError(f'invalid {name}')
    return np.array(arr, dtype=float, copy=True)


def _matrix(value, shape, name):
    if not sparse.issparse(value) or value.shape != shape or np.iscomplexobj(value.data):
        raise ValueError(f'invalid {name}')
    result = value.astype(float).tocsc(copy=True)
    result.sum_duplicates()
    result.sort_indices()
    if not np.isfinite(result.data).all():
        raise ValueError(f'nonfinite {name}')
    return result


def _inf(value):
    return float(np.max(np.abs(value), initial=0.))


def assemble(K, rhs, fluid, u_previous, config):
    """Return owned arrays; preserve caller tangent, force and all fluid state."""
    config.validate()
    if not sparse.issparse(K) or K.shape[0] != K.shape[1]:
        raise ValueError('K must be square sparse')
    nu = K.shape[0]
    np_ = len(fluid.model.storage)
    if nu < 1 or np_ < 1 or nu + np_ > config.max_unknowns:
        raise ValueError('unknown count outside configured envelope')
    K = _matrix(K, (nu, nu), 'K')
    B = _matrix(fluid.model.B, (nu, np_), 'B')
    L = _matrix(fluid.model.L, (np_, np_), 'L')
    storage = _vector(fluid.model.storage, np_, 'storage')
    if np.any(storage <= 0) or not math.isfinite(fluid.dt) or fluid.dt < 0:
        raise ValueError('invalid storage/time increment')
    rhs = _vector(rhs, nu, 'rhs')
    reference = _vector(fluid.model.reference_displacement_free, nu, 'reference displacement')
    previous = _vector(u_previous, nu, 'previous displacement')
    mass = _vector(fluid.rhs_storage, np_, 'mass RHS')
    H = sparse.diags(storage) + fluid.dt * L
    A = sparse.bmat([[K, -B], [-B.T, -H]], format='csc')
    if A.nnz > config.max_nnz:
        raise ValueError('nnz outside configured envelope')
    force = rhs - K @ reference
    r = np.r_[force, -mass]
    kd = np.maximum(abs(K.diagonal()), np.finfo(float).eps*np.asarray(abs(K).sum(axis=1)).ravel())
    if np.any(kd <= 0):
        raise ValueError('zero mechanical row cannot be scaled')
    pd = H.diagonal() + np.asarray(B.power(2).T @ (1/kd)).ravel()
    if np.any(pd <= 0):
        raise ValueError('nonpositive hydraulic scaling')
    scale = np.r_[1/np.sqrt(kd), 1/np.sqrt(pd)]
    S = sparse.diags(scale)
    Q = (S @ A @ S).tocsc()
    if not np.isfinite(Q.data).all() or not np.isfinite(scale).all():
        raise ValueError('nonfinite scaled system')
    symmetry_error = _inf((Q-Q.T).data)/max(1., _inf(Q.data))
    if config.matrix_structure == 'symmetric' and symmetry_error > config.symmetry_relative:
        raise ValueError('NOT_SUPPORTED: nonsymmetric coupled tangent')
    return dict(K=K, B=B, H=H.tocsc(), rhs=rhs, reference=reference, previous=previous,
                mass=mass, scale=scale, Q=Q, r=r, nu=nu, np=np_, dt=float(fluid.dt),
                relative_asymmetry=symmetry_error)


def readback(work, system, tolerance):
    """Validate independent native action and both unscaled balance equations."""
    n = system['nu'] + system['np']
    z = _vector(np.loadtxt(work/'native_solution.txt', ndmin=1), n, 'native solution')
    action = _vector(np.loadtxt(work/'native_action.txt', ndmin=1), n, 'native action')
    K, B, H = (system[key] for key in ('K', 'B', 'H'))
    x = system['scale'] * z
    du, pressure = x[:system['nu']], x[system['nu']:]
    displacement = system['reference'] + du
    rm = K@displacement - B@pressure - system['rhs']
    rh = B.T@du + H@pressure - system['mass']
    ms = max(1., _inf(K@displacement), _inf(B@pressure), _inf(system['rhs']))
    hs = max(1., _inf(B.T@du), _inf(H@pressure), _inf(system['mass']))
    expected_action = system['Q']@z
    metrics = dict(force_relative=_inf(rm)/ms, mass_relative=_inf(rh)/hs,
                   mass_max_m3=_inf(rh), pressure_dot_mass_j=float(pressure@rh),
                   native_action_relative=_inf(action-expected_action)/max(1., _inf(expected_action)))
    if not all(math.isfinite(v) for v in metrics.values()):
        raise ValueError('nonfinite unscaled balance')
    return displacement, pressure, metrics, all(metrics[k] <= tolerance for k in
        ('force_relative', 'mass_relative', 'native_action_relative'))


def gpu_proof(work):
    text = (work/'jointgpu.DSP').read_text(errors='replace')
    percentages = list(map(float, re.findall(r'percentage of GPU accelerated flops\s*=\s*([0-9.]+)', text)))
    if 'GPU acceleration activated' not in text or not percentages or min(percentages) <= 0:
        raise RuntimeError('Native automatic CPU execution is not accepted as GPU')
    return dict(actual_GPU_factorizations=text.count('GPU acceleration activated'), GPU_flops_percent=percentages)


def _own_processes(folder, executable):
    # Reuse the inherited Windows CIM route; do not require optional psutil.
    name = Path(executable).name
    if os.name != 'nt' or not re.fullmatch(r'[A-Za-z0-9_.-]+\.exe', name, re.I):
        raise RuntimeError('Native lifecycle requires Windows and a valid executable basename')
    script = '''$ErrorActionPreference='Stop';
$rows=@(Get-CimInstance Win32_Process -Filter "Name='$env:VKM_NATIVE_PROCESS_IMAGE'" |
Select-Object ProcessId,ParentProcessId,Name,CommandLine);
ConvertTo-Json -InputObject $rows -Compress'''
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(os.environ, VKM_NATIVE_PROCESS_IMAGE=name))
    if result.returncode:
        raise RuntimeError('Cannot prove native lifecycle through Windows CIM: '+result.stderr[:1000])
    if not result.stdout.strip():
        raise RuntimeError('Native lifecycle CIM response missing')
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise RuntimeError('Native lifecycle CIM response is not an inventory')
    target = str(Path(folder).resolve()).replace('/', '\\').casefold()
    pattern = re.compile(re.escape(target)+r'(?:[\\\s"]|$)')
    own = []
    for row in rows:
        if not row.get('CommandLine'):
            raise RuntimeError('Native process command line inaccessible; retain external lease')
        if pattern.search(row['CommandLine'].replace('/', '\\').casefold()):
            own.append(int(row['ProcessId']))
    return own


def _owned(path, root):
    root = Path(root).absolute()
    path = Path(path).absolute()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('archive path outside owned attempt')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('archive refuses symbolic links/junctions')
        if part == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('resolved archive path escaped owned attempt')
    return path


def restore_matrix(root, entry, destination):
    """Restore exactly one archived logical matrix into a NEW owned path."""
    blob = _owned(Path(root)/entry['cas'], root)
    dest = _owned(destination, root)
    digest = hashlib.sha256()
    count = 0
    with gzip.open(blob, 'rb') as source, dest.open('xb') as target:
        while chunk := source.read(2**20):
            target.write(chunk)
            digest.update(chunk)
            count += len(chunk)
    if digest.hexdigest() != entry['sha256'] or count != entry['bytes']:
        raise ValueError('restored matrix hash/size mismatch; original retained')
    return dest


def archive_matrices(root, out):
    """Lossless per-attempt CAS; only own verified .mtx copies may be removed."""
    root, out = Path(root), Path(out)
    cas = _owned(root/'matrix_cas', root)
    cas.mkdir(exist_ok=True)
    entries = {}
    for source in sorted(out.glob('case/*/*.mtx')):
        source = _owned(source, root)
        digest, size = sha(source), source.stat().st_size
        blob = _owned(cas/f'{digest}.mtx.gz', root)
        if not blob.exists():
            with source.open('rb') as raw, blob.open('xb') as compressed:
                with gzip.GzipFile(filename='', mode='wb', fileobj=compressed, compresslevel=1, mtime=0) as writer:
                    shutil.copyfileobj(raw, writer, length=2**20)
        entry = dict(cas=blob.relative_to(root).as_posix(), sha256=digest, bytes=size,
                     compressed_sha256=sha(blob), compressed_bytes=blob.stat().st_size)
        # A fresh actual file is restored and reread before removing the active copy.
        restored = _owned(cas/f'{digest}.{len(entries)}.restore', root)
        restore_matrix(root, entry, restored)
        if sha(restored) != digest or restored.stat().st_size != size or sha(source) != digest:
            raise ValueError('matrix changed during archival; active source retained')
        # Validate MatrixMarket header of the byte-identical reconstructed stream.
        with restored.open('rb') as stream:
            if not stream.readline().startswith(b'%%MatrixMarket matrix '):
                raise ValueError('restored payload is not MatrixMarket')
        logical = source.relative_to(root).as_posix()
        entries[logical] = entry
        _write(out/f'archive_entry_{len(entries):03d}.json', {logical:entry})
        _owned(source, root).unlink()
        _owned(restored, root).unlink()
    _write(out/'matrix_archive.json', dict(entries=entries,
        rebuild='restore_matrix(attempt_root, entry, new_destination_within_attempt)',
        policy='level1 gzip; streamed restore SHA256+size+header verified before owned active copy removal'))
    return entries


@contextmanager
def _bounded_runtime(config, out):
    """Restore every scoped MAPDL hook even on failure; no installed-file edits."""
    if not _NATIVE_LOCK.acquire(blocking=False):
        raise RuntimeError('Concurrent bridge calls in one interpreter are forbidden')
    original = (mapdl_batch._job_object, mapdl_batch._assign, mapdl_batch.build_argv)
    try:
        if os.environ.get('VKM_DEEP_GPU_OWNER') != config.lease_owner:
            raise RuntimeError('External canonical whole-host GPU lease owner mismatch')
        if os.environ.get('ANSGPU_OVERRIDE'):
            raise RuntimeError('GPU support override forbidden')
        if sha(config.executable) != config.executable_sha256:
            raise RuntimeError('Native executable pin changed')
        import win32job
        def capped():
            job = original[0]()
            if job is None:
                raise RuntimeError('Windows hard memory cap unavailable')
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            info['JobMemoryLimit'] = config.ram_bytes
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            return job
        def assigned(job, proc):
            if not original[1](job, proc):
                mapdl_batch.kill_tree(proc.pid, job)
                raise RuntimeError('Native process could not enter capped job')
            return True
        def argv(exe, spec, work):
            if Path(exe).resolve() != Path(config.executable).resolve() or sha(exe) != config.executable_sha256:
                raise RuntimeError('Detected executable differs from approved pin')
            args = original[2](exe, spec, work) + ['-acc', 'nvidia', '-na', '1']
            _write(out/'actual_command.json', dict(argv=args, executable_sha256=sha(exe)))
            return args
        mapdl_batch._job_object, mapdl_batch._assign, mapdl_batch.build_argv = capped, assigned, argv
        yield
    finally:
        mapdl_batch._job_object, mapdl_batch._assign, mapdl_batch.build_argv = original
        _NATIVE_LOCK.release()


class AnsysCoupledBridge:
    def __init__(self, folder, config: NativeConfig):
        config.validate()
        self.config = config
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=False)
        self.count = 0
        self.records = []
        self.failed = False
        self.closed = False
        _write(self.folder/'config.json', asdict(config))

    def _execute_native(self, out):
        try:
            with _bounded_runtime(self.config, out):
                return mapdl_batch.run_job(out/'case', dict(os.environ, ANSGPU_PRINTDEVICES='1',
                    OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1'))
        finally:
            remaining = _own_processes(out, self.config.executable)
            _write(out/'lifecycle.json', dict(remaining_own_native=remaining))
            if remaining:
                raise RuntimeError('Own native process remains; retain external GPU lease')

    def __call__(self, K, rhs, fluid, u_previous):
        if self.failed or self.closed:
            raise RuntimeError('Bridge is terminal; create a new attempt without overwriting this one')
        self.count += 1
        out = self.folder/f'block_{self.count:03d}'
        ind = out/'case/in'
        ind.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        try:
            system = assemble(K, rhs, fluid, u_previous, self.config)
            # MatrixMarket decimal records, two active copies plus restore scratch.
            expected = 128*(system['Q'].nnz + len(system['r']))*3
            used = sum(p.stat().st_size for p in self.folder.rglob('*') if p.is_file())
            free = shutil.disk_usage(self.folder).free
            if free-expected < self.config.disk_headroom_bytes or used+expected > self.config.attempt_cap_bytes:
                raise RuntimeError('DISK_BUDGET: retain caller checkpoint; matrix write/native launch refused')
            mmwrite(ind/'operator.mtx', system['Q'], precision=17, symmetry='general')
            mmwrite(ind/'rhs.mtx', (system['scale']*system['r'])[:, None], precision=17, symmetry='general')
            (ind/'input.inp').write_text(input_deck(), encoding='ascii')
            mapdl_batch.MapdlSpec(jobname='jointgpu', np=1, parallel='smp',
                ram_mb=min(6144, int(self.config.ram_bytes//2**21)), timeout_s=self.config.timeout_s,
                lock_timeout_s=5, includes=['operator.mtx', 'rhs.mtx'],
                keep=[*mapdl_batch.DEFAULT_KEEP, 'dsp', 'pcs'], label=f'coupled native GPU step {self.count}').dump(ind/'mapdl_spec.json')
            np.savez(out/'binding.npz', reference_displacement=system['reference'],
                previous_displacement=system['previous'], scale=system['scale'])
            _write(out/'request.json', dict(config=asdict(self.config), index=self.count,
                nu=system['nu'], np=system['np'], dt_s=system['dt'], nnz=system['Q'].nnz,
                matrix_structure=self.config.matrix_structure, relative_asymmetry=system['relative_asymmetry'],
                inputs={p.name:sha(p) for p in ind.iterdir()}, binding_sha256=sha(out/'binding.npz'),
                adapter_sha256=sha(__file__), formulation='(K,-B;-Bt,-H), H=storage+dt*L; x=D*y; u=reference+du',
                scientific_scope='Externally assembled tangent; no native geometry or quadrature validation'))
            code = self._execute_native(out)
            _write(out/'exit.json', dict(exit_code=code, seconds=time.monotonic()-started))
            if code:
                raise RuntimeError(f'Native step failed exit {code}; no CPU fallback')
            proof = gpu_proof(out/'case/work')
            u, p, metrics, passed = readback(out/'case/work', system, self.config.residual_relative)
            _write(out/'comparison.json', dict(**metrics, **proof, checks_passed=passed))
            if not passed:
                raise RuntimeError('Native unscaled balance/action check failed; trial not accepted')
            np.savez(out/'solution.npz', displacement_free=u, pressure_increment_pa=p)
            archive_matrices(self.folder, out)
            row = dict(index=self.count, **metrics, **proof, seconds=time.monotonic()-started,
                       files_sha256={v.relative_to(out).as_posix():sha(v) for v in out.rglob('*') if v.is_file()})
            _write(out/'receipt.json', dict(status='ready_for_review', **row))
            self.records.append(row)
            return u, p
        except BaseException as exc:
            self.failed = True
            lifecycle = out/'lifecycle.json'
            # Never rotate matrices while native process ownership is unknown.
            if lifecycle.is_file() and not json.loads(lifecycle.read_text())['remaining_own_native'] and not (out/'matrix_archive.json').exists():
                try:
                    archive_matrices(self.folder, out)
                except Exception as archive_error:
                    _write(out/'archive_failure.json', dict(error=str(archive_error), originals_not_globally_deleted=True))
            _write(out/'failure.json', dict(status='FAILED', error=f'{type(exc).__name__}: {exc}',
                seconds=time.monotonic()-started, accepted_records=len(self.records),
                files_sha256={v.relative_to(out).as_posix():sha(v) for v in out.rglob('*') if v.is_file()}))
            raise

    def close(self, error=None):
        if self.closed:
            return
        _write(self.folder/'execution.json', dict(status='FAILED' if self.failed or error else 'ready_for_review',
            requests=self.count, records=self.records, error=str(error) if error else None,
            adapter_sha256=sha(__file__), CPU_fallback_count=0, state_commit_owned_by_caller=True))
        self.closed = True
