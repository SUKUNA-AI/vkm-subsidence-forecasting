"""Optional FP64 Torch execution of the same fixed scalar graph equations.

No torch import or CUDA allocation at module import. Host queue/lease policy is
injected by the caller; this module has no machine paths or resource manager.
"""
import numpy as np
from scipy.sparse import diags


def _compressed_incidence(B,*,max_dense_bytes,other_dense_entries=0):
    B=B.tocsr(copy=True);B.sum_duplicates();B.eliminate_zeros();coo=B.tocoo();nb=B.shape[0]
    if not np.isin(coo.data,[-1.,1.]).all():raise ValueError('Torch compression requires unit-incidence branch rows')
    plus=coo.data>0;minus=coo.data<0
    if not np.array_equal(np.bincount(coo.row[plus],minlength=nb),np.ones(nb,dtype=int)):
        raise ValueError('each branch needs exactly one positive endpoint')
    if np.any(np.bincount(coo.row[minus],minlength=nb)>1):raise ValueError('at most one negative endpoint')
    top=np.empty(nb,int);bottom=np.full(nb,-1,int)
    top[coo.row[plus]]=coo.col[plus];bottom[coo.row[minus]]=coo.col[minus]
    unique,first,group=np.unique(top,return_index=True,return_inverse=True);other=bottom[first]
    if not np.array_equal(other[group],bottom):raise ValueError('incompatible endpoints for one top DOF')
    # Guard BEFORE constructing either potentially large host dense array.
    estimate=8*(len(unique)*B.shape[1]+len(unique)*nb+other_dense_entries)
    if estimate>max_dense_bytes:raise MemoryError('dense execution budget exceeded before host/device allocation')
    operator=np.zeros((len(unique),B.shape[1]));operator[np.arange(len(unique)),unique]=1
    valid=other>=0;operator[np.flatnonzero(valid),other[valid]]=-1
    gather=np.zeros((len(unique),nb));gather[group,np.arange(nb)]=1
    return operator,gather,group,estimate


def _run(factor,operator,forces,s,ke,r,idx,begin=0,initial=None):
    import torch
    q=torch.zeros(operator.shape[1],device=operator.device,dtype=operator.dtype)
    mem=torch.zeros(ke.numel(),device=operator.device,dtype=operator.dtype)
    if initial is not None:q,mem=[v.clone() for v in initial]
    qs=[];zs=[]
    for step in range(begin,len(forces)):
        rhs=forces[step]+operator.T@(s@(ke*mem));q=torch.cholesky_solve(rhs[:,None],factor).ravel();d=(operator@q)[idx];mem=(mem+r*d)/(1+r);qs.append(q);zs.append(mem)
    return torch.stack(qs),torch.stack(zs)


def run_torch(model,old,forces,dt,*,device='cpu',precision='float64',
              verify_cpu=True,comparison_tolerance=2e-9,absolute_tolerance=1e-8,acceptance_tolerance=1e-8,
              max_dense_bytes=512*1024**2,cuda_lease_verifier=None):
    """Transactional trajectory; no accepted state is changed on any failure.

    CUDA caller must provide a lease verifier before allocation. It is a policy
    hook, not a second lock or a proof of authorization. Compatible topologies
    retain their full branch dimension; no padding or constitutive homogenizing.
    """
    model.validate_state(old);model.energy(old);forces=np.asarray(forces,float)
    if forces.ndim!=2 or len(forces)==0 or forces.shape[1]!=len(old.q) or not np.isfinite(forces).all():raise ValueError('nonempty finite force path required')
    if not np.isfinite(dt) or dt<0 or precision!='float64':raise ValueError('finite nonnegative dt and FP64 required')
    if any(not np.isfinite(x) or x<=0 for x in (comparison_tolerance,absolute_tolerance,acceptance_tolerance,max_dense_bytes)):raise ValueError('positive finite limits required')
    import torch
    dev=torch.device(device)
    if dev.type not in ('cpu','cuda'):raise ValueError('only CPU or CUDA device supported')
    lease=None
    if dev.type=='cuda':
        if cuda_lease_verifier is None:raise RuntimeError('CUDA requires the caller host-queue lease verifier')
        lease=cuda_lease_verifier()
        if not lease:raise RuntimeError('CUDA lease not confirmed')
        if not torch.cuda.is_available():raise RuntimeError('CUDA unavailable')
    m=model.operators();K0,B=m['K0'],m['B'];rr=dt*model.inverse_tau;ke=model.km/(1+rr)
    # Upper allocation estimate includes A, factor, duplicate factor workspace,
    # gather/operator and complete output memories. Vendor overhead is external.
    other_entries=3*len(old.q)**2+forces.size+len(forces)*(len(old.q)+len(old.memory))
    operator,gather,group,estimate=_compressed_incidence(B,max_dense_bytes=max_dense_bytes,other_dense_entries=other_entries)
    A=K0+B.T@diags(ke)@B
    tensor=lambda a:torch.as_tensor(a,device=dev,dtype=torch.float64)
    at=tensor(A.toarray());bt=tensor(operator);s=tensor(gather);r=tensor(rr);ket=tensor(ke)
    idx=torch.as_tensor(group,device=dev);F=tensor(forces)
    factor,info=torch.linalg.cholesky_ex(at)
    if int(info)!=0:raise RuntimeError('graph algorithmic operator is not SPD')
    q,z=_run(factor,bt,F,s,ket,r,idx,initial=(tensor(old.q.copy()),tensor(old.memory.copy())))
    if dev.type=='cuda':torch.cuda.synchronize(dev)
    qs,zs=q.cpu().numpy(),z.cpu().numpy();steps=[];accepted=old
    for f,qi,zi in zip(forces,qs,zs):
        result=model.audit(accepted,qi,zi,f,dt)
        if max(result.relative_force_residual,result.relative_energy_residual,result.relative_memory_residual)>acceptance_tolerance:
            raise RuntimeError('device trajectory failed balance checks; original state unchanged')
        steps.append(result);accepted=result.state
    oracle=None
    if verify_cpu:
        cpu=old;cq=[];cz=[]
        for f in forces:
            cpu=model.advance(cpu,f,dt).state;cq.append(cpu.q);cz.append(cpu.memory)
        aq=np.asarray(cq);az=np.asarray(cz)
        error_q=float(np.linalg.norm(qs-aq)/max(np.finfo(float).tiny,np.linalg.norm(aq)))
        error_z=float(np.linalg.norm(zs-az)/max(np.finfo(float).tiny,np.linalg.norm(az)))
        abs_q=float(np.max(abs(qs-aq)));abs_z=float(np.max(abs(zs-az))) if zs.size else 0.
        if max(error_q,error_z)>comparison_tolerance or max(abs_q,abs_z)>absolute_tolerance:raise RuntimeError('device/CPU comparison failed; original state unchanged')
        oracle=dict(status='CHECKED',q_relative=error_q,memory_relative=error_z,
            q_absolute_m=abs_q,memory_absolute_m=abs_z,relative_tolerance=comparison_tolerance,absolute_tolerance_m=absolute_tolerance)
    record=dict(device=str(dev),device_name=torch.cuda.get_device_name(dev) if dev.type=='cuda' else 'CPU',
        torch=torch.__version__,cuda=torch.version.cuda if dev.type=='cuda' else None,
        precision='float64',deterministic_algorithms=torch.are_deterministic_algorithms_enabled(),
        world_id=model.world_id,physical_signature=model.physical_signature,
        representation_signature=model.representation_signature,branch_count=len(model.km),
        dense_estimate_bytes=estimate,cpu_oracle=oracle or {'status':'NOT_RUN'},lease=lease,
        peak_cuda_allocated_bytes=torch.cuda.max_memory_allocated(dev) if dev.type=='cuda' else 0)
    return tuple(steps),record
