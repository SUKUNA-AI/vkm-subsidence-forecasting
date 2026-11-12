"""Small dry total-Lagrangian TET4 core; E/S constitutive extension is MODEL_CHOICE.

SI, tensile-positive, Mandel [xx,yy,zz,sqrt(2)xy,sqrt(2)yz,sqrt(2)xz].
Material is injected; its tangent need not be symmetric. No hydraulic coupling.
"""
from dataclasses import dataclass, field
import hashlib
import numpy as np

VERSION = 'dry-TL-ES-TET4/1'


def frozen(a, dtype=float):
    a = np.asarray(a, dtype=dtype)
    return np.frombuffer(a.tobytes(), dtype=a.dtype).reshape(a.shape)


def _finite(a, name):
    a = np.asarray(a, dtype=float)
    if not np.isfinite(a).all():
        raise ValueError(name + ' must be finite')
    return a


def _indices(a, name):
    a = np.asarray(a)
    if a.dtype.kind not in 'iu' or a.dtype.kind == 'b':
        raise ValueError(name + ' must have integer dtype')
    return np.asarray(a, dtype=np.int64)


def _signature(*items):
    h = hashlib.sha256(VERSION.encode())
    for a in items:
        if isinstance(a, str):
            h.update(a.encode())
        else:
            a = np.asarray(a)
            h.update(str((a.shape, a.dtype.str)).encode())
            h.update(a.tobytes())
    return h.hexdigest()


_m = np.zeros((6, 3, 3))
_m[:3] = np.array([np.diag(x) for x in np.eye(3)])
for _a, (_i, _j) in enumerate(((0, 1), (1, 2), (0, 2)), 3):
    _m[_a, _i, _j] = _m[_a, _j, _i] = 1 / np.sqrt(2.)
MANDEL = frozen(_m)


def green(F):
    F = _finite(F, 'F')
    if F.shape[-2:] != (3, 3):
        raise ValueError('F must end in (3,3)')
    J = np.linalg.det(F)
    if np.any(J <= 0):
        raise ValueError('positive J required')
    return .5 * (np.swapaxes(F, -1, -2) @ F - np.eye(3)), J


@dataclass(frozen=True, slots=True)
class Response:
    def __deepcopy__(self, memo):
        return self

    F: np.ndarray
    E: np.ndarray
    S: np.ndarray
    P: np.ndarray
    sigma: np.ndarray
    A: np.ndarray
    A_material: np.ndarray
    A_geometric: np.ndarray
    J: np.ndarray
    cofactor: np.ndarray
    material_trial: object

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            if name != 'material_trial':
                object.__setattr__(self, name, frozen(getattr(self, name)))


def evaluate_material(law, old, F, dt):
    """Return stress/tangent densities; no V0 multiplication here.

    The old material state stays in a fixed reference basis throughout Newton.
    A[i,J,k,L] = dP[i,J]/dF[k,L], including geometric stress stiffness.
    """
    if not np.isfinite(dt) or dt < 0:
        raise ValueError('dt must be finite and nonnegative')
    F = _finite(F, 'F')
    E, J = green(F)
    e = np.einsum('aij,...ij->...a', MANDEL, E)
    trial = law.instantaneous(old, e) if dt == 0 else law.trial(old, e, dt)
    S = np.einsum('...a,aij->...ij', trial.stress, MANDEL)
    H = np.einsum('...ik,akj->...aij', F, MANDEL)
    Am = np.einsum('...aij,...ab,...bkl->...ijkl', H, trial.tangent, H)
    Ag = np.einsum('ik,...lj->...ijkl', np.eye(3), S)
    P = F @ S
    sigma = (P @ np.swapaxes(F, -1, -2)) / J[..., None, None]
    cof = J[..., None, None] * np.swapaxes(np.linalg.inv(F), -1, -2)
    return Response(F, E, S, P, sigma, Am + Ag, Am, Ag, J, cof, trial)


@dataclass(frozen=True, slots=True)
class TetMesh:
    def __deepcopy__(self, memo):
        return self

    nodes: np.ndarray
    tets: np.ndarray
    density: np.ndarray
    grad: np.ndarray = field(init=False)
    volumes: np.ndarray = field(init=False)
    dofs: np.ndarray = field(init=False)
    signature: str = field(init=False)

    def __post_init__(self):
        x = _finite(self.nodes, 'nodes')
        t = _indices(self.tets, 'tets')
        if x.ndim != 2 or x.shape[1] != 3 or len(x) < 4:
            raise ValueError('nodes must be (n>=4,3)')
        if t.ndim != 2 or t.shape[1] != 4 or len(t) == 0:
            raise ValueError('tets must be (m>0,4)')
        if np.any(t < 0) or np.any(t >= len(x)):
            raise ValueError('tet index out of range')
        D = np.transpose(x[t[:, 1:]] - x[t[:, :1]], (0, 2, 1))
        det = np.linalg.det(D)
        if not np.isfinite(D).all() or not np.isfinite(det).all() or np.any(det <= 0):
            raise ValueError('reference tetrahedra must have positive orientation')
        grad = np.empty((len(t), 4, 3))
        grad[:, 1:] = np.linalg.inv(D)
        grad[:, 0] = -grad[:, 1:].sum(axis=1)
        if not np.isfinite(grad).all():
            raise ValueError('nonfinite reference shape gradients')
        rho = np.broadcast_to(_finite(self.density, 'density'), (len(t),))
        if np.any(rho <= 0):
            raise ValueError('density must be positive')
        for name, a, dtype in [('nodes', x, float), ('tets', t, np.int64),
                ('density', rho, float), ('grad', grad, float),
                ('volumes', det / 6, float),
                ('dofs', (3 * t[:, :, None] + np.arange(3)).reshape(-1, 12), np.int64)]:
            object.__setattr__(self, name, frozen(a, dtype))
        object.__setattr__(self, 'signature', _signature(x, t, rho))

    def deformation_gradient(self, u):
        u = _finite(u, 'u')
        if u.shape != self.nodes.shape:
            raise ValueError('u shape differs from nodes')
        return np.eye(3) + np.einsum('eai,eaj->eij', u[self.tets], self.grad)


def reference_body_force(mesh, acceleration):
    b = _finite(acceleration, 'acceleration')
    if b.shape != (3,):
        raise ValueError('acceleration must have shape (3,)')
    f = np.zeros_like(mesh.nodes)
    np.add.at(f, mesh.tets, (mesh.density * mesh.volumes / 4)[:, None, None] *
              np.broadcast_to(b, (len(mesh.tets), 4, 3)))
    return f


@dataclass(frozen=True, slots=True)
class FiniteState:
    def __deepcopy__(self, memo):
        return self

    u: np.ndarray
    material: object
    model_signature: str

    def __post_init__(self):
        object.__setattr__(self, 'u', frozen(_finite(self.u, 'state.u')))


@dataclass(frozen=True, slots=True)
class Step:
    def __deepcopy__(self, memo):
        return self

    state: FiniteState
    response: Response
    reactions: np.ndarray
    iterations: int
    residual_relative: float
    residual_norm_N: float

    def __post_init__(self):
        object.__setattr__(self, 'reactions', frozen(self.reactions))


def _check_gauge(x, fixed):
    r = x - x.mean(axis=0)
    rb = np.zeros((len(x), 3, 6))
    rb[:, :, :3] = np.eye(3)
    for j in range(3):
        rb[:, :, 3+j] = np.cross(np.eye(3)[j], r)
    rb = rb.reshape(-1, 6)
    scales = np.linalg.norm(rb, axis=0)
    if np.any(scales == 0) or np.linalg.matrix_rank(rb[fixed] / scales, tol=1e-10) < 6:
        raise ValueError('Dirichlet constraints do not remove six rigid modes')


@dataclass(frozen=True, slots=True)
class TLModel:
    def __deepcopy__(self, memo):
        return self

    mesh: TetMesh
    law: object
    signature: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, 'signature', _signature(self.mesh.signature, self.law.signature))

    def initial_state(self):
        return FiniteState(np.zeros_like(self.mesh.nodes),
                           self.law.initial_state((len(self.mesh.tets),)), self.signature)

    def validate_state(self, old):
        if old.model_signature != self.signature:
            raise ValueError('finite world signature mismatch')
        E, _ = green(self.mesh.deformation_gradient(old.u))
        em = np.einsum('aij,eij->ea', MANDEL, E)
        if old.material.strain.shape != em.shape or not np.allclose(
                old.material.strain, em, rtol=1e-11, atol=1e-13):
            raise ValueError('accepted geometry and material strain disagree')

    def assemble(self, old, u, dt):
        self.validate_state(old)
        response = evaluate_material(self.law, old.material,
                                     self.mesh.deformation_gradient(u), dt)
        g, v = self.mesh.grad, self.mesh.volumes
        fe = np.einsum('e,eij,eaj->eai', v, response.P, g)
        ke = np.einsum('e,eaj,eijkl,ebl->eaibk', v, g, response.A, g).reshape(-1,12,12)
        f = np.zeros_like(self.mesh.nodes)
        np.add.at(f, self.mesh.tets, fe)
        K = np.zeros((f.size, f.size))
        np.add.at(K, (self.mesh.dofs[:, :, None], self.mesh.dofs[:, None, :]), ke)
        return f, K, response

    def solve(self, old, external_force, fixed_dofs, fixed_values, dt,
              rtol=1e-10, max_iterations=40, max_backtracks=20):
        """One accepted step; all failed Newton/material trials leave old unchanged.

        Dead reference nodal loads only. Follower loads require their own tangent.
        Reactions = internal minus external; returned for all dofs (free residuals).
        Dense solve is intentionally limited to small verification meshes.
        """
        self.validate_state(old)
        ext = _finite(external_force, 'external force')
        if ext.shape != self.mesh.nodes.shape:
            raise ValueError('external force shape mismatch')
        fixed = _indices(fixed_dofs, 'fixed dofs')
        vals = _finite(fixed_values, 'fixed values')
        if fixed.ndim != 1 or vals.shape != fixed.shape or len(np.unique(fixed)) != len(fixed):
            raise ValueError('fixed dofs must be unique vector with matching values')
        if np.any(fixed < 0) or np.any(fixed >= ext.size):
            raise ValueError('fixed dof out of range')
        if not np.isfinite(rtol) or rtol <= 0 or max_iterations < 0 or max_backtracks < 0:
            raise ValueError('invalid Newton controls')
        _check_gauge(self.mesh.nodes, fixed)
        free = np.setdiff1d(np.arange(ext.size), fixed)
        u = np.array(old.u, copy=True)
        u.ravel()[fixed] = vals
        scale = None
        for it in range(max_iterations + 1):
            f, K, response = self.assemble(old, u, dt)
            res = (f - ext).ravel()
            norm = float(np.linalg.norm(res[free]))
            if scale is None:
                scale = max(1., float(np.linalg.norm(ext)), float(np.linalg.norm(f)))
            if norm <= rtol * scale:
                state = FiniteState(u, self.law.commit(response.material_trial), self.signature)
                return Step(state, response, res.reshape(u.shape), it, norm / scale, norm)
            if it == max_iterations:
                break
            du = np.linalg.solve(K[np.ix_(free, free)], -res[free])
            alpha = 1.
            for _ in range(max_backtracks + 1):
                candidate = u.copy()
                candidate.ravel()[free] += alpha * du
                try:
                    ft, _, _ = self.assemble(old, candidate, dt)
                    nt = np.linalg.norm((ft-ext).ravel()[free])
                except (ValueError, RuntimeError, np.linalg.LinAlgError):
                    nt = np.inf
                if nt <= (1 - 1e-4 * alpha) * norm:
                    u = candidate
                    break
                alpha *= .5
            else:
                raise RuntimeError('Newton line search failed; accepted state unchanged')
        raise RuntimeError('Newton did not converge; accepted state unchanged')
