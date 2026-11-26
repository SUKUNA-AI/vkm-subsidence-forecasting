"""Независимые CPU-проверки аналитических примеров AMHF-R2. Запуск из любого каталога."""
from fractions import Fraction as F
from pathlib import Path
import json
import platform

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import scipy
from scipy.integrate import quad
from scipy.linalg import null_space
from scipy.optimize import linprog
from scipy.special import gamma, expit
import sympy as sp

OUT = Path(__file__).resolve().parent
tests = []
rates = np.arange(1, 5, dtype=float)
weights = np.full(4, 0.25)
epochs = np.log([1.0, 4.0 / 3, 2.0])


def equal(name, actual, expected, atol=1e-10, rtol=1e-9):
    a, b = np.asarray(actual, dtype=float), np.asarray(expected, dtype=float)
    error = float(np.max(np.abs(a - b)))
    ok = np.all(np.isfinite(a)) and np.allclose(a, b, rtol=rtol, atol=atol)
    tests.append(dict(id=name, status="PASS" if ok else "FAIL",
                      max_abs_error=error, atol=atol, rtol=rtol,
                      actual=a.tolist(), expected=b.tolist()))


def pulse_matrix(duration, bins):
    step = duration / bins
    young = np.arange(bins) * step
    return (weights * (-np.expm1(-rates * step)))[:, None] * np.exp(-np.outer(rates, young))


def response(times, matrix):
    return np.exp(-np.outer(np.atleast_1d(times), rates)) @ matrix


def exact_pair():
    # Четыре интервала с отсчётом возраста от начала общих воздействий.
    roots = [sp.Rational(1), sp.Rational(3, 4), sp.Rational(1, 2)]
    z = sp.symbols("z")
    polynomial = sp.Poly(z * sp.prod(z - root for root in roots), z)
    c = sp.Matrix([polynomial.nth(j) for j in range(1, 5)])
    matrix = sp.Matrix(4, 4, lambda j, l:
                       sp.Rational(1, 4) * (1 - sp.Rational(1, 2)**(j+1)) *
                       sp.Rational(1, 2)**((j+1)*l))
    u = matrix.inv() * c
    scale = 1 / max(abs(item) for item in u)
    u *= scale
    c *= scale
    assert matrix.det() != 0 and matrix * u == c
    assert all(polynomial.eval(q) == 0 for q in roots)
    final = polynomial.eval(sp.Rational(1, 4)) * scale
    assert final != 0
    report = dict(unscaled_coefficients=[str(t) for t in polynomial.all_coeffs()],
                  determinant=str(matrix.det()), scale=str(scale), controls=[str(t) for t in u],
                  modal_differences=[str(t) for t in c], future_exact=str(final))
    return report, np.array(u, dtype=float).ravel(), float(final)


def roll_kelvin(stress, durations, mode_rates=rates):
    q = np.zeros_like(mode_rates, dtype=float)
    work = 0.0
    diss = 0.0
    for u, step in zip(stress, durations):
        delta = weights*u - q
        one_minus = -np.expm1(-mode_rates*step)
        work += u * np.sum(delta*one_minus)
        diss += np.sum(delta**2/(2*weights) * (-np.expm1(-2*mode_rates*step)))
        q += delta*one_minus
    energy = np.sum(q*q/(2*weights))
    return q, work, energy, diss


def bounded_pair(A, b, eps):
    # Разность историй ограничена единицей; каждый из двух рядов имеет ошибку eps.
    m, n = A.shape
    bounds = [(-1, 1)]*n
    opts = {"primal_feasibility_tolerance": 1e-10, "dual_feasibility_tolerance": 1e-10}
    if eps == 0:
        result = linprog(-b, A_eq=A, b_eq=np.zeros(m), bounds=bounds, method="highs", options=opts)
        multipliers = -result.eqlin.marginals if result.success else None
    else:
        result = linprog(-b, A_ub=np.vstack([A, -A]), b_ub=np.full(2*m, 2*eps),
                         bounds=bounds, method="highs", options=opts)
        multipliers = (-result.ineqlin.marginals[:m] + result.ineqlin.marginals[m:]
                       if result.success else None)
    if not result.success:
        raise RuntimeError(result.message)
    dual = np.sum(abs(b - multipliers @ A)) + 2*eps*np.sum(abs(multipliers))
    u = result.x.copy()
    if eps == 0:
        basis = null_space(A)
        u = basis @ (basis.T @ u)
    else:
        res = np.max(np.abs(A @ u))
        if res > 2*eps:
            u *= 2*eps/res
    u /= max(1., np.max(np.abs(u)))
    return float(b @ u), u, multipliers, float(dual)


def continuous_upper(times, target, duration, eps, v):
    # Интеграл модуля подписанного экспоненциального полинома по возрасту нагрузки.
    amps = weights * (np.exp(-rates*target) - v @ np.exp(-np.outer(times, rates)))
    roots = np.polynomial.polynomial.polyroots(np.r_[0., rates*amps])
    interior = [-np.log(z.real) for z in roots if abs(z.imag) < 1e-8
                and np.exp(-duration) < z.real < 1.]
    grid = np.array([0., *sorted(interior), duration])
    jumps = amps @ (np.exp(-np.outer(rates, grid[:-1])) -
                    np.exp(-np.outer(rates, grid[1:])))
    val = np.sum(np.abs(jumps))
    integral = quad(lambda a: abs(np.sum(rates*amps*np.exp(-rates*a))),
                    0., duration, points=interior, epsabs=1e-13)[0]
    return float(val+2*eps*np.sum(abs(v))), abs(integral-val)


def forecast_band(duration, horizon, eps, times=epochs, bins=24):
    matrix = pulse_matrix(duration, bins)
    target = times[-1]+horizon
    A = response(times, matrix)
    b = response(target, matrix)[0]
    lower, u, v, dual = bounded_pair(A, b, eps)
    upper, int_error = continuous_upper(times, target, duration, eps, v)
    return dict(D=float(duration), H=float(horizon), eps=float(eps), bins=bins,
                lower=lower, upper=upper, dual_gap=abs(dual-lower),
                obs_violation=float(max(0., max(abs(A@u))-2*eps)),
                quadrature_error=float(int_error))


def abel_lab():
    # Параметры строки 0.2 таблицы 11 SRC-044. Время исходной единицы δ — секунды.
    alpha, delta = .69, .047
    nu = 1-alpha
    edges = np.arange(5, dtype=float)
    def row(t):
        return np.diff((t+edges)**nu)
    A = np.array([row(t) for t in [0., 1., 2.]])
    direction = null_space(A)[:, 0]
    direction /= max(abs(direction))
    b = row(6.)
    gap = float(abs(b@direction))
    equal("A01: cancellation of fractional histories", A@direction, [0]*3)
    equal("A02: nonzero fractional witness", gap > 1e-7, True)
    # Независимо через квадратуру ядра; в первом отрезке при t=0 есть интегрируемая особенность.
    for t in [0., 1., 2., 6.]:
        values = [quad(lambda a: nu*(t+a)**(-alpha), i, i+1)[0] for i in range(4)]
        equal("A03: exact interval moments "+str(t), row(t), values)
    coeff = {}
    for h in (6., 24., 96.):
        mult = delta/nu*(h*3600)**nu
        coeff[str(h)] = float(mult*gap)
    # При фиксированной амплитуде напряжения и геометрии физический масштаб L P/E.
    return dict(alpha=alpha, delta_per_s_power=delta, ages_h=[6,24,96],
                coeff_per_LP_over_E=coeff, pulse_controls=direction.tolist(),
                unit_gap=gap, determinant_augmented=float(np.linalg.det(np.vstack([A,b]))))


def spectrum_test():
    # Все времена представлены рациональными z. Подбирается только доказательный
    # вход двух копий одной модели. Материальные параметры внутри пары постоянны.
    R = sp.Rational
    ends = [R(1), R(49,50), R(22,25), R(1,2), R(1,100)]
    target = R(2,5)
    cases = []
    for js in [[1,2,3,4], [1,4,16,64]]:
        M = sp.Matrix(4,4,lambda j,l: (ends[l]**js[j]-ends[l+1]**js[j])/4)
        for zz in [[R(1),R(3,4),R(1,2)], [R(1),R(49,50),R(24,25)]]:
            A = sp.Matrix([[z**j for j in js] for z in zz])*M
            n, = A.nullspace()
            u = n/max(abs(x) for x in n)
            assert A*u == sp.zeros(3,1) and max(abs(x) for x in u)==1
            val = abs((sp.Matrix([[target**j for j in js]]) * M * u)[0])
            assert val > 0
            cases.append(dict(rates=js, z_hist=[float(z) for z in zz], gap=float(val),
                              controls=[float(x) for x in u], final_z=float(target),
                              age_edges_z=[float(v) for v in ends]))
    equal("S01: separated rates and short history give percent-level witness",
          .018 < cases[-1]["gap"] < .019,True)
    # Для самой большой предъявленной пары интегрируем обе абсолютные истории
    # от начала подготовки. Порядок ступеней здесь хронологический.
    witness = cases[-1]
    age_edges = -np.log(witness["age_edges_z"])
    durations = np.append(np.diff(age_edges)[::-1], -np.log(witness["final_z"]))
    controls = np.asarray(witness["controls"])[::-1]
    modal_rates = np.asarray(witness["rates"], dtype=float)
    balances = []
    for sign in [1, -1]:
        stress = np.append((1 + sign * controls) / 2, 1.0)
        q, work, stored, dissipated = roll_kelvin(stress, durations, modal_rates)
        assert np.all((stress >= 0) & (stress <= 1)) and dissipated >= 0
        equal(f"S02: wide-spectrum individual work balance {sign}", work, stored + dissipated)
        balances.append(dict(sign=sign, work=work, energy=stored,
                             dissipation=dissipated, final_level=float(sum(q))))
    equal("S03: wide-spectrum difference from individual histories",
          abs(balances[0]["final_level"] - balances[1]["final_level"]), witness["gap"])
    witness["individual_balances"] = balances
    return cases


def observable_examples():
    # Глобальная секущая, потерянная при локальной проверке, и неограниченный обратный.
    xs=np.array([-1.,1.]); equal("O01: same nonlinear observations",np.diff(xs**2),[0])
    equal("O02: nonlinear target separates pair",np.diff(xs**3),[2])
    y_norm=[]; b_norm=[]
    for n in (10,100,1000):
        e=np.zeros(n);e[-1]=1.; y_norm.append(np.linalg.norm(e/np.arange(1,n+1)))
        b_norm.append(np.linalg.norm(e))
    equal("O03: observation norm tends to zero",y_norm,[.1,.01,.001])
    equal("O04: target norm stays one",b_norm,[1,1,1])


def kinetics():
    D = 4*np.log(2.)
    report, u, final = exact_pair()
    matrix = pulse_matrix(D, 4)
    equal("K01: exact historical zeros", response(epochs, matrix)@u, [0]*3)
    equal("K02: exact future difference", (response(np.log(4.), matrix)@u)[0], final, atol=1e-15)
    energies = []
    for sign in (1., -1.):
        pre = (1 + sign*u[::-1])/2
        stress = np.r_[pre, 1.]
        dts = np.r_[np.full(4, D/4), np.log(4.)]
        q, work, energy, diss = roll_kelvin(stress, dts)
        equal("K03: individual work balance "+str(sign), work, energy+diss)
        equal("K04: compression and dissipation "+str(sign),
              min(min(stress), 1-max(stress), diss) >= -1e-14, True)
        energies.append(dict(work=work,energy=energy,dissipation=diss,output=sum(q)))
    equal("K05: two full trajectories", energies[0]['output']-energies[1]['output'], final, atol=1e-15)
    # Управляемость для произвольной длительности и провал при совпадающих скоростях.
    b = rates*weights
    W = np.outer(b,b)*(-np.expm1(-np.add.outer(rates,rates)*D))/np.add.outer(rates,rates)
    equal("K06: input cost Gramian positive", np.min(np.linalg.eigvalsh(W)) > 0, True)
    equal("K07: identical rates collapse", np.linalg.matrix_rank(np.exp(-np.outer(epochs,[1.,1.]))),1)
    equal("K08: fourth distinct observation identifies four modes",
          np.linalg.matrix_rank(np.exp(-np.outer(np.r_[epochs,np.log(4.)],rates))),4)
    return report, energies


def integral_laws():
    # Абелев полугрупповой закон, резольвенты двух разных типов, сходимость SOE.
    a, b, t = .31,.46,1.7
    lhs = quad(lambda s:(t-s)**(a-1)*s**(b-1),0.,t)[0]/(gamma(a)*gamma(b))
    equal("V01: beta convolution", lhs,t**(a+b-1)/gamma(a+b),rtol=1e-8)
    beta, c = .7,.4
    def resolvent_series(time):
        return sum((-c)**n*time**(a*(n+1)-1)/gamma(a*(n+1)) for n in range(55))
    for p in (.1,1.,5.):
        R = c/(p+beta)**a
        equal("V02: tempered resolvent "+str(p),1/(1-R),1+c/((p+beta)**a-c))
        # Ряд проверяется ниже во времени; образ ядра получен независимой квадратурой.
        laplace = quad(lambda x:np.exp(-(p+beta)*x)*x**(a-1)*c/gamma(a),0,np.inf)[0]
        equal("V03: transformed Koltunov kernel "+str(p),laplace,R,rtol=2e-7)
    time=.7
    convolution=quad(lambda s:(time-s)**(a-1)*resolvent_series(s)/gamma(a),0,time)[0]
    equal("V04: fractional resolvent equation",resolvent_series(time)+c*convolution,
          time**(a-1)/gamma(a),atol=1e-8)
    nodes=np.linspace(-50,16,661);step=nodes[1]-nodes[0]
    mu=step*np.exp(.69*nodes)/gamma(.69);mu[[0,-1]]*=.5
    tau=np.geomspace(.01,100,51)
    error=float(np.max(abs((np.exp(-np.outer(tau,np.exp(nodes)))@mu)*tau**.69-1)))
    equal("V05: positive SOE on specified window",error,0,atol=2e-12)
    # Источниковая S-кривая для Вк; множитель δ удаляется в Φ''/Φ.
    t0=6.;nu=.1;beta=.3;rho=4.;f=expit(beta*t0-rho)
    ratio=nu*(nu-1)/t0**2+2*nu*beta*(1-f)/t0+beta**2*(1-f)*(1-2*f)
    phi=lambda t:t**nu*expit(beta*t-rho)
    h=.001
    equal("V06: source shape second derivative",ratio,
          (phi(t0+h)-2*phi(t0)+phi(t0-h))/(h*h*phi(t0)),rtol=2e-6)
    equal("V07: locally convex field curve",ratio>0,True)
    return dict(soe_max_relative_error=error,field_ratio_year_minus2=float(ratio))


def make_figures(bands, exact, energies):
    fig, ax = plt.subplots(1, 2, figsize=(11,4.6),layout="constrained")
    u=np.array([float(F(s)) for s in exact['controls']])
    D=4*np.log(2.);edges=np.linspace(-D,0,5)
    for sign,col,label in [(1,'#156381','История 1'),(-1,'#aa5325','История 2')]:
        load = (1+sign*u[::-1])/2
        ax[0].stairs(load,edges,baseline=None,color=col,label=label,linewidth=2)
        ax[0].vlines(edges[0],0.,load[0],color=col,lw=1.2)
        ax[0].vlines(0.,load[-1],1.,color=col,lw=1.2)
    ax[0].plot([0,1.6],[1,1],color='black',label='Общая нагрузка')
    ax[0].set(xlabel='Время t/T',ylabel='Сжимающее напряжение σ/P',title='Достижимые предыстории')
    ax[0].legend(fontsize=8,loc='lower right')
    times=np.linspace(0,3.8,450)
    c=np.array([float(F(x)) for x in exact['modal_differences']])
    vals=np.exp(-np.outer(times,rates))@c
    ax[1].plot(times,vals*1e6,color='#156381')
    ax[1].scatter(epochs,[0]*3,c='black',s=25,label='Эпохи наблюдения',zorder=3)
    ax[1].axvline(np.log(4.),color='#aa5325',ls='--',label='Проверочное будущее')
    ax[1].set(xlabel='Время t/T',ylabel='Разность оседаний, миллионные доли S',title='Разность двух точных решений')
    ax[1].axhline(0,color='gray',lw=.6);ax[1].legend(fontsize=8)
    fig.savefig(OUT/'REACHABLE_HISTORIES.png',dpi=155);plt.close(fig)
    fig, ax=plt.subplots(figsize=(8,4.8),layout='constrained')
    for eps,col in [(0.,'#156381'),(.001,'#aa5325'),(.01,'#447345')]:
        arr=[x for x in bands if x['D']==2 and x['eps']==eps]
        hs=[x['H'] for x in arr];lo=[x['lower'] for x in arr];hi=[x['upper'] for x in arr]
        ax.plot(hs,lo,'.-',color=col,label=f'ε/S = {eps:g}')
        ax.fill_between(hs,lo,hi,color=col,alpha=.25)
    ax.set(xlabel='Горизонт после последней эпохи H/T',ylabel='Диаметр / S',yscale='log',
           title='Допустимое расхождение при ограниченной нагрузке (D/T = 2)')
    ax.legend();ax.grid(which='both',alpha=.2)
    fig.savefig(OUT/'AMBIGUITY_BOUNDS.png',dpi=155);plt.close(fig)


def main():
    exact, energies = kinetics()
    bands=[forecast_band(D,H,eps) for D in [.25,2.,8.] for eps in [0.,.001,.01]
           for H in [.05,.15,.4,.75,1.,2.,4.]]
    for field in ['dual_gap','obs_violation','quadrature_error']:
        equal("B01: "+field,max(x[field] for x in bands),0,atol=2e-8)
    equal("B02: continuous upper covers feasible subclass",
          min(x['upper']-x['lower'] for x in bands)>=-1e-10,True)
    observable_examples()
    for H in [.15,.75,2.]:
        vals=[forecast_band(2.,H,e)['lower'] for e in [0.,.001,.01]]
        equal("B04: monotonicity in noise "+str(H),min(np.diff(vals))>=-1e-10,True)
    report=dict(scope="conditional mathematical experiments, no field fitting",python=platform.python_version(),
                numpy=np.__version__,scipy=scipy.__version__,sympy=sp.__version__,matplotlib=matplotlib.__version__,
                exact_pair=exact,individual_balances=energies,bands=bands,spectrum_cases=spectrum_test(),lab_Abel=abel_lab(),
                kernels=integral_laws(), tests=tests)
    make_figures(bands,exact,energies)
    (OUT/'RESULTS.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8', newline='\n')
    failures=[t['id'] for t in tests if t['status']!='PASS']
    print(f"{len(tests)-len(failures)}/{len(tests)}. FAIL {failures}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
