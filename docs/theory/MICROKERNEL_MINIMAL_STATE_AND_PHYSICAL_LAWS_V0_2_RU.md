# Минимально достаточное состояние microkernel representation

**Проект:** VKM / SKRU-1  
**Версия:** 0.2  
**Статус:** reduced-model specification  
**Заменяет:** `MICROKERNEL_MINIMAL_STATE_AND_PHYSICAL_LAWS_V0_1_RU.md`

---

# 0. Главная поправка

В v0.2 минимальное состояние относится не к «фундаментальному микроядру природы», а к microkernel внутри конкретного `WorldRepresentation`.

Full PhysicalWorld может иметь намного больше degrees of freedom.

---

# 1. Полное и редуцированное состояние

Для PhysicalWorld:

\[
X(\mathbf r,t)
\]

Для microkernel representation:

\[
x_i(t)
=
\mathcal R_i[X|_{\Omega_i}]
\]

где \(\mathcal R_i\) — reduction mapping.

---

# 2. Определение достаточности

Reduced-state \(x_i\) достаточен для:

\[
(\mathcal T,H,\varepsilon)
\]

если любые два full states, mapped в одинаковое \(x_i\), дают практически одинаковый required external response при одинаковых future inputs.

\[
\mathcal R(X_a)=\mathcal R(X_b)
\]

должно влечь:

\[
d_Y(
Y_a,
Y_b
)
\le
\varepsilon
\]

на горизонте \(H\).

---

# 3. Минимальность

State минимален относительно выбранной representation, если удаление существенной компоненты нарушает closure/response criterion.

Не существует абсолютного «минимального состояния ВКМ».

---

# 4. Task dependence

\[
x_i^{min}
=
x_i^{min}
(
\mathcal T,H,\varepsilon,\mathcal H
)
\]

Surface forecast, stability analysis и GPR forward model могут требовать разные reduced states.

---

# 5. State vs parameters vs geometry

Разделять:

## State

эволюционирует.

## Internal memory

хранит history dependence.

## Parameters

задают law.

## Geometry

задаёт spatial domain.

## Derived quantities

вычисляются.

---

# 6. Общий reduced object

\[
K_i=
\{
G_i,
\Theta_i,
x_i,
m_i,
P_i,
B_i,
U_i
\}
\]

где:

- \(G_i\) — representation geometry;
- \(\Theta_i\) — parameters/laws;
- \(x_i\) — dynamic reduced state;
- \(m_i\) — memory/internal variables;
- \(P_i\) — ports;
- \(B_i\) — local event history;
- \(U_i\) — uncertainty/provenance.

---

# 7. Generalized coordinates

Не хранить всё FE field, если достаточно modal/generalized variables:

\[
\mathbf u(\mathbf r,t)
\approx
\sum_a
q_a(t)\psi_a(\mathbf r)
\]

\(q_a\) могут описывать:

- vertical convergence;
- shear;
- average strain;
- bending/tilt;
- volume change.

---

# 8. Quasistatic reduction

Для long-term subsidence:

\[
\rho\ddot u\approx0
\]

поэтому velocity/momentum могут отсутствовать в minimal state.

Для seismic/dynamic representation — должны вернуться.

---

# 9. History dependence

Критическая поправка сохраняется:

\[
(\sigma(t),\varepsilon(t))
\]

не обязаны определять будущее creep material.

History information входит либо:

```text
full history
```

либо:

```text
internal memory variables
```

---

# 10. Markov closure

Ищем state, для которого:

\[
x(t+\Delta t)
=
F(
x(t),
u_{[t,t+\Delta t]}
)
+
O(\varepsilon)
\]

без обращения ко всей pre-\(t\) history.

Если невозможно — state incomplete или reduced dynamics inherently non-Markovian.

---

# 11. Mori–Zwanzig warning

Даже если fine system Markovian, reduction может породить:

- memory;
- noise;
- effective forcing.

Поэтому coarse memory kernel не следует автоматически интерпретировать как fundamental material law.

Это важный research caveat v0.2.

---

# 12. Memory mechanisms — не смешивать

Рабочие альтернативы:

1. spectrum of unresolved linear modes;
2. nonlinear local relaxation;
3. primary creep / explicit constitutive memory.

Они могут давать похожие temporal tails.

Mechanism identification — отдельная research problem.

---

# 13. Mechanical state candidate

Для mechanical solid kernel:

\[
x_M=
\{
q,
\eta_{cr},
\varepsilon^p,
D,
\kappa,\ldots
\}
\]

только активные variables.

---

# 14. Creep memory

Possible representations:

```text
creep strain
Prony modes
internal hereditary variables
fractional-memory state approximation
```

Выбор зависит от law/representation.

---

# 15. Damage

\[
0\le D\le1
\]

только если конкретная damage model использует scalar variable.

Не навязывать \(D\) каждому миру.

---

# 16. Plastic/internal variables

\[
\varepsilon^p,\kappa
\]

добавляются только в corresponding physical hypothesis.

---

# 17. HM state

Possible reduced state:

\[
x_{HM}
=
\{
q,
m,
D,
p,
S,
m_f,
\phi,
k
\}
\]

Но если \(k,\phi\) fixed, они parameters.

---

# 18. Effective stress

Если используется соответствующая model:

\[
\sigma'
=
\sigma-\alpha pI
\]

Это law-specific relation, не универсальный axiom всех worlds.

---

# 19. Hydraulic conservation

Reduced model должен сохранять mass balance в пределах declared tolerance.

---

# 20. Thermal state

\(T\) dynamic только если thermal evolution relevant.

Иначе:

```text
prescribed field
```

или parameter.

---

# 21. Geometry as evolving state

Некоторые geometric descriptors становятся state:

- effective pillar width;
- fracture aperture;
- contact area;
- void volume;
- backfill compaction.

Не вся CAD geometry входит в state vector.

---

# 22. Contact/interface state

Possible:

\[
x_C=
\{
\delta_n,\delta_s,p_c,a,D_c
\}
\]

если interface representation активен.

---

# 23. Void representation

Dry void:

```text
volume
boundary geometry
```

Fluid-filled:

```text
+ fluid pressure
+ fluid mass
```

---

# 24. Backfill representation

Possible:

```text
compaction
maturity
damage
saturation
pressure
```

Возраст может быть derived:

\[
a=t-t_{\mathrm{fill}}
\]

---

# 25. State-dependent parameters

Пример:

\[
E_{\mathrm{eff}}(D,S,T)
\]

не хранить одновременно как independent parameter и state без provenance/relation.

---

# 26. DAE structure

Long-term reduced mechanics может естественно быть:

\[
0=g(q,z,u)
\]

\[
\dot z=f(q,z,u)
\]

где \(q\) quasistatic, \(z\) internal evolving state.

---

# 27. Port closure

Minimal state должен позволять вычислять required boundary variables.

Если нельзя восстановить port response из state — state insufficient.

---

# 28. Observation closure

Если representation должна produce a modality:

\[
y=H(x,\theta)
\]

то required properties должны быть derivable из state+parameters.

---

# 29. State closure test

Практический тест:

1. найти разные fine states \(X_a,X_b\);
2. добиться:
   \[
   \mathcal R(X_a)=\mathcal R(X_b)
   \]
3. приложить одинаковые future inputs;
4. сравнить output trajectories.

Если outputs diverge above tolerance:

```text
STATE_INSUFFICIENT
```

---

# 30. Representation error vs world uncertainty

Если state reduction ошибочен — это representation error.

Если неизвестен реальный law/geometry — world uncertainty.

Не смешивать.

---

# 31. Latent regime

\[
Z
\]

не входит автоматически в fundamental physical state.

Это inferred reduced state:

\[
Z=\Psi(x,y_{0:t})
\]

---

# 32. World-0 reduced state candidate

Для future mesomodel:

\[
v_i(t)
\]

— local convergence,

\[
z_i(t)
\]

— internal variables.

Potential model:

\[
\sigma_i=
\sigma_i^0-\sum_jK_{ij}v_j
\]

\[
\dot v_i=
h_iA_i\phi(
\sigma_i-P_i(v_i,z_i)
)
\]

Но это research representation, не universal microkernel law.

---

# 33. Knothe/Norton research candidate

Одноэлементный reduced case может иметь analytically derived response.

Он должен храниться как `RESEARCH_CANDIDATE`, пока assumptions и derivation не прошли audit.

Не заменять им constitutive model automatically.

---

# 34. Effective memory research candidate

Если coarse relaxation spectrum меняется с degradation/backfill state, effective temporal response может меняться.

Это candidate explanation, не FACT.

---

# 35. Adaptive state dimension

Representation architecture может поддерживать:

```text
STATE_ENRICHMENT
STATE_REDUCTION
```

но первая implementation может иметь fixed schemas.

---

# 36. State enrichment trigger

Кандидаты:

- high reduction error;
- new coupling;
- regime change;
- systematic residual;
- new modality requirement.

---

# 37. Time-scale reduction

Для:

\[
\tau_f\ll\tau_s
\]

fast state можно approximate quasi-steady, если error validated.

---

# 38. Energy consistency

Если representation допускает energy formulation:

\[
\dot E
=
P_{\mathrm{ports}}
+
P_{\mathrm{sources}}
-
D
\]

с:

\[
D\ge0
\]

можно использовать как validation check.

Но не объявлять universal для evolving damage без thermodynamic derivation.

---

# 39. Stability caveat

Tangent matrix positive definiteness может быть useful local static stability criterion для конкретной reduced model.

Не переносить автоматически на full history-dependent world.

---

# 40. Physical admissibility

Candidate reduced state должен удовлетворять:

- variable bounds;
- law validity;
- conservation;
- geometry feasibility;
- applicable thermodynamic restrictions.

---

# 41. Minimal common envelope

```yaml
microkernel:
  kernel_id:
  representation_id:
  physical_entity_refs:
  geometry_ref:

  state_schema:
  state:

  active_laws:
  parameter_set_refs:
  memory_schema:

  ports:
  uncertainty:
  provenance:

  task:
  horizon:
  tolerance:
```

Ключевая новая ссылка:

```text
representation_id
```

---

# 42. Physical entity mapping

Microkernel может представлять:

```text
one physical entity
many physical entities
part of one physical entity
```

Нужна relation:

```text
REPRESENTS
```

---

# 43. State versioning

Если representation changes:

```text
state_schema_version
```

must change.

Не переписывать state silently.

---

# 44. Validation metrics

State/reduction validation может включать:

- port error;
- field summary error;
- observation Mahalanobis error;
- forecast error;
- conservation residual.

---

# 45. Research quantity D_obs

Для observation-aware state validation можно использовать candidate:

\[
D_M^2=
\Delta y^T R^{-1}\Delta y
\]

с полной covariance.

Порог и statistical interpretation — отдельная research задача.

---

# 46. Не принимать как универсальные state variables

Не делать обязательными для каждого kernel:

```text
damage
pressure
temperature
saturation
chemistry
```

Они появляются только при активной physics family.

---

# 47. v0.2 implementation minimum

На будущей software стороне сначала достаточно schema contract без solver:

```text
state_schema metadata
parameter references
physical entity mapping
representation mapping
ports
provenance
```

Не требуется сейчас вычислять эти states в Corpus Platform.

---

# 48. Статус

```text
Task-dependent state                     ACCEPTED_DRAFT
State closure criterion                  ACCEPTED_DRAFT
History/internal variables               ACCEPTED_DRAFT
World vs representation separation       ACCEPTED_DRAFT
Mori-Zwanzig caveat                      ACCEPTED_RESEARCH_CONTEXT
Adaptive state dimension                 CANDIDATE
Exact states for SKRU1 worlds            UNKNOWN
Formal closure proof                     REQUIRED_LATER
```

---

# 49. Короткое определение

> **Минимально достаточное состояние microkernel — наименьшее reduced representation состояния выбранного PhysicalWorld, которое сохраняет требуемую будущую port/observation response для конкретной задачи, горизонта и допуска.**
