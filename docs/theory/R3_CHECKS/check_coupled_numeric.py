"""Сверка (S8) полным равновесием в исходных координатах и рисунок для ВКР."""
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import scipy
from scipy.integrate import solve_ivp

OUT = Path(__file__).resolve().parent


def integrate_history(sign, rates, contrast, tolerance):
    K = np.array([[2., 1.], [1., 2.]])
    O = np.eye(4) - np.ones((4, 4))/2
    V0 = np.array([[1., 1., 1., 1.], [1., -1., 2., -2.]])
    C = K @ V0 @ O.T
    D = O @ np.diag(rates) @ O.T + C.T @ np.linalg.solve(K, C)
    # Сначала четыре последовательные ступени подготовки, затем три интервала общего входа.
    histories = (1 + sign*contrast[::-1])/2
    levels = np.r_[histories, np.ones(3)]
    durations = np.r_[np.full(4, np.log(2)), np.log(4/3), np.log(3/2), np.log(2)]
    x = np.zeros(6)                  # 4 координаты z, работа и диссипация
    force_old = np.zeros(2)
    observed = []
    balance_errors = []
    for index, (level, length) in enumerate(zip(levels, durations)):
        force = np.array([level, 0.])
        q_before = np.linalg.solve(K, force_old + C @ x[:4])
        q_start = np.linalg.solve(K, force + C @ x[:4])
        # Скачок общего внешнего усилия. Память непрерывна, порт имеет упругий скачок.
        x[4] += ((force_old + force)/2) @ (q_start - q_before)
        if index == 4:
            observed.append(q_start.copy())

        def rhs(time, state):
            memory = state[:4]
            q = np.linalg.solve(K, force + C @ memory)
            dz = C.T @ q - D @ memory
            dq = np.linalg.solve(K, C @ dz)
            return np.r_[dz, force @ dq, dz @ dz]

        sol = solve_ivp(rhs, (0., length), x, method="DOP853",
                        rtol=tolerance, atol=tolerance*0.01)
        if not sol.success:
            raise RuntimeError(sol.message)
        x = sol.y[:, -1]
        q = np.linalg.solve(K, force + C @ x[:4])
        energy = q @ K @ q / 2 - q @ C @ x[:4] + x[:4] @ D @ x[:4] / 2
        balance_errors.append(abs(x[4]-energy-x[5]))
        if index >= 4:
            observed.append(q.copy())
        force_old = force
    return np.array(observed), {"work":float(x[4]),"stored":float(energy),
                                "dissipated":float(x[5]),"max_balance_residual":float(max(balance_errors))}


def make_figure(reference):
    case = reference["separable"]
    contrast = np.asarray(case["loading_difference_young_to_old"])
    rates = np.array([1., 4., 16., 64.])
    edges = np.array([1., .98, .88, .5, .01])
    moments = (edges[None,:-1]**rates[:,None] - edges[None,1:]**rates[:,None])/4
    times = np.linspace(0, np.log(2.5), 430)
    historic = -np.log([1., .98, .96])
    scale_mm = 1000*(225/32)*.006
    contrast_mm = scale_mm * (np.exp(-np.outer(times, rates)) @ (moments @ contrast))
    pos = np.linspace(-10, 10, 160)
    fig, ax = plt.subplots(1, 2, figsize=(9.0, 3.6), layout="constrained")
    ax[0].plot(pos,(1-(pos/10)**2)*contrast_mm[-1],color="#19635c",lw=2.3,label="Будущая эпоха")
    ax[0].plot(pos,np.zeros_like(pos),color="#393d46",ls="--",label="Три прошлые эпохи")
    ax[0].set(xlabel="Координата репера x, м",ylabel="Разность оседаний, мм",title="Поперечный профиль")
    ax[0].legend(loc="lower center",bbox_to_anchor=(.5,.12),fontsize=8)
    ax[1].plot(times,contrast_mm,lw=2.3,color="#19635c")
    ax[1].scatter(historic,np.zeros(3),s=32,marker="o",facecolor="white",edgecolor="#222832",
                  zorder=4,label="Наблюдения")
    ax[1].scatter([times[-1]],[contrast_mm[-1]],s=35,color="#a34320",zorder=4,label="Будущая эпоха")
    ax[1].set(xlabel="Время после подготовки t/T",ylabel="Разность оседаний, мм",title="Центральный репер")
    ax[1].legend(loc="lower right",fontsize=8)
    for axis in ax:
        axis.grid(axis="y",alpha=.22)
        axis.spines[["top","right"]].set_visible(False)
    fig.savefig(OUT/"R3_SPATIAL_PROFILE.png",dpi=190,metadata={"Software":"AMHF R3 research"})
    plt.close(fig)


def main():
    from fractions import Fraction
    ref=json.loads((OUT/"R3_RESULTS.json").read_text("utf-8"))
    pair=ref["coupled"]
    u=np.array([float(Fraction(v)) for v in pair["compressed_load_contrast"]])
    expected=np.array([0,0,0,float(Fraction(pair["future_difference"]))])
    expected_second=np.array([float(Fraction(a)) for a in pair["second_channel"]])
    outcomes={}
    for tol in [1e-8,1e-12]:
        plus,ap=integrate_history(1, [1,2,3,4],u,tol)
        minus,am=integrate_history(-1, [1,2,3,4],u,tol)
        difference=plus-minus
        err=max(abs(difference[:,0]-expected).max(),abs(difference[:3,1]-expected_second).max())
        energy_error=max(ap["max_balance_residual"],am["max_balance_residual"])
        scale=max(1.,np.abs(plus).max(),np.abs(minus).max(),abs(ap["work"]),abs(am["work"]))
        # Численный критерий данного нормированного опыта, не инженерный допуск рудника.
        check_limit=100*max(tol*scale, np.finfo(float).eps*scale)
        if err>check_limit or energy_error>check_limit or min(ap["dissipated"],am["dissipated"])<0:
            raise RuntimeError("Ошибка прямого воспроизведения или энергетики")
        outcomes[str(tol)]={"relative_tolerance":tol,"absolute_tolerance":tol*.01,
                            "acceptance_limit":float(check_limit),"port_error":float(err),
                            "max_energy_error":float(energy_error),"positive_history":ap,"negative_history":am}
    print("PASS",outcomes["1e-12"]["port_error"],outcomes["1e-12"]["max_energy_error"])
    report={"method":"Full K equilibrium at each DOP853 stage, normalized system (S8)",
            "numpy":np.__version__,"scipy":scipy.__version__,"matplotlib":matplotlib.__version__,
            "levels":outcomes}
    (OUT/"R3_NUMERIC_RESULTS.json").write_bytes((json.dumps(report,indent=2)+"\n").encode("utf8"))
    make_figure(ref)


if __name__=="__main__":
    main()
