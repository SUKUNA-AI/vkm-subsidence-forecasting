import json
import os
from pathlib import Path
import numpy as np
import pytest
from scipy.linalg import expm
from vkm_world.representations.world0.dynamic_schur import DynamicCondensation, modal_projection, project_with_detail
from vkm_world.representations.world0.coupled_forms import (up_residual_jacobian,constrain_system,work_conjugate_transfer,
                           check_internal_incidence,kelvin_vector,matrix_solver_class)
from vkm_world.representations.world0.ports import mechanical_port_basis,port_power_density,PortKind
from vkm_world.representations.world0.closure import state_closure_test

METRICS={}
CONTEXT={x:"synthetic-physical-world-v1" for x in ("geometry","materials","topology","boundary","partition","laws")}

@pytest.fixture(scope="session",autouse=True)
def write_metrics():
    yield
    target=os.environ.get("MATH_METRICS")
    if target:
        Path(target).write_text(json.dumps(METRICS,indent=2,allow_nan=False),encoding="utf-8")

def world():
    # Five storage nodes, four resistive edges, one grounded boundary.
    b=np.zeros((5,4))
    for j in range(4): b[j,j]=1; b[j+1,j]=-1
    k=b@np.diag([1.,2.,3.,4.])@b.T+np.diag([.3,0,0,0,.2])
    c=np.diag([1.,2.,.7,1.5,.8])
    return c,k

def test_exact_condensation_cross_capacity_and_nonzero_history():
    c,k=world(); c[0,1]=c[1,0]=.15
    op=DynamicCondensation(c,k,[0,4],CONTEXT)
    x=np.array([0.,1.,-.2,.7,0.]); state=op.initial_state(x)
    worst=0.
    for j in range(150):
        dt=.01*(1+j%3); f=np.array([np.sin(j*.1),.1,0,0,.2])
        old=op.reconstruct(state).copy()
        trial=op.trial_step(state,f,dt)
        np.testing.assert_array_equal(op.reconstruct(state),old)
        x=np.linalg.solve(k+c/dt,f+c@x/dt)
        state=trial
        worst=max(worst,float(np.max(abs(op.reconstruct(state)-x))))
    METRICS["exact_full_vs_condensed_max_abs"]=worst
    assert worst<2e-12

def test_continuous_solution_dt_convergence_and_static_limit():
    c,k=world(); x0=np.array([0,1,-.2,.7,0.]); f=np.array([1,0,0,0,0.])
    stationary=np.linalg.solve(k,f)
    exact=stationary+expm(-np.linalg.solve(c,k)*2)@(x0-stationary)
    errs=[]
    for dt in [.1,.05,.025]:
        op=DynamicCondensation(c,k,[0,4],CONTEXT); state=op.initial_state(x0)
        for _ in range(round(2/dt)): state=op.trial_step(state,f,dt)
        errs.append(float(np.max(abs(op.reconstruct(state)-exact))))
    assert errs[1]<.6*errs[0] and errs[2]<.6*errs[1]
    np.testing.assert_allclose(op.static_solve(f),stationary,atol=1e-12)
    np.testing.assert_allclose(op.laplace_operator(0),k[np.ix_(op.r,op.r)]-k[np.ix_(op.r,op.i)]@np.linalg.solve(k[np.ix_(op.i,op.i)],k[np.ix_(op.i,op.r)]),atol=1e-12)
    METRICS["continuous_reference_errors_dt_0p1_0p05_0p025"]=errs

def test_laplace_initial_term_complex_and_static_only_negative():
    c,k=world(); op=DynamicCondensation(c,k,[0,4],CONTEXT)
    x0=np.array([0.,1.,0,0,0]); f=np.zeros(5)
    s=1+2j
    full=np.linalg.solve(k+s*c,f+c@x0)
    q=np.linalg.solve(op.laplace_operator(s),op.laplace_rhs(s,f,x0))
    np.testing.assert_allclose(q,full[op.r],atol=2e-14)
    bad=np.linalg.solve(op.laplace_operator(s),op.laplace_rhs(s,f,np.zeros(5)))
    assert np.max(abs(bad-q))>.01
    assert np.max(abs(op.laplace_operator(s)-op.laplace_operator(0)))>.1
    METRICS["lost_initial_state_laplace_error"]=float(np.max(abs(bad-q)))

def test_memory_kernel_formula_and_cross_capacity_guard():
    c=np.eye(2); k=np.array([[2.,-1.],[-1.,3.]])
    op=DynamicCondensation(c,k,[0],CONTEXT)
    for t in [0,.2,2]: np.testing.assert_allclose(op.memory_kernel(t),[[np.exp(-3*t)]],atol=1e-14)
    c[0,1]=.1
    with pytest.raises(ValueError): DynamicCondensation(c,k,[0],CONTEXT).memory_kernel(1)

def test_modal_error_vs_number_of_hidden_states():
    c,k=world(); x0=np.array([0,1.,-.2,.7,0]); f=np.array([1.,0,0,0,0]); dt=.02
    detailed=[]; x=x0.copy()
    for _ in range(200):
        x=np.linalg.solve(c/dt+k,f+c@x/dt); detailed.append(x.copy())
    errors=[]
    for m in range(4):
        t,cm,km=modal_projection(c,k,[0,4],m)
        z=np.linalg.solve(cm,t.T@c@x0)
        approx=[]
        for _ in range(200):
            z=np.linalg.solve(cm/dt+km,t.T@f+cm@z/dt); approx.append(t@z)
        errors.append(float(np.max(abs(np.asarray(approx)[:,[0,4]]-np.asarray(detailed)[:,[0,4]]))))
        assert np.linalg.eigvalsh(km).min()>0 and np.linalg.eigvalsh(cm).min()>0
        # Static constraint modes preserve boundary-only static solution.
        np.testing.assert_allclose(t@np.linalg.solve(km,t.T@f),np.linalg.solve(k,f),atol=2e-12)
    assert errors[-1]<2e-12
    assert errors[0]>1e-3
    METRICS["modal_surface_errors_m_0_1_2_3"]=errors
    METRICS["modal_scope"]="one world, fixed forcing, H=4s, sup retained-state norm; no uniform monotonic error theorem"

def test_same_surface_distinct_memory_and_future():
    c,k=world(); a=np.zeros(5); b=a.copy(); b[1]=1
    response=lambda x: (expm(-np.linalg.solve(c,k)*.5)@x)[[0,4]]
    result=state_closure_test([a,b],lambda x:x[[0,4]],response,1e-6)
    assert result.verdict=="STATE_INSUFFICIENT"
    METRICS["same_surface_hidden_memory_future_spread"]=result.spread

def test_split_merge_conserves_storage_energy_and_history_with_detail():
    m=np.diag([2.,3.,4.,1.]); p=np.array([[1.,0],[1,0],[0,1],[0,1]])
    x=np.array([1.,3.,4.,8.]); q,z=project_with_detail(x,p,m)
    np.testing.assert_allclose(p@q+z,x,atol=1e-14)
    np.testing.assert_allclose(p.T@m@z,0,atol=1e-14)
    np.testing.assert_allclose(np.ones(4)@m@(p@q),np.ones(4)@m@x,atol=1e-14)
    ef=.5*x@m@x; ec=.5*q@(p.T@m@p)@q; ed=.5*z@m@z
    assert abs(ef-ec-ed)<2e-13 and ed>1
    history=np.array([.2,.7,-.1,.9]); hq,hz=project_with_detail(history,p,m)
    np.testing.assert_allclose(p@hq+hz,history,atol=1e-14)
    METRICS["merge_energy_total_coarse_detail"]=[ef,ec,ed]

def test_passivity_be_identity():
    c,k=world(); op=DynamicCondensation(c,k,[0],CONTEXT)
    x0=np.array([.1,.2,-.4,.2,0]); f=np.array([.3,0,0,0,0]); dt=.2
    x=op.reconstruct(op.trial_step(op.initial_state(x0),f,dt)); d=x-x0
    # Storage functional H=x^T C x/2; not automatically mechanical elastic energy.
    balance=.5*x@c@x-.5*x0@c@x0+.5*d@c@d+dt*x@k@x-dt*x@f
    assert abs(balance)<1e-13
    METRICS["be_passivity_identity_residual"]=float(balance)

def test_rebinding_invalidates_capacity_law_partition_and_restart():
    c,k=world(); op=DynamicCondensation(c,k,[0],CONTEXT)
    op.assert_applicable(c,k,CONTEXT)
    changed=c.copy(); changed[0,0]+=1
    with pytest.raises(ValueError): op.assert_applicable(changed,k,CONTEXT)
    with pytest.raises(ValueError): op.assert_applicable(c,k,{**CONTEXT,"laws":"new"})
    with pytest.raises(ValueError): DynamicCondensation(c,k,[1],CONTEXT).reconstruct(op.initial_state(np.zeros(5)))
    state=op.trial_step(op.initial_state(np.arange(5)),np.ones(5),.1)
    restored=op.initial_state(op.reconstruct(state),state.time_s)
    np.testing.assert_array_equal(op.reconstruct(op.trial_step(state,np.zeros(5),.2)),op.reconstruct(op.trial_step(restored,np.zeros(5),.2)))

@pytest.mark.parametrize("r",[[0,0],[.5],[],[5],[-1]])
def test_bad_partition_rejected(r):
    c,k=world()
    with pytest.raises(ValueError): DynamicCondensation(c,k,r,CONTEXT)

def test_singular_elimination_rejected():
    op=DynamicCondensation(np.zeros((2,2)),np.zeros((2,2)),[0],CONTEXT)
    with pytest.raises(np.linalg.LinAlgError):op.trial_step(op.initial_state([0,0]),[0,0],1)

def test_up_manufactured_jacobian_and_solver_class():
    k=np.array([[4.,-1],[-1,3]]); q=np.array([[1.],[-.5]]); s=np.array([[.3]]); h=np.array([[.2]])
    u=np.array([.2,-.1]); p=np.array([.7]); u0=np.array([.1,.2]); p0=np.array([.4]); dt=.3
    f=k@u-q@p; g=q.T@(u-u0)/dt+s@(p-p0)/dt+h@p
    args=(k,q,s,h,u,p,u0,p0,dt,f,g)
    residual,j=up_residual_jacobian(*args)
    assert np.linalg.norm(residual)<1e-14
    numeric=np.zeros_like(j); x=np.r_[u,p]; eps=1e-6
    for i in range(3):
        d=np.eye(3)[i]*eps
        plus=up_residual_jacobian(k,q,s,h,(x+d)[:2],(x+d)[2:],u0,p0,dt,f,g)[0]
        minus=up_residual_jacobian(k,q,s,h,(x-d)[:2],(x-d)[2:],u0,p0,dt,f,g)[0]
        numeric[:,i]=(plus-minus)/(2*eps)
    error=float(np.max(abs(j-numeric))); assert error<1e-8
    assert matrix_solver_class(k)=="SPD_CG_PERMITTED"
    assert matrix_solver_class(j)=="GENERAL_DIRECT_OR_GMRES"
    scaled=j.copy(); scaled[2:]*=-dt
    assert matrix_solver_class(scaled)=="SYMMETRIC_INDEFINITE_MINRES_OR_DIRECT"
    METRICS["up_jacobian_fd_error"]=error

def test_boundary_lifting_and_rigid_mode_pressure_gauge():
    a=np.array([[1.,-1],[-1,1.]])
    assert matrix_solver_class(a).startswith("SINGULAR")
    ar,br,lift,free=constrain_system(a,np.array([-.5,.5]),{0:.2})
    x=lift.copy(); x[free]=np.linalg.solve(ar,br)
    np.testing.assert_allclose(x,[.2,.7],atol=1e-14)
    np.testing.assert_allclose(a@x,[-.5,.5],atol=1e-14)
    # Neumann flow gauge appears only when storage and coupling do not fix it.
    gauge=np.array([[0,1.]])
    saddle=np.block([[a,gauge.T],[gauge,np.zeros((1,1))]])
    assert np.linalg.matrix_rank(saddle)==3
    duplicate=np.r_[gauge,gauge]
    assert np.linalg.matrix_rank(duplicate)<len(duplicate)

def test_ports_work_mass_thermal_and_negative_orientation():
    p=np.array([[1.,0],[.4,.6],[0,1]])
    area=np.array([.2,.3,.5]); traction=np.array([4.,-1.,2]); velocity=np.array([.3,-.2])
    fc=work_conjugate_transfer(p,traction,area)
    work_f=float((p@velocity)@(area*traction)); work_c=float(velocity@fc)
    assert abs(work_f-work_c)<1e-15
    assert abs(velocity@(p.T@traction)-work_f)>.1
    b=np.array([[1,0,-1],[-1,1,0],[0,-1,1]])
    assert check_internal_incidence(b)
    assert abs(np.ones(3)@(b@np.array([.1,.3,-.2])))<1e-15
    b[1,0]=1
    with pytest.raises(ValueError):check_internal_incidence(b)
    with pytest.raises(ValueError):mechanical_port_basis([1,0,0],[0,1,0],[1,0,0])
    basis=mechanical_port_basis([1,0,0],[0,1,0],[-1,0,0])
    np.testing.assert_allclose(basis@basis.T,np.eye(3))
    assert port_power_density(PortKind.HYDRAULIC,2e6,1e-6)==2
    assert port_power_density(PortKind.THERMAL,300,12)==12
    assert 300*(12/300)==12
    METRICS["port_virtual_work_error"]=abs(work_f-work_c)

def test_kelvin_work_and_engineering_voigt_counterexample():
    stress=np.array([[2.,3,1],[3,4,-2],[1,-2,5]])
    strain=np.array([[.1,.2,.3],[.2,-.1,.8],[.3,.8,.2]])
    assert abs(kelvin_vector(stress)@kelvin_vector(strain)-np.sum(stress*strain))<1e-14
    naive=np.array([2,4,5,3,1,-2])@np.array([.1,-.1,.2,.2,.3,.8])
    assert abs(naive-np.sum(stress*strain))>.1


def test_event_operator_change_keeps_surviving_memory():
    c,k=world(); old=DynamicCondensation(c,k,[0,4],CONTEXT)
    state=old.trial_step(old.initial_state([0,1,.2,-.3,0]),np.ones(5),.1)
    changed=k+np.diag([.1,.2,0,0,0])
    new=DynamicCondensation(c,changed,[0,4],{**CONTEXT,"materials":"event-new"})
    with pytest.raises(ValueError):new.trial_step(state,np.zeros(5),.1)
    transferred=new.initial_state(old.reconstruct(state),state.time_s)
    x=new.reconstruct(new.trial_step(transferred,np.zeros(5),.1))
    reference=np.linalg.solve(changed+c/.1,c@old.reconstruct(state)/.1)
    np.testing.assert_allclose(x,reference,atol=1e-13)
    assert np.linalg.norm(transferred.hidden)>0


def test_one_dimensional_mixed_rank_and_mesh_stability_witness():
    # P1 displacement/P0 pressure in a unit bar, both endpoints fixed;
    # eliminate pressure-constant gauge, inspect normalized Schur spectrum.
    betas=[]
    for n in [4,8,16]:
        h=1/n; gradient=np.zeros((n,n-1))
        for cell in range(n):
            if cell>0:gradient[cell,cell-1]=-1/h
            if cell<n-1:gradient[cell,cell]=1/h
        k=h*gradient.T@gradient
        q=h*gradient.T
        schur=q.T@np.linalg.solve(k,q)/h
        eig=np.linalg.eigvalsh(schur)
        assert abs(eig[0])<1e-12
        betas.append(float(np.sqrt(eig[1])))
    np.testing.assert_allclose(betas,np.ones(3),atol=1e-12)
    METRICS["1d_inf_sup_beta_n_4_8_16"]=betas


def test_current_water_operator_matches_discrete_up_residual():
    from vkm_world.representations.world0 import hydromechanics as mod
    k=np.array([[4.,-1.],[-1.,3.]])
    q=np.array([[.2,-.1],[.05,.15]])
    storage=np.array([.3,.4]); dt=.2
    model=mod.WaterModel(q,storage,np.ones(2),np.ones(2)*.7,np.ones(2)*1e5,
                         np.zeros(2),[[0,1,.2]],0,.1,["A","B"],"synthetic")
    u0=np.array([.1,-.2]); p0=np.array([.3,-.1]); f=np.array([.4,-.2]); target=.2
    previous=mod.WaterState(p0,q.T@u0,"synthetic")
    trial=model.trial(previous,dt,lambda rhs:np.linalg.solve(k,rhs),np.diag(k),f,well_target_pa=target)
    g=np.array([model.well_t*target,0.])
    residual,j=up_residual_jacobian(k,q,np.diag(storage),model.L.toarray(),trial.displacement_free,
                                   trial.state.pressure_pa,u0,p0,dt,f,g)
    assert np.max(abs(residual))<1e-10
    rhs=np.r_[f,q.T@u0/dt+storage*p0/dt+g]
    direct=np.linalg.solve(j,rhs)
    np.testing.assert_allclose(direct,np.r_[trial.displacement_free,trial.state.pressure_pa],atol=1e-10)
    METRICS["current_water_operator_up_residual"]=float(np.max(abs(residual)))
