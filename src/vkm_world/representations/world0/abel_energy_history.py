"""Exact Abel full-spectrum energy of a fixed reference material carrier.

All accepted states, including dt=0 events, are retained. Supports may only
decrease at dt=0. Initial nonzero stress carries elastic energy; its loading
work is not reconstructed. No mesh, solver, mine or filesystem dependency.
"""
import numpy as np

def _project(a,branch):
    mean=a[...,:3].mean(axis=-1)
    if branch==0:
        out=np.zeros_like(a);out[...,:3]=mean[...,None];return out
    out=a.copy();out[...,:3]-=mean[...,None];return out

def weighted_history_energy(times,stress,strain,support,volume_weights,*,K,G,
        alpha_bulk,delta_bulk,alpha_dev,delta_dev):
    """Return every phase's exact-law energy and actual hybrid work diagnostic.

    stress/strain: [N,P,6] Mandel, support:[N,P], volume_weights:[P] m3.
    Each positive interval uses its endpoint stress. time[0]=0, zero-duration
    transitions retain hereditary memory. Energies are J on the active carrier.
    Cost O(E*N²*P + N³), E=number of support epochs; memory O(N*P+E*N²).
    """
    t=np.asarray(times,float);s=np.asarray(stress,float);eps=np.asarray(strain,float)
    sup=np.asarray(support,float);v=np.asarray(volume_weights,float)
    if t.ndim!=1 or len(t)<1 or t[0]!=0 or np.any(np.diff(t)<0):raise ValueError('time order/origin')
    if s.ndim!=3 or s.shape[-1]!=6 or eps.shape!=s.shape or s.shape[0]!=len(t):raise ValueError('tensor history shape')
    N,P,_=s.shape
    if sup.shape!=(N,P) or v.shape!=(P,):raise ValueError('carrier shape')
    if not all(np.isfinite(x).all() for x in (t,s,eps,sup,v)):raise ValueError('nonfinite input')
    if np.any(v<=0) or np.any((sup<0)|(sup>1)) or np.any(np.diff(sup,axis=0)>1e-14):raise ValueError('invalid/remade host support')
    if not np.isfinite([K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev]).all() or min(K,G)<=0 or not all(0<a<1 for a in (alpha_bulk,alpha_dev)) or min(delta_bulk,delta_dev)<0:raise ValueError('invalid Abel parameters')
    dt=np.r_[0,np.diff(t)]
    if any(dt[i]>0 and not np.array_equal(sup[i],sup[i-1]) for i in range(1,N)):
        raise ValueError('support change must be a separate dt=0 event')
    jump_time=np.r_[0,t[:-1]]
    jump=np.diff(np.concatenate([np.zeros_like(s[:1]),s]),axis=0)
    # A zero-duration stress change and the next stress step may share a time.
    # Keeping both jumps preserves all states; zero ages give zero creep work.
    age=np.maximum(t[:,None]-jump_time[None,:],0.)
    epoch=np.zeros(N,dtype=int);weights=[v*sup[0]]
    for i in range(1,N):
        if not np.array_equal(sup[i],sup[i-1]):weights.append(v*sup[i])
        epoch[i]=len(weights)-1
    memory=np.zeros_like(s);elastic_point=np.zeros((N,P));elastic_excess_point=np.zeros((N,P))
    delta_stress=np.diff(np.concatenate([s[:1],s]),axis=0)
    psi=np.zeros(N);D=np.zeros(N);removed_memory=np.zeros(N)
    for branch,(M,a,delta) in enumerate(zip((3*K,2*G),(alpha_bulk,alpha_dev),(delta_bulk,delta_dev))):
        beta=1-a;ps=_project(s,branch);J=_project(jump,branch)
        elastic_point+=np.sum(ps*ps,axis=-1)/(2*M)
        elastic_excess_point+=np.sum(_project(delta_stress,branch)**2,axis=-1)/(2*M)
        memory+=delta/(M*beta)*(age**beta@J.reshape(N,-1)).reshape(s.shape)
        grams=[]
        for w in weights:
            X=(J*np.sqrt(w)[None,:,None]).reshape(N,-1)
            grams.append(X@X.T)
        for i in range(N):
            ages=age[i,:i+1];ab=ages[:,None]+ages[None,:]
            F=ages[:,None]**beta+ages[None,:]**beta-ab**beta
            gram=grams[epoch[i]][:i+1,:i+1]
            psi[i]+=delta/(2*M*beta)*np.sum(gram*F)
            if i and dt[i]>0:
                a0=t[i-1]-jump_time[:i+1]
                if np.min(a0)<0:raise ValueError('noncausal stress jump')
                ab0=a0[:,None]+a0[None,:]
                D[i]+=delta/(2*M*beta)*np.sum(gram*((ab0+2*dt[i])**beta-ab0**beta))
            if i and epoch[i]!=epoch[i-1]:
                change=grams[epoch[i-1]][:i+1,:i+1]-gram
                removed_memory[i]+=delta/(2*M*beta)*np.sum(change*F)
    elastic=np.einsum('np,np,p->n',elastic_point,sup,v)
    removed_elastic=np.zeros(N);reference_work=np.zeros(N);hybrid_endpoint=np.zeros(N)
    excess=np.zeros(N);defect_bound=np.zeros(N);stress_strain_error=np.zeros(N)
    pure_elastic=_project(s,0)/(3*K)+_project(s,1)/(2*G)
    error=eps-pure_elastic-memory
    for i in range(N):stress_strain_error[i]=np.max(abs(error[i]))
    for i in range(1,N):
        w=weights[epoch[i]]
        removed_elastic[i]=np.sum(v*(sup[i-1]-sup[i])*elastic_point[i-1])
        reference_work[i]=np.sum(w*(elastic_point[i]-elastic_point[i-1]+np.sum(s[i]*(memory[i]-memory[i-1]),axis=-1)))
        hybrid_endpoint[i]=np.sum(w*np.sum(s[i]*(eps[i]-eps[i-1]),axis=-1))
        excess[i]=np.sum(w*elastic_excess_point[i])
        defect_bound[i]=np.sum(w*np.linalg.norm(s[i],axis=-1)*np.linalg.norm(error[i]-error[i-1],axis=-1))
    stored=elastic+psi;removed=removed_elastic+removed_memory
    delta_stored=np.r_[0,np.diff(stored)]
    balance=reference_work-delta_stored-removed-D
    hybrid_work=hybrid_endpoint-excess
    return dict(time_s=t,dt_s=dt,support_epoch=epoch,stored_elastic_j=elastic,stored_memory_j=psi,
        stored_total_j=stored,physical_D_j=D,reference_work_j=reference_work,
        full_spectrum_balance_error_j=balance,hybrid_endpoint_work_j=hybrid_endpoint,
        elastic_endpoint_excess_j=excess,hybrid_corrected_work_j=hybrid_work,
        hybrid_minus_full_work_j=hybrid_work-reference_work,hybrid_full_work_defect_bound_j=defect_bound,
        exact_memory_strain=memory,actual_strain_error_max=stress_strain_error,
        removed_stored_elastic_j=removed_elastic,removed_stored_memory_j=removed_memory,
        removed_stored_total_j=removed,initial_stored_energy_j=float(stored[0]),
        point_count=P,state_count=N,positive_interval_count=int(np.count_nonzero(dt)),
        support_epoch_count=len(weights),scope='All supplied reference Abel points; removed energy recipient not specified; excludes other laws/fluid/gravity/fill')
