"""Small assembled u-p systems; tension-positive mechanics and outward flux.

This algebra does not certify inf-sup stability of the caller's FE spaces.
"""
import numpy as np


def up_residual_jacobian(k, coupling, storage, flow, u, p, previous_u,
                         previous_p, dt, mechanical_rhs, hydraulic_rhs):
    """R_u=K u-Qp-f; R_p=Q^T du/dt+S dp/dt+Hp-g.

    Units: displacement m, pressure Pa, mechanics N, fluid equation m3/s.
    Coupling Q: m2; S: m3/Pa; H: m3/(Pa*s). Consistent quadrature assumed.
    """
    if not np.isfinite(dt) or dt <= 0:
        raise ValueError("positive finite dt required")
    k,q,s,h = (np.asarray(a,dtype=float) for a in (k,coupling,storage,flow))
    u,p,u0,p0,f,g = (np.asarray(a,dtype=float) for a in (u,p,previous_u,previous_p,mechanical_rhs,hydraulic_rhs))
    nu,np_ = len(u),len(p)
    if any(a.shape != shape for a,shape in ((k,(nu,nu)),(q,(nu,np_)),(s,(np_,np_)),(h,(np_,np_)),(u,(nu,)),(u0,(nu,)),(f,(nu,)),(p,(np_,)),(p0,(np_,)),(g,(np_,)))):
        raise ValueError("u-p dimensions")
    if not all(np.isfinite(a).all() for a in (k,q,s,h,u,p,u0,p0,f,g)):
        raise ValueError("nonfinite u-p input")
    residual = np.r_[k@u-q@p-f, q.T@(u-u0)/dt+s@(p-p0)/dt+h@p-g]
    jacobian = np.block([[k,-q],[q.T/dt,s/dt+h]])
    return residual,jacobian


def constrain_system(matrix, rhs, prescribed):
    """Exact Dirichlet lifting; returns reduced matrix/rhs and full lift/free.

    Original rows must be retained separately when reporting reactions.
    """
    a,b = np.asarray(matrix,dtype=float),np.asarray(rhs,dtype=float)
    if a.ndim != 2 or a.shape != (len(b),len(b)) or b.ndim != 1 or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("invalid system")
    if any(not isinstance(i,(int,np.integer)) or i < 0 or i >= len(b) or not np.isfinite(v) for i,v in prescribed.items()):
        raise ValueError("invalid prescribed coordinate")
    lift = np.zeros(len(b))
    for i,value in prescribed.items():
        lift[i] = value
    free = np.array([i for i in range(len(b)) if i not in prescribed],dtype=int)
    return a[np.ix_(free,free)],(b-a@lift)[free],lift,free


def work_conjugate_transfer(prolongation, traction, quadrature_area):
    """u_f=P q, coarse generalized force=P^T W t.

    Each row is one displacement component at a quadrature point; the area
    weight is repeated for every vector component. No geometry is inferred.
    """
    p,t,w = (np.asarray(a,dtype=float) for a in (prolongation,traction,quadrature_area))
    if p.ndim != 2 or t.shape != (len(p),) or w.shape != t.shape or np.any(w<=0) or not all(np.isfinite(a).all() for a in (p,t,w)):
        raise ValueError("invalid transfer")
    return p.T@(w*t)


def check_internal_incidence(incidence):
    """Each internal oriented link has one source (+1) and sink (-1)."""
    b = np.asarray(incidence)
    if b.ndim != 2 or not np.isfinite(b).all() or not np.all(np.isin(b,[-1,0,1])):
        raise ValueError("invalid incidence")
    if not np.all(np.sum(b==1,axis=0)==1) or not np.all(np.sum(b==-1,axis=0)==1):
        raise ValueError("internal link must have opposite orientations")
    return True


DEFAULT_SHEAR_ORDER = ("yz", "xz", "xy")
PROJECT_SHEAR_ORDER = ("xy", "yz", "xz")
_SHEAR_INDICES = {"xy": (0, 1), "yz": (1, 2), "xz": (0, 2)}


def _shear_order(value):
    if not isinstance(value, (tuple, list)) or len(value) != 3 or set(value) != set(_SHEAR_INDICES):
        raise ValueError("shear_order must be a permutation of ('xy', 'yz', 'xz')")
    return tuple(value)


def kelvin_vector(tensor, *, shear_order=DEFAULT_SHEAR_ORDER):
    """Tensor to work-conjugate Kelvin/Mandel vector, stress or strain.

    Default keeps the original math convention [xx,yy,zz,yz,xz,xy].
    TensorStages/constitutive binding MUST pass PROJECT_SHEAR_ORDER explicitly.
    Engineering shear strain (2*epsilon) is a distinct representation.
    """
    a=np.asarray(tensor,dtype=float)
    if a.shape!=(3,3) or not np.isfinite(a).all() or not np.allclose(a,a.T,rtol=0,atol=1e-12):
        raise ValueError("finite symmetric tensor required")
    order = _shear_order(shear_order)
    return np.r_[np.diag(a), np.sqrt(2)*np.array([a[_SHEAR_INDICES[key]] for key in order])]


def kelvin_tensor(vector, *, shear_order=DEFAULT_SHEAR_ORDER):
    """Inverse Kelvin/Mandel map with explicitly declared shear ordering."""
    a=np.asarray(vector,dtype=float)
    if a.shape!=(6,) or not np.isfinite(a).all():
        raise ValueError("finite six-component Kelvin vector required")
    order = _shear_order(shear_order)
    tensor=np.diag(a[:3])
    for value,key in zip(a[3:]/np.sqrt(2),order,strict=True):
        i,j=_SHEAR_INDICES[key]
        tensor[i,j]=tensor[j,i]=value
    return tensor


def reorder_kelvin(vector, *, source_order, target_order):
    """Reorder Kelvin/Mandel components; both conventions are mandatory.

    This only permutes normalized shear components, never converts engineering
    Voigt strain or unscaled physical stress components to Kelvin.
    """
    source,target=_shear_order(source_order),_shear_order(target_order)
    a=np.asarray(vector,dtype=float)
    if a.shape!=(6,) or not np.isfinite(a).all():
        raise ValueError("finite six-component Kelvin vector required")
    permutation=[0,1,2]+[3+source.index(key) for key in target]
    return a[permutation].copy()


def engineering_strain_to_kelvin(vector, *, source_order, target_order):
    """gamma_ij=2*epsilon_ij -> sqrt(2)*epsilon_ij, with explicit orders."""
    out=reorder_kelvin(vector,source_order=source_order,target_order=target_order)
    out[3:]/=np.sqrt(2)
    return out


def kelvin_strain_to_engineering(vector, *, source_order, target_order):
    out=reorder_kelvin(vector,source_order=source_order,target_order=target_order)
    out[3:]*=np.sqrt(2)
    return out


def voigt_stress_to_kelvin(vector, *, source_order, target_order):
    """Physical tau_ij -> sqrt(2)*tau_ij; inverse factor to strain conversion."""
    out=reorder_kelvin(vector,source_order=source_order,target_order=target_order)
    out[3:]*=np.sqrt(2)
    return out


def kelvin_stress_to_voigt(vector, *, source_order, target_order):
    out=reorder_kelvin(vector,source_order=source_order,target_order=target_order)
    out[3:]/=np.sqrt(2)
    return out


def matrix_solver_class(matrix):
    """Diagnostic after physical scaling/constraints, never a stability proof."""
    a=np.asarray(matrix,dtype=float)
    if a.ndim!=2 or a.shape[0]!=a.shape[1] or not np.isfinite(a).all():
        raise ValueError("invalid matrix")
    if np.linalg.matrix_rank(a)<len(a):
        return "SINGULAR_CHECK_GAUGES_AND_CONSTRAINTS"
    if not np.allclose(a,a.T,rtol=1e-12,atol=1e-14):
        return "GENERAL_DIRECT_OR_GMRES"
    try:
        np.linalg.cholesky(a)
        return "SPD_CG_PERMITTED"
    except np.linalg.LinAlgError:
        return "SYMMETRIC_INDEFINITE_MINRES_OR_DIRECT"
