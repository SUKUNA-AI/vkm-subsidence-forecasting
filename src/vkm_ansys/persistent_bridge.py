"""Same-process PyMAPDL callback; exact full-operator factor reuse only.

Requires the separately qualified installed PyMAPDL environment. No fallback,
no constitutive state cache, no matrix symmetrization and no runtime installation.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time

import numpy as np
from scipy import sparse
from vkm_ansys import coupled_bridge as base
from vkm_ansys import mapdl_batch
from vkm_ansys.licence_lock import LicenceLock


@dataclass(frozen=True)
class PersistentConfig(base.NativeConfig):
    port: int = 50072
    session_timeout_s: float = 90.
    startup_timeout_s: float = 30.
    workspace_mb: int = 512
    database_mb: int = 64
    verify_upload: bool = True

    def validate(self):
        super().validate()
        if not 1024 < self.port < 65536 or not 0 < self.session_timeout_s <= 40000:
            raise ValueError('invalid local port/session wall limit')
        if not 0 < self.startup_timeout_s <= 60 or self.startup_timeout_s > self.session_timeout_s:
            raise ValueError('invalid startup timeout')
        if not 64 <= self.database_mb <= self.workspace_mb <= self.ram_bytes//2**20:
            raise ValueError('workspace/database exceed hard memory envelope')


def canonical_operator(matrix):
    if not sparse.issparse(matrix) or matrix.shape[0] != matrix.shape[1] or np.iscomplexobj(matrix.data):
        raise ValueError('expected square real sparse operator')
    q = matrix.astype(np.float64).tocsr(copy=True)
    q.sum_duplicates(); q.sort_indices(); q.eliminate_zeros()
    if not np.isfinite(q.data).all():
        raise ValueError('nonfinite operator')
    digest = hashlib.sha256(b'vkm-full-scaled-CSR-f64-v1\0')
    for arr, dtype in ((q.shape, '<i8'), (q.indptr, '<i8'), (q.indices, '<i8'), (q.data, '<f8')):
        value = np.asarray(arr, dtype=dtype)
        digest.update(np.asarray(value.shape, dtype='<i8').tobytes())
        digest.update(value.tobytes())
    return q, digest.hexdigest()


def native_argv(config, work):
    return [config.executable, '-j', 'jointgpu', '-np', '1', '-port', str(config.port),
            '-grpc', '-transport', 'wnua', '-smp', '-acc', 'nvidia', '-na', '1',
            '-m', str(config.workspace_mb), '-db', str(config.database_mb), '-s', 'noread',
            '-dir', str(work), '-b', '-i', 'startup.inp', '-o', 'native.out']


def dependency_receipt():
    if os.name != 'nt':
        raise RuntimeError('qualified native runtime requires Windows')
    from vkm_jobs.procs import _resume_process
    from ansys.mapdl.core.mapdl_grpc import MapdlGrpc
    from ansys.math.core.math import AnsMath
    modules = [base, mapdl_batch, inspect.getmodule(LicenceLock), inspect.getmodule(_resume_process),
               inspect.getmodule(MapdlGrpc), inspect.getmodule(AnsMath)]
    return dict(versions={name:importlib.metadata.version(name) for name in
        ('ansys-mapdl-core','ansys-math-core','ansys-api-mapdl','grpcio','numpy','scipy','pywin32')},
        source_sha256={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in modules},
        adapter_sha256=base.sha(__file__))


def gpu_factor_proof(text):
    percentages = [float(x) for x in re.findall(r'percentage of GPU accelerated flops\s*=\s*([0-9.]+)', text)]
    if 'GPU acceleration activated' not in text or not percentages or min(percentages) <= 0:
        raise RuntimeError('New factorization lacks independent positive GPU proof; no fallback')
    return dict(GPU_flops_percent=percentages, actual_GPU_factorizations=text.count('GPU acceleration activated'))


class PersistentSession:
    """One owned capped MAPDL process and licence, one cached factorization."""
    def __init__(self, folder, config):
        config.validate()
        self.folder, self.config = Path(folder), config
        self.folder.mkdir(parents=True, exist_ok=False)
        self.work = self.folder/'work'; self.work.mkdir()
        self._job = self._proc = self._client = self._math = None
        self._factor = self._native_matrix = self._factor_key = None
        self._factor_gpu = None
        self._timer = None
        self._lock = LicenceLock('ansys', label='persistent exact coupled DSP GPU', session_id=self.folder.name)
        self._mutex = threading.Lock()
        self._io = []
        self.closed = False; self.failed = False; self.timed_out = False
        self.factorizations = 0; self.reuses = 0; self.solves = 0
        base._write(self.folder/'config.json', asdict(config))

    def _kill(self):
        self.timed_out = True
        self.failed = True
        with self._mutex:
            if self._job is not None:
                import win32job
                win32job.TerminateJobObject(self._job, 124)

    @contextmanager
    def guard(self, seconds):
        timer = threading.Timer(seconds, self._kill); timer.daemon = True; timer.start()
        try:
            yield
            if self.timed_out:
                raise TimeoutError('Owned persistent native process exceeded wall limit')
        finally:
            timer.cancel(); timer.join(timeout=2.)

    def start(self):
        if self.closed or self.failed: raise RuntimeError('terminal persistent session')
        if self._client is not None: return
        c = self.config
        if os.name != 'nt': raise RuntimeError('qualified native runtime requires Windows')
        from vkm_jobs.procs import _resume_process
        if os.environ.get('VKM_DEEP_GPU_OWNER') != c.lease_owner or os.environ.get('ANSGPU_OVERRIDE'):
            raise RuntimeError('external lease mismatch or forbidden GPU override')
        if base.sha(c.executable) != c.executable_sha256: raise RuntimeError('executable pin changed')
        deps = dependency_receipt()
        base._write(self.folder/'dependencies.json', deps)
        # Do not connect to or take over any already running server.
        with socket.socket() as check:
            check.bind(('127.0.0.1', c.port))
        if not self._lock.acquire(5.): raise RuntimeError('Ansys licence pool busy')
        started = time.monotonic()
        try:
            import win32job
            self._job = mapdl_batch._job_object()
            if self._job is None: raise RuntimeError('Windows Job Object unavailable')
            info = win32job.QueryInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            info['JobMemoryLimit'] = c.ram_bytes
            win32job.SetInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation, info)
            (self.work/'startup.inp').write_text('FINISH\n', encoding='ascii')
            argv = native_argv(c, self.work)
            base._write(self.folder/'actual_command.json', dict(argv=argv, executable_sha256=base.sha(c.executable),
                transport='wnua', client_host='127.0.0.1', cpu=1, hard_memory_bytes=c.ram_bytes))
            self._io = [(self.folder/name).open('xb') for name in ('stdout.log','stderr.log')]
            self._proc = subprocess.Popen(argv, cwd=self.work, stdin=subprocess.DEVNULL,
                stdout=self._io[0], stderr=self._io[1], creationflags=subprocess.CREATE_NO_WINDOW|0x4,
                env=dict(os.environ, ANSGPU_PRINTDEVICES='1', OMP_NUM_THREADS='1',
                         OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1'))
            if not mapdl_batch._assign(self._job, self._proc):
                self._proc.kill()
                raise RuntimeError('Native process assignment failed before resume')
            self._timer = threading.Timer(c.session_timeout_s, self._kill)
            self._timer.daemon = True; self._timer.start()
            if _resume_process(self._proc.pid) == 0: raise RuntimeError('Could not resume owned native process')
            from ansys.mapdl.core.mapdl_grpc import MapdlGrpc
            from ansys.math.core.math import AnsMath
            with self.guard(c.startup_timeout_s):
                self._client = MapdlGrpc(ip='127.0.0.1', port=c.port, timeout=int(c.startup_timeout_s),
                    cleanup_on_exit=False, remove_temp_dir_on_exit=False, local=True,
                    process=self._proc, run_location=str(self.work), disable_run_at_connect=True,
                    transport_mode='wnua', log_apdl=str(self.folder/'commands.apdl'),
                    set_no_abort=False)
                self._client.run('/VERIFY,OFF')
                self._client.run('/GOPR')
                self._client.run('/SOLU')
                self._client.run('DSPOPTION,,,,,,PERFORMANCE')
                actual_dir = Path(self._client.directory).resolve()
                if actual_dir != self.work.resolve(): raise RuntimeError('Connected native directory mismatch')
                self._math = AnsMath(mapdl=self._client)
            base._write(self.folder/'started.json', dict(pid=self._proc.pid, seconds=time.monotonic()-started,
                directory=str(actual_dir), library_server_version=str(self._client._server_version)))
        except BaseException:
            self.failed = True
            self.close()
            raise

    def solve(self, matrix, rhs, out):
        if self.closed or self.failed: raise RuntimeError('terminal persistent session')
        q, key = canonical_operator(matrix)
        b = base._vector(rhs, q.shape[0], 'scaled RHS')
        out = Path(out)
        self.start()
        start = time.monotonic(); reuse = key == self._factor_key
        try:
            with self.guard(self.config.timeout_s):
                if not reuse:
                    # Discard factors on ANY full-Q change, including H, B, dt, scaling.
                    self._math.free()
                    self._factor = self._native_matrix = self._factor_key = None
                    dsp = self.work/'jointgpu.DSP'
                    previous = dsp.read_text(errors='replace') if dsp.exists() else ''
                    native_out = self.work/'native.out'
                    previous_out = native_out.read_text(errors='replace') if native_out.exists() else ''
                    self._native_matrix = self._math.matrix(q, name='VKM_Q', triu=False)
                    if self.config.verify_upload:
                        _, uploaded_key = canonical_operator(self._native_matrix.asarray())
                        if uploaded_key != key: raise RuntimeError('Native uploaded CSR differs from full operator')
                    self._factor = self._math.factorize(self._native_matrix, algo='DSP', inplace=False)
                else:
                    self.reuses += 1
                native_b = self._math.set_vec(b, name='VKM_RHS')
                # The server RHS readback is independent of the Python request.
                uploaded_b = np.asarray(native_b.asarray()).reshape(-1)
                if not np.array_equal(uploaded_b, b): raise RuntimeError('Native RHS upload changed')
                native_x = self._factor.solve(native_b)
                native_action = self._native_matrix.dot(native_x)
                y = np.asarray(native_x.asarray()).reshape(-1)
                action = np.asarray(native_action.asarray()).reshape(-1)
                for obj in (native_b,native_x,native_action): self._math.free(obj)
                if not reuse:
                    current = dsp.read_text(errors='replace') if dsp.exists() else ''
                    delta = current[len(previous):] if current.startswith(previous) else current
                    proof_source='jointgpu.DSP'
                    if not delta:
                        current_out=native_out.read_text(errors='replace') if native_out.exists() else ''
                        delta=current_out[len(previous_out):] if current_out.startswith(previous_out) else current_out
                        proof_source='native.out DSPOPTION PERFORMANCE'
                    (out/'factorization.DSP').write_text(delta)
                    np.savez(out/'trial_native_readback.npz',solution=y,action=action,rhs_readback=uploaded_b)
                    self._factor_gpu = gpu_factor_proof(delta)
                    self._factor_gpu['source']=proof_source
                    self.factorizations += 1
                    self._factor_key = key
                y = base._vector(y, q.shape[0], 'native solution')
                action = base._vector(action, q.shape[0], 'native action')
                action_error = base._inf(action-q@y)/max(1.,base._inf(q@y))
                residual = base._inf(action-b)/max(1.,base._inf(b))
                if max(action_error,residual) > self.config.residual_relative:
                    raise RuntimeError('Persistent native action/residual failed')
            self.solves += 1
            row = dict(operator_sha256=key, reused_factorization=reuse, factorizations=self.factorizations,
                reuses=self.reuses, solves=self.solves, native_action_relative=action_error,
                scaled_residual=residual, factor_gpu=self._factor_gpu, seconds=time.monotonic()-start)
            np.savez(out/'native_readback.npz', solution=y, action=action, rhs_readback=uploaded_b)
            base._write(out/'native_solve.json', row)
            return y,action,row
        except BaseException:
            self.failed = True
            self.close()
            raise

    def close(self):
        if self.closed: return
        errors=[]; peak=None; shutdown='not_started'
        try:
            if self._client is not None and not self.timed_out:
                try:
                    with self.guard(10.):
                        if self._math is not None:self._math.free()
                        self._client.run('FINISH')
                    # Native Ctrl(EXIT) is defective with VERIFY OFF in this
                    # installed server. Close only our already drained Job;
                    # caller readbacks are durable before this operation.
                    import win32job
                    with self._mutex:
                        if self._job is not None:win32job.TerminateJobObject(self._job,0)
                    self._client._channel.close()
                    shutdown='drained_owned_job_termination'
                except Exception as exc: errors.append(str(exc))
            if self._proc is not None:
                try: self._proc.wait(timeout=10.)
                except subprocess.TimeoutExpired: self._kill(); self._proc.wait(timeout=10.)
            if self._timer: self._timer.cancel(); self._timer.join(timeout=2.)
            if self._job is not None:
                import win32api,win32job
                peak=win32job.QueryInformationJobObject(self._job,win32job.JobObjectExtendedLimitInformation)
                with self._mutex:
                    win32api.CloseHandle(self._job);self._job=None
            for stream in self._io: stream.close()
            remaining=base._own_processes(self.folder,self.config.executable)
            base._write(self.folder/'lifecycle.json',dict(remaining_own_native=remaining,
                native_exit_code=self._proc.returncode if self._proc else None, timed_out=self.timed_out,
                shutdown_method='watchdog' if self.timed_out else shutdown,
                cleanup_errors=errors, peak_job_memory_bytes=peak['PeakJobMemoryUsed'] if peak else None,
                factorizations=self.factorizations, reuses=self.reuses, solves=self.solves))
            if remaining: raise RuntimeError('Owned native remains; retain licence and external GPU lease')
            self._lock.release()
            self.closed=True
        except BaseException:
            self.failed=True
            raise


class PersistentCoupledBridge:
    def __init__(self, folder, config):
        config.validate(); self.folder=Path(folder);self.config=config
        self.folder.mkdir(parents=True,exist_ok=False)
        self.session=PersistentSession(self.folder/'native_session',config)
        self.count=0;self.records=[];self.failed=False;self.closed=False
        self.store=self.folder/'operators';self.store.mkdir()

    def __call__(self,K,rhs,fluid,u_previous):
        if self.closed or self.failed: raise RuntimeError('terminal persistent bridge')
        self.count+=1;out=self.folder/f'block_{self.count:04d}';out.mkdir()
        try:
            s=base.assemble(K,rhs,fluid,u_previous,self.config)
            q,key=canonical_operator(s['Q']);path=self.store/f'{key}.npz'
            expected=q.nnz*40+len(s['r'])*64
            used=sum(p.stat().st_size for p in self.folder.rglob('*') if p.is_file())
            if used+expected>self.config.attempt_cap_bytes or shutil.disk_usage(self.folder).free-expected<self.config.disk_headroom_bytes:
                raise RuntimeError('DISK_BUDGET: retain caller checkpoint')
            if not path.exists(): sparse.save_npz(path,q,compressed=False)
            else:
                _,stored_key=canonical_operator(sparse.load_npz(path))
                if stored_key!=key: raise RuntimeError('Immutable operator store changed')
            np.savez(out/'binding.npz',scaled_rhs=s['scale']*s['r'],scale=s['scale'],
                reference=s['reference'],previous=s['previous'])
            base._write(out/'request.json',dict(config=asdict(self.config),operator_sha256=key,
                operator_npz_sha256=base.sha(path),operator_path=path.relative_to(self.folder).as_posix(),
                binding_sha256=base.sha(out/'binding.npz'),nu=s['nu'],np=s['np'],dt=s['dt'],
                adapter_sha256=base.sha(__file__),base_adapter_sha256=base.sha(base.__file__)))
            y,action,native=self.session.solve(q,s['scale']*s['r'],out)
            x=s['scale']*y;u=s['reference']+x[:s['nu']];p=x[s['nu']:]
            rm=s['K']@u-s['B']@p-s['rhs'];rh=s['B'].T@(u-s['reference'])+s['H']@p-s['mass']
            ms=max(1.,base._inf(s['K']@u),base._inf(s['B']@p),base._inf(s['rhs']))
            hs=max(1.,base._inf(s['B'].T@(u-s['reference'])),base._inf(s['H']@p),base._inf(s['mass']))
            row=dict(index=self.count,force_relative=base._inf(rm)/ms,mass_relative=base._inf(rh)/hs,
                pressure_dot_mass_j=float(p@rh),native=native)
            if not np.isfinite([row['force_relative'],row['mass_relative'],row['pressure_dot_mass_j']]).all() or max(row['force_relative'],row['mass_relative'])>self.config.residual_relative:
                raise RuntimeError('Persistent original force/mass balance failed')
            np.savez(out/'solution.npz',displacement_free=u,pressure_increment_pa=p)
            base._write(out/'receipt.json',dict(status='ready_for_review',**row,
                files_sha256={z.name:base.sha(z) for z in out.iterdir() if z.is_file()}))
            self.records.append(row);return u,p
        except BaseException as exc:
            self.failed=True
            base._write(out/'failure.json',dict(error=f'{type(exc).__name__}: {exc}',accepted_records=len(self.records)))
            self.session.failed=True;self.session.close();raise

    def close(self,error=None):
        if self.closed:return
        self.session.close()
        base._write(self.folder/'execution.json',dict(status='FAILED' if self.failed or error else 'ready_for_review',
            requests=self.count,records=self.records,error=str(error) if error else None,CPU_fallback_count=0,
            factor_reuse_scope='Exact full scaled CSR operator identity only; no state cache'))
        self.closed=True
