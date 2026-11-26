"""Независимые синтетические проверки R1. Только CPU, длины в м, время в годах.
Запуск: python check_surface.py . Результаты создаются рядом со скриптом.
"""
from pathlib import Path
from fractions import Fraction
import json
import platform

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

OUT = Path(__file__).resolve().parent
# Допуски до эксперимента. Равенства проверяются в указанных ниже шкалах.
EXACT = 2e-12
FD = 2e-8
results = []


def check(name, actual, expected, tol=EXACT):
    actual = np.asarray(actual, dtype=float)
    expected = np.asarray(expected, dtype=float)
    error = float(np.max(np.abs(actual - expected)))
    results.append(dict(id=name, expected=expected.tolist(), actual=actual.tolist(),
                        abs_tol=tol, max_abs_error=error,
                        status="PASS" if np.isfinite(error) and error <= tol else "FAIL"))


def design(x, y):
    return np.column_stack([np.ones_like(x), x, y, x*x/2, x*y, y*y/2])


# C01. Аналитический пик A[1-(1+t)exp(-t)] и форма конечного окна.
x = np.linspace(-3, 3, 61)
f = np.exp(-x*x/2)
F = lambda t: 1-(1+t)*np.exp(-t)
d1, d2 = (F(.3)-F(.1))*f, (F(.8)-F(.3))*f
check("C01 shape", d1/np.linalg.norm(d1), d2/np.linalg.norm(d2))
eps = 1e-4
check("C01 positive acceleration", (F(.5+eps)-2*F(.5)+F(.5-eps))/eps**2,
      .5*np.exp(-.5), 5e-8)

# C02. Один и тот же анизотропный гауссов профиль подвергаем известному
# усилению, переносу, повороту и изотропной дилатации. Скорость по симметричной
# временной разности наблюдений сравнивается с аналитическими генераторами.
X, Y = np.meshgrid(np.linspace(-2, 2, 13), np.linspace(-2, 2, 13))
xx, yy = X.ravel(), Y.ravel()
def patch(t, coeff, width):
    alpha, cx, cy, omega, dilation = coeff
    angle = omega*t
    dx, dy = xx-cx*t, yy-cy*t
    xbody = (np.cos(angle)*dx+np.sin(angle)*dy)*np.exp(-dilation*t)
    ybody = (-np.sin(angle)*dx+np.cos(angle)*dy)*np.exp(-dilation*t)
    return np.exp(alpha*t-.5*(xbody**2+ybody**2/width**2))

def generators(width):
    v = np.exp(-.5*(xx*xx+yy*yy/width**2))
    gx, gy = -xx*v, -yy*v/width**2
    return np.column_stack([v, -gx, -gy, yy*gx-xx*gy, -xx*gx-yy*gy])

basis = generators(1.6)
for name, coeff in {"translation":[0,.4,-.2,0,0],"rotation":[0,0,0,.3,0],
                    "width":[0,0,0,0,.2],"mixed":[.7,.4,-.2,.3,.2]}.items():
    dt = 1e-5
    dv = (patch(dt,coeff,1.6)-patch(-dt,coeff,1.6))/(2*dt)
    check("C02 "+name, np.linalg.lstsq(basis,dv,rcond=None)[0], coeff, FD)
check("C02 ranks", [np.linalg.matrix_rank(basis),np.linalg.matrix_rank(generators(1))], [5,4])
# v(x,t)=exp[-(x-2t)^2/2] tested as settlement pattern here.
def moving(x, t):
    return np.exp(-.5*(x-2*t)**2)
step=1e-3
hessian=(moving(step,0)-2*moving(0,0)+moving(-step,0))/step**2
accel=(moving(0,step)-2*moving(0,0)+moving(0,-step))/step**2
mixed=(moving(step,step)-moving(step,-step)-moving(-step,step)+moving(-step,-step))/(4*step**2)
# Central stencils have O(step²) truncation error; absolute tolerance 3e-5.
check("C02 peak drift/accelerations", [-mixed/hessian,accel,accel-mixed**2/hessian],
      [2.,-4.,0.],3e-5)

# C03. Quartic double peak with merge, one-sided boundary and switching branches.
u=.25
candidates=np.array([-np.sqrt(u),0,np.sqrt(u)])
check("C03 double peak", 1-(candidates**2-u)**2, [1,15/16,1])
check("C03 degenerate second derivative", ((1-eps**4)-2+(1-eps**4))/eps**2,0,3e-8)
check("C03 boundary argmax", np.argmax(1-(np.arange(0,1.001,.01)+.2)**2),0)
check("C03 switch slopes", [(1-max(1-eps,1+eps))/eps,(max(1+eps,1-eps)-1)/eps],[-1,1])

# C04. Unequal epochs h=1,k=3. Cubic is deliberately biased at the central epoch.
def differences(fn,h,k):
    left=(fn(0)-fn(-h))/h
    right=(fn(k)-fn(0))/k
    return [(k*left+h*right)/(h+k),2*(right-left)/(h+k)]
check("C04 quadratic",differences(lambda t: 3+5*t+2*t*t,1,3),[5,4])
check("C04 cubic bias",differences(lambda t:t**3,1,3),[3,4])
post=lambda t:2*t+3*(t>=1)
check("C04 right-closed jump",[post(1)-post(0),post(3)-post(1),post(3)-post(0)],[5,4,9])
# Identical observations at t=0,2 for two different possible event dates.
check("C04 unresolved event date", [int(2>=.3)-int(0>=.3),int(2>=1.7)-int(0>=1.7)],[1,1])

# C05. Central differences vs quadratic least squares on one line / 2D grid.
# Exact RMS including bias. For correlated noise compute the covariance by a
# second independent route using a square root of the kernel. No estimated RMS.
statistics=[]
grid=np.arange(-2,3,dtype=float)
for dimension in [1,2]:
    if dimension==1:
        pts=grid[:,None]
        x=pts[:,0]
        A=np.column_stack([np.ones_like(x),x,x*x/2])
        truth=1+2*x+x*x/2+.04*x**4
    else:
        X,Y=np.meshgrid(grid,grid)
        x,y=X.ravel(),Y.ravel()
        pts=np.column_stack([x,y]);A=design(x,y)
        truth=1+2*x+x*x/2+.04*x**4+.3*x*y-y*y
    center=np.all(pts==0,axis=1); left=pts[:,0]==-1; right=pts[:,0]==1
    if dimension==2:
        left &= pts[:,1]==0; right &= pts[:,1]==0
    local=np.linalg.pinv(A)
    for quantity,col in [("s",0),("gx",1),("Hxx",2 if dimension==1 else 3)]:
        exact={"s":1,"gx":2,"Hxx":1}[quantity]
        direct={"s":center.astype(float),"gx":(right.astype(float)-left)/2,
                "Hxx":right.astype(float)+left-2*center}[quantity]
        for noise,rho in [("independent",0.),("correlated",.65)]:
            # Separable stationary covariance sigma^2 rho^ManhattanDistance; PSD.
            dist=np.abs(pts[:,None,:]-pts[None,:,:]).sum(axis=2)
            cov=.1**2*np.power(rho,dist)
            for method,weight in [("central",direct),("polynomial",local[col])]:
                bias=float(weight@truth-exact)
                variance=float(weight@cov@weight)
                rms=float(np.sqrt(bias*bias+variance))
                check(f"C05 {dimension}D {quantity} {noise} {method} variance",
                      np.linalg.norm(weight@np.linalg.cholesky(cov))**2, variance)
                if method=="central" and quantity=="Hxx":
                    check(f"C05 {dimension}D {noise} analytic Hessian variance",variance,
                          .1**2*(6-8*rho+2*rho**2))
                statistics.append(dict(dim=dimension,quantity=quantity,noise=noise,
                                       method=method,bias=bias,sd=variance**.5,rms=rms))
check("C05 harmonic curvature",.001*(2*np.pi/10)**2,.0003947841760435743)

# C06. Directional and area geometry, including a curved track on a tilted plane.
dirs=np.array([[1.,0],[0,1],[1/np.sqrt(2),1/np.sqrt(2)]])
qx,qy=dirs.T
rows=np.column_stack([qx*qx,2*qx*qy,qy*qy])
check("C06 directional ranks",[np.linalg.matrix_rank(rows[:2]),np.linalg.matrix_rank(rows)],[2,3])
check("C06 full quadratic ranks",[np.linalg.matrix_rank(design(grid,np.zeros_like(grid))),
                                np.linalg.matrix_rank(design(xx,yy))],[3,6])
def invisible(x,y):
    return 3*y+2*x*y+4*y*y
check("C06 invisible additions",invisible(grid,0),np.zeros_like(grid))
check("C06 undetermined derivatives",
      [(invisible(0,1)-invisible(0,-1))/2,
       (invisible(1,1)-invisible(-1,1)-invisible(1,-1)+invisible(-1,-1))/4,
       invisible(0,1)-2*invisible(0,0)+invisible(0,-1)],[3,2,8])
check("C06 curve on affine plane",(2*np.cos(eps)-2)/eps**2,-1,FD)

# C07. Four positive Kelvin retardation weights. At common stress and zero
# initial strain analytic differences are a factorized polynomial. A separate
# example uses the SAME two elements but differently aged past stress pulses.
f1=[Fraction(1)]*4
f2=list(map(Fraction,["39/40","6/5","23/40","5/4"]))
def creep(z,weights):
    return sum(w*(1-z**j) for j,w in enumerate(weights,1))
for z in [Fraction(1),Fraction(1,2),Fraction(1,5),Fraction(1,10),Fraction(0)]:
    actual=creep(z,f2)-creep(z,f1)
    expected=z*(1-z)*(z-Fraction(1,2))*(z-Fraction(1,5))/4
    check("C07 response z="+str(z),float(actual),float(expected),0)
T=np.array([1.,.5])
def pulse(age):
    state=(1-np.exp(-.25/T))*np.exp(-age/T)
    return state/state.sum()
ea,eb=pulse(.25),pulse(1.25)
check("C07 common initial output",ea.sum(),eb.sum())
diff=ea-eb
check("C07 future split", np.sum(diff*np.exp(-1/T)),diff[0]*(np.exp(-1)-np.exp(-2)))
check("C07 nonzero future split",abs(np.sum(diff*np.exp(-1/T)))>.01,True,0)
# Spring E=1 and viscosity eta=T, zero future stress; energy rate plus dissipation.
edot=-ea/T
check("C07 energy identity",ea@edot+(T*edot)@edot,0)
check("C07 passive dissipation",(T*edot)@edot>0,True,0)

# C08-09. Common zero, shifted benchmark, missing epoch and sensitivity.
D=np.array([[-1,1,0],[0,-1,1]])
check("C08 common zero",D@np.full(3,17.),[0,0])
check("C08 epoch reference step",D@np.array([17.,17.,19.]),[0,2])
check("C08 covariance",D@(np.eye(3)+2*np.ones((3,3)))@D.T,[[2,-1],[-1,2]])
check("C08 missing link nullity",4-np.linalg.matrix_rank([[-1,1,0,0],[0,0,-1,1]]),2)
reference_column=np.ones((2,1))
projector=np.eye(2)-reference_column@np.linalg.pinv(reference_column)
check("C09 projected nuisance",projector,[[.5,-.5],[-.5,.5]])
check("C09 weak history/strong target", [np.linalg.norm(np.diag([1,1e-4])@[0,1]),np.dot([0,1],[0,1])],[1e-4,1])
noise_norm=.02
small_norm=.01
check("C09 unresolved normalized signal", small_norm<=noise_norm,True,0)
# Flat peak s=-mu*x^2/2 and small perturbation slope n over [-1,1].
grid_peak=np.linspace(-1,1,201)
base=-.0005*grid_peak**2
changed=base+.0008*grid_peak
check("C09 ill-conditioned peak",grid_peak[np.argmax(changed)]-grid_peak[np.argmax(base)],.8)
check("C09 amplitude Lipschitz",abs(changed.max()-base.max())<=.0008,True,0)

# C10. Normalized log-sum-exp, attenuation, finite masks and width of level sets.
values=np.array([0.,1.]); beta=4.
soft=np.log(np.mean(np.exp(beta*values)))/beta
check("C10 soft peak bounds", 0<1-soft<=np.log(2)/beta,True,0)
radius=1.;k=4.;xg=np.linspace(-5,5,4001)
atten=np.trapezoid(np.exp(-np.pi*xg*xg)*np.cos(k*xg),xg)
check("C10 Gaussian transfer",atten,np.exp(-k*k/(4*np.pi)))
check("C10 different masks mimic growth", np.dot([.25,.75],values)-np.dot([.5,.5],values),.25)
# A=2 exp(t), ell=1, level=1, area of disk equals 2*pi*log(A).
check("C10 superlevel area rate",(2*np.pi*np.log(2*np.exp(eps))-2*np.pi*np.log(2*np.exp(-eps)))/(2*eps),2*np.pi,FD)

report=dict(scope="SYNTHETIC_CPU_ONLY",units="m, year; C02 time in yr, X/Y in m; C05 d=1 m, sigma=0.1 m",
            python=platform.python_version(),numpy=np.__version__,matplotlib=matplotlib.__version__,
            tests=results,derivative_errors=statistics,
            unresolved=["C04 event date within interval","C02 circle orientation",
                        "C09 shape below error norm","C06 Hessian transverse to a single line"])
(OUT/"RESULTS.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
fig,axes=plt.subplots(1,3,figsize=(12,3.65),layout="constrained")
for t in [.15,.45,.75]:
    axes[0].plot(np.linspace(-3,3,61),F(t)*f,label=f"t/T = {t:.2f}")
axes[0].set(xlabel="x / ℓ",ylabel="s / A₀",title="Усиление при той же форме")
axes[0].legend(fontsize=8)
times=np.linspace(0,3,301)
axes[1].plot(times,1000*np.array([creep(np.exp(-v),np.array(f2,float))-creep(np.exp(-v),np.array(f1,float)) for v in times]))
axes[1].scatter([0,np.log(2),np.log(5)],[0]*3,color="black",s=18)
axes[1].axhline(0,lw=.6,color=".5");axes[1].set(xlabel="t / T",ylabel="1000 (s₂ − s₁) / A₀",title="Равенство в трёх эпохах")
a=[row for row in statistics if row["dim"]==1 and row["quantity"]=="Hxx"]
axes[2].bar(np.arange(4),[row["sd"] for row in a],color=["#30678a","#bd8844"]*2)
axes[2].set_xticks(np.arange(4),["разн.","полин.","разн.","полин."])
axes[2].set(xlabel="Без корреляции       С корреляцией",ylabel="СКО второй производной, м⁻¹",title="Ошибки на профиле")
fig.suptitle("Синтетические примеры R1",fontsize=13)
fig.savefig(OUT/"SYNTHETIC_R1.png",dpi=160);plt.close(fig)
failed=[r["id"] for r in results if r["status"]!="PASS"]
print(f"{len(results)-len(failed)}/{len(results)}; failed={failed}")
raise SystemExit(bool(failed))
