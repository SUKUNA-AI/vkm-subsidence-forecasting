"""Малые воспроизводимые эталоны R3. Запуск: python -X utf8 check_spatial.py."""
from pathlib import Path
import json
import platform

import sympy as sp

OUT = Path(__file__).resolve().parent
Q = sp.Rational
I4 = sp.eye(4)
checks = []


def verify(test_id, condition):
    if not bool(condition):
        raise AssertionError(test_id)
    checks.append(test_id)


def is_zero(matrix):
    return all(sp.simplify(entry) == 0 for entry in matrix)


def tensor_bending():
    x, zz, nu = sp.symbols("x z nu", real=True)
    a, h, b = sp.symbols("a h b", positive=True)
    factor = 1 + b * zz / h
    stress = sp.diag(-factor, -nu * factor, 0)
    strain = (1 + nu) * stress - nu * sp.trace(stress) * sp.eye(3)
    ux = -(1 - nu**2) * x * factor
    uz = ((1 - nu**2) * b * (x*x - a*a)/(2*h)
          + nu*(1 + nu)*(zz + b*zz**2/(2*h)))
    grad = sp.Matrix([[sp.diff(ux,x),0,sp.diff(ux,zz)], [0,0,0],
                      [sp.diff(uz,x),0,sp.diff(uz,zz)]])
    verify("S01.compatibility", is_zero((grad+grad.T)/2-strain))
    verify("S02.equilibrium_free_faces",
           all(sp.diff(stress[i,0],x)+sp.diff(stress[i,2],zz)==0 for i in range(3))
           and is_zero(stress[:,2]))
    density = sp.trace(stress.T * strain)
    verify("S03.tensor_energy", sp.simplify(density-(1-nu**2)*factor**2)==0)
    average = sp.integrate(density,(zz,-h,0))/h
    verify("S04.integrated_energy", sp.simplify(average-(1-nu**2)*(1-b+b*b/3))==0)
    profile = sp.simplify(uz.subs({zz:0,x:a}) - uz.subs(zz,0))
    verify("S05.surface_chord", sp.simplify(profile-(1-nu**2)*b*(a*a-x*x)/(2*h))==0)
    beam = {a:10,h:4,b:Q(3,5),nu:Q(1,4),x:0}
    return profile.subs(beam), average.subs(beam)


def pulse_witness(modes, boundaries, observation_z, final_z, weights):
    # Уровень от общего ступенчатого входа; интервалы перечислены по возрасту.
    n = len(modes)
    moment = sp.Matrix(n,n,lambda j,l: weights[j] * (
        boundaries[l]**modes[j] - boundaries[l+1]**modes[j]))
    hist = sp.Matrix([[z**j for j in modes] for z in observation_z])
    future = sp.Matrix([[final_z**j for j in modes]])
    null, = (hist*moment).nullspace()
    u = null / max(map(abs,null))
    return moment, hist, future, u


def separated_witness(length):
    modes=[1,4,16,64]
    ends=[Q(1),Q(49,50),Q(22,25),Q(1,2),Q(1,100)]
    zhist=[Q(1),Q(49,50),Q(24,25)]
    moment,hist,future,u=pulse_witness(modes,ends,zhist,Q(2,5),[Q(1,4)]*4)
    gap=abs((future*moment*u)[0])
    verify("S06.reachability_zeros", moment.det()!=0 and is_zero(hist*moment*u)
           and all(0 <= (1+sign*v)/2 <= 1 for sign in [-1,1] for v in u))
    verify("S07.network_rank_one", is_zero(sp.kronecker_product(hist*moment*u,sp.Matrix([1,Q(3,4),0])))
           and gap>0)
    # Коэффициент передачи на центральный репер, иллюстративная общая деформация 0.006.
    millimetres=1000*length*Q(6,1000)*gap
    return {"gap_over_characteristic_settlement":float(gap),"center_gap_mm_synthetic":float(millimetres),
            "loading_difference_young_to_old":[float(a) for a in u],"history_rank":hist.rank()}


def coupled_witness():
    K=sp.Matrix([[2,1],[1,2]])
    O=I4-sp.ones(4,4)/2
    V0=sp.Matrix([[1,1,1,1],[1,-1,2,-2]])
    V=V0*O.T
    C=K*V
    rates=[1,2,3,4]
    S=O*sp.diag(*rates)*O.T
    D=S+C.T*K.inv()*C
    g=C.T*K.inv()*sp.Matrix([1,0])
    verify("S08.schur_gradient", is_zero(O.T*O-I4) and is_zero(D-C.T*K.inv()*C-S)
           and K.is_positive_definite and S.is_positive_definite and is_zero(O.T*g-sp.ones(4,1)))
    ends=[Q(1,2)**l for l in range(5)]
    moment,hist,future,u=pulse_witness(rates,ends,[Q(1),Q(3,4),Q(1,2)],Q(1,4),
                                       [1/sp.Integer(j) for j in rates])
    obs2=hist*sp.diag(*list(V0[1,:]))
    modal_state=moment*u
    verify("S09.coupled_reachable", moment.det()!=0 and is_zero(hist*modal_state)
           and max(map(abs,u))==1 and (future*modal_state)[0]!=0)
    verify("S10.geometry_changes_rank",hist.rank()==3 and hist.col_join(hist*2).rank()==3
           and hist.col_join(obs2).rank()==4 and not is_zero(obs2*modal_state))
    el=sp.Matrix([Q(4,3),-Q(1,3)]); zz=sp.Matrix([1,-1,2,3])/8
    zd=C.T*el-D*zz; qd=sp.Matrix([Q(2,9),-Q(3,7)]); f=K*el-C*zz
    dpsi=((K*el-C*zz).T*qd+(-C.T*el+D*zz).T*zd)[0]
    verify("S11.port_balance",(f.T*qd)[0]-dpsi==(zd.T*zd)[0] and (zd.T*zd)[0]>=0)
    bad=D-5*I4
    soft=O[:,0]
    verify("S12.indefinite_negative_control",(soft.T*(bad-C.T*K.inv()*C)*soft)[0]==-4)
    singular=sp.Matrix([[1,-1],[-1,1]]);null=sp.Matrix([1,1])
    verify("S13.unsupported_rigid_load",is_zero(singular*null) and (null.T*sp.Matrix([1,0]))[0]!=0)
    return {"lanes_rank":[hist.rank(),hist.col_join(2*hist).rank(),hist.col_join(obs2).rank()],
            "future_difference":str((future*modal_state)[0]),"second_channel":list(map(str,obs2*modal_state)),
            "compressed_load_contrast":list(map(str,u))}


def transfer_and_measurement():
    x,z,k,mu,A=sp.symbols("x z k mu a",real=True,positive=True)
    ux=A*k*z*sp.exp(-k*z)*sp.sin(k*x); uz=A*(1+k*z)*sp.exp(-k*z)*sp.cos(k*x)
    pressure=2*mu*A*k*sp.exp(-k*z)*sp.cos(k*x)
    lap=lambda w:sp.diff(w,x,2)+sp.diff(w,z,2)
    verify("S14.halfspace",sp.simplify(sp.diff(ux,x)+sp.diff(uz,z))==0
           and is_zero(sp.Matrix([mu*lap(ux)-sp.diff(pressure,x),mu*lap(uz)-sp.diff(pressure,z)]))
           and ux.subs(z,0)==0 and sp.simplify(uz.subs(z,0)-A*sp.cos(k*x))==0)
    v=sp.symbols("v",positive=True)
    transfer=(1+v)*sp.exp(-v)
    verify("S15.attenuation_without_exact_null",transfer.is_positive and sp.diff(transfer,v).simplify()==-v*sp.exp(-v))
    a1,a2=2/sp.E,4/sp.E**3
    verify("S16.two_depth_null",is_zero(sp.Matrix([[a1,a2]])*sp.Matrix([a2,-a1])))
    D=sp.Matrix([[-1,1,0],[0,-1,1]])
    W=sp.diag(1,Q(1,2))*D
    verify("S17.irregular_covariance",W*W.T==sp.Matrix([[2,-Q(1,2)],[-Q(1,2),Q(1,2)]]))
    verify("S18.datum",is_zero(D*sp.ones(3,1)) and (sp.Matrix([[0,0,1]])*sp.ones(3,1))[0]==1)
    lo=sp.Matrix([[Q(3,5),0,Q(4,5)]]);invisible=sp.Matrix([4,0,-3])
    verify("S19.single_LOS",is_zero(lo*invisible) and invisible[2]!=0)
    # Нормировка Кельвина сохраняет двойное свёртывание симметричных тензоров.
    sig=sp.Matrix([[0,3,0],[3,0,0],[0,0,0]]); eps=sig*Q(2,3)
    verify("S20.kelvin_port",sp.trace(sig.T*eps)==12 and (sp.sqrt(2)*3)*(sp.sqrt(2)*2)==12)
    k11=sp.Matrix([[3]]);k12=sp.Matrix([[1]]);k22=sp.Matrix([[2]])
    reduced=k11-k12*k22.inv()*k12.T
    verify("S21.condensation_with_rhs",reduced[0]==Q(5,2) and
           (sp.Matrix([[3,1],[1,2]]).inv()*sp.Matrix([4,3]))[0]==((4-Q(3,2))/reduced[0]))
    # Одна скалярная цель, шум ε. Ошибка двух копий в данных ≤2ε.
    ep=Q(1,100);gain=Q(1,10)
    verify("S22.noise_diameter",2*ep/gain==Q(1,5) and (2*ep/gain)/2==Q(1,10))
    return {"attenuation_kd1_kd3":[float(a1),float(a2)],"rate_covariance_equal_height_variance":str(W*W.T)}


def main():
    length,energy=tensor_bending()
    report={"purpose":"synthetic exact spatial mechanics, no field data","python":platform.python_version(),
            "sympy":sp.__version__,"central_length_m":str(length),"volume_energy_factor":str(energy),
            "separable":separated_witness(length),"coupled":coupled_witness(),
            "transfer":transfer_and_measurement(),"passed":checks}
    (OUT/"R3_RESULTS.json").write_bytes((json.dumps(report,ensure_ascii=False,indent=2)+"\n").encode())
    print(f"{len(checks)} / {len(checks)} PASS")


if __name__=="__main__":
    main()
