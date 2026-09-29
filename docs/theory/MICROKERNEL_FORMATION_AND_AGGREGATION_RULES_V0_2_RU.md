# Правила образования, разделения и агрегации микроядер как редуцированного представления

**Проект:** VKM / SKRU-1  
**Версия:** 0.2  
**Статус:** specification of WorldRepresentation  
**Заменяет:** `MICROKERNEL_FORMATION_AND_AGGREGATION_RULES_V0_1_RU.md`

---

# 0. Главная поправка v0.2

Microkernel больше не трактуется как фундаментальная локальная сущность PhysicalWorld.

> **Microkernel — единица редуцированного WorldRepresentation.**

Реальный объект содержит:

```text
породу
слои
камеры
целики
закладку
нарушения
флюиды
```

а microkernel partition отвечает на другой вопрос:

> какие внутренние степени свободы можно скрыть для данной задачи, горизонта и допуска?

---

# 1. Формальное место partition

Пусть:

\[
w\in\mathcal W
\]

— полностью доопределённый PhysicalWorld.

Representation:

\[
\rho_{\mathcal T,H,\varepsilon}(w)
\]

может содержать partition:

\[
\mathcal P=
\{K_1,\ldots,K_N\}
\]

где \(K_i\) — microkernel.

Следовательно:

\[
\mathcal P
\notin
\text{physical truth of }w
\]

а является `REPRESENTATION_CHOICE`.

---

# 2. Определение microkernel

> **Microkernel — локальная редуцированная подсистема представления мира, для которой выбранный reduced-state достаточно описывает будущий внешний отклик через конечный набор физических интерфейсов в пределах заданного допуска.**

---

# 3. Что может породить initial partition

Горные/геологические объекты дают candidate boundaries:

- chamber;
- pillar;
- roof;
- floor;
- interbed;
- backfill;
- fault zone;
- hydraulic compartment;
- water-protective strata;
- mining block;
- panel.

Но ни один из этих labels не означает автоматически `one object = one kernel`.

---

# 4. Microkernel != mining entity

Один pillar:

```text
fine representation:
core + edge zones + contacts
```

или:

```text
coarse representation:
1 pillar kernel
```

или вообще:

```text
effective mined-layer representation:
pillar hidden inside equivalent cell
```

PhysicalWorld остаётся тем же.

---

# 5. Microkernel != finite element

Finite element — discretization.

Microkernel — model reduction unit.

Возможны:

```text
1 microkernel → many FE
many microkernels → 1 surrogate node
```

---

# 6. Task-dependent partition

Partition зависит от:

\[
\mathcal P
=
\mathcal P(
\mathcal T,
H,
\varepsilon,
\mathcal H
)
\]

где:

- \(\mathcal T\) — задача;
- \(H\) — горизонт;
- \(\varepsilon\) — tolerance;
- \(\mathcal H\) — observations of interest.

Это ещё раз подтверждает, что partition не является физической истиной.

---

# 7. Физические порты

Microkernel взаимодействует через typed ports.

## Mechanical

\[
(\mathbf u,\mathbf t)
\]

\[
\mathbf t=\boldsymbol\sigma\mathbf n
\]

## Hydraulic

\[
(p,q_n)
\]

## Thermal

\[
(T,q_T)
\]

## Contact

```text
opening
slip
contact traction
hydraulic aperture
```

---

# 8. Port semantics

Порт является интерфейсом representation, а не новым физическим законом.

Внешняя область должна получать достаточную boundary information.

---

# 9. Hard split conditions

Candidate region должна разделяться, если coarse representation не может корректно скрыть внутреннюю структуру.

Сильные triggers:

1. different phases/materials;
2. incompatible constitutive laws;
3. physical discontinuity;
4. different event history;
5. different regime;
6. strong gradients;
7. topology/coupling change;
8. task/observation requires distinction.

---

# 10. Material/phase split

Примеры:

```text
solid | void
salt | backfill
intact | strongly damaged zone
permeable | sealing layer
```

---

# 11. Constitutive split

Если две области требуют разных law families:

```text
creep solid
vs
void

elastic roof
vs
viscoplastic pillar
```

aggregation без homogenization invalid.

---

# 12. History split

Разная история может сделать одинаковые текущие параметры недостаточными:

- extraction epoch;
- loading path;
- backfill age;
- wetting;
- damage history.

---

# 13. State-gradient split

Для key state \(x\):

\[
H_X=
\max_{\mathbf r\in\Omega}
\|
W_X(X-\bar X)
\|
\]

если \(H_X\) превышает task-dependent threshold, coarse kernel надо проверить/refine.

Это trigger, не универсальный физический порог.

---

# 14. Coupling/topology split

Новый crack может:

```text
создать hydraulic edge
ослабить mechanical contact
```

Representation graph должен позволять topology update.

---

# 15. Observation-aware split

Если две области должны различаться конкретной observation system, merge может быть запрещён даже при похожей механике.

Но observation requirement относится к representation purpose, а не к physical ontology.

---

# 16. External equivalence — главный критерий merge

Пусть:

\[
\rho_f(w)
\]

— fine representation,

\[
\rho_c(w)
\]

— candidate coarse representation.

Для task outputs \(Y\):

\[
E_{\mathrm{ext}}
=
d_Y(
Y_f,
Y_c
)
\]

Merge допустим, если:

\[
E_{\mathrm{ext}}
\le
\varepsilon_{\mathcal T}.
\]

---

# 17. External equivalence по портам

Можно сравнивать:

- boundary displacement;
- traction;
- fluid flux;
- pressure;
- energy exchange;
- contact response.

---

# 18. External equivalence по observations

\[
E_{\mathrm{obs}}^{(m)}
=
\sup_t
\|
H_m[X_f(t)]
-
H_m[X_c(t)]
\|_{R_m^{-1}}
\]

Это особенно важно, если representation предназначен для прогнозирования наблюдаемого оседания.

---

# 19. Не путать две агрегации

v0.2 разделяет:

## Dynamic aggregation

Можно ли coarse model воспроизвести физическую динамику.

## Observational aggregation

Можно ли differences fine-scale вообще увидеть данной observation system.

Они не эквивалентны.

---

# 20. Practical observability

Поверхность может сильно фильтровать fine spatial modes.

Это даёт:

```text
dynamically important
but
practically unobservable from surface
```

Такой fine structure иногда можно скрыть для surface forecasting, но нельзя автоматически скрыть для damage/stability analysis.

---

# 21. Conservation requirement

При merge должны сохраняться соответствующие balances.

Для additive conserved quantity:

\[
Q_C=
\sum_{i\in C}Q_i
\]

Internal exchanges должны взаимно сокращаться, если representation сохраняет conservation form.

Это известный принцип, не новая теорема проекта.

---

# 22. Energy/dissipation consistency

Для dissipative representation желательно:

\[
\dot{\mathcal E}
=
P_{\mathrm{boundary}}
+
P_{\mathrm{sources}}
-
\mathcal D,
\qquad
\mathcal D\ge0
\]

где применимо.

Это validation criterion reduced model.

---

# 23. State coherence

Для candidate merge:

\[
H_X(S)<\varepsilon_X
\]

может использоваться как heuristic.

Но low state variance сама по себе недостаточна.

Главный тест — external response.

---

# 24. Parameter coherence

\[
H_\Theta(S)
=
\max d_\Theta(\Theta_i,\Theta_j)
\]

— ещё один heuristic.

Разные parameters могут быть homogenizable; одинаковые parameters могут давать разное поведение из-за history.

---

# 25. Time-scale coherence

Если:

\[
\tau_i
\]

сильно различаются, coarse Markovian representation может породить apparent memory.

Поэтому merge должен учитывать spectrum relaxation times.

---

# 26. Memory induced by reduction

При устранении внутренних degrees of freedom coarse dynamics может стать history-dependent.

Это не считать дефектом автоматически.

Нужно различать:

```text
true constitutive memory
effective memory from unresolved modes
nonlinear relaxation
primary creep
```

Это будущий research question.

---

# 27. Dynamic split/merge

Partition:

\[
\mathcal P(t)
\]

может изменяться.

### Split triggers

- localization;
- new fracture;
- hydraulic breakthrough;
- different regime;
- excessive reduction error;
- new observation requirement.

### Merge triggers

- internal modes become irrelevant;
- external equivalence restored;
- system returns to homogeneous regime.

---

# 28. Hysteresis

Чтобы избежать oscillatory repartitioning, split и merge thresholds не должны совпадать механически.

Но конкретная policy — implementation detail будущего representation.

---

# 29. Chamber

Chamber — physical/geometric entity.

В representation она может быть:

```text
VOID_KERNEL
FLUID_KERNEL
BACKFILL_KERNEL
```

или hidden inside effective cell.

---

# 30. Pillar

Pillar — physical entity.

Representations:

```text
1 kernel
```

или:

```text
core + damaged edges + contacts
```

Выбор зависит от task/tolerance.

---

# 31. Chamber + pillar

На fine scale это разные физические domains.

На coarse scale они могут быть represented одним:

```text
EFFECTIVE_MINED_LAYER_KERNEL
```

только после external-equivalence test.

---

# 32. Block

Mining block — хороший aggregation candidate, но не automatic kernel.

Merge более вероятен при:

- similar geometry;
- similar extraction history;
- same backfill regime;
- no active anomaly;
- close physical state;
- acceptable external error.

---

# 33. Panel

Panel — high-level coarse representation.

Для detailed mechanics почти всегда содержит many kernels.

---

# 34. Backfill

Backfill обычно separate physical domain из-за:

- own law;
- age;
- compaction;
- evolving modulus/strength;
- saturation;
- imperfect contacts.

---

# 35. Fault/fracture

Representation options:

```text
VOLUME_KERNEL
INTERFACE_EDGE
```

зависят от thickness/scale/task.

---

# 36. Multiplex representation graph

Один spatial partition может иметь разные coupling layers:

```text
mechanical
hydraulic
thermal
contact
```

Не нужно сводить их к одному scalar edge weight.

---

# 37. Observation entities

Не microkernels:

- benchmark;
- GNSS receiver;
- SAR pixel;
- leveling line;
- seismic station;
- GPR antenna.

Они принадлежат ObservationWorld.

---

# 38. Hierarchical representations

Допустима иерархия:

```text
fine material domains
↓
pillar/chamber kernels
↓
block kernels
↓
panel kernels
```

Каждый уровень — representation layer.

---

# 39. Fine-to-coarse mapping

Для каждого aggregate хранить:

```text
parent_representation
child_kernels
reduction_method
tolerance
horizon
task
validation_run
provenance
```

---

# 40. Initial partition

Строится из evidence-backed geometry:

- material boundaries;
- openings;
- pillars;
- blocks;
- backfill;
- known structures;
- hydro boundaries.

Статус:

```text
EVIDENCE_CONSTRAINED_INITIAL_REPRESENTATION
```

---

# 41. Solver-driven refinement

После fine simulation анализировать:

\[
\nabla\sigma,
\nabla\varepsilon,
\nabla D,
\nabla p
\]

а также flux, sensitivity, local instability indicators.

Но refinement algorithm не является частью PhysicalWorld.

---

# 42. Observation-driven refinement

Persistent residual:

\[
r=y^{obs}-y^{model}
\]

может быть trigger.

Но residual не доказывает, что причина именно coarse partition.

Нужно различать:

```text
wrong geometry
wrong parameters
wrong physics
wrong observation operator
insufficient representation resolution
```

---

# 43. Uncertainty-driven representations

Если geometry uncertain, создаются разные PhysicalWorld geometry realizations.

Не надо маскировать physical uncertainty refinement одного representation.

---

# 44. World vs representation uncertainty

Это принципиально разные uncertainty sources:

```text
WORLD UNCERTAINTY:
какая физика/геометрия реальна?

REPRESENTATION ERROR:
насколько точно мы посчитали выбранный мир?
```

Их нельзя смешивать в одной error bar.

---

# 45. Microkernel state

Каждый kernel хранит reduced state, достаточный для выбранной representation.

State definition находится в отдельном документе v0.2.

---

# 46. Graph node for GNN

Если позже используется GNN:

```text
node = microkernel representation
```

а не «реальная физическая частица».

Это сохраняет честную интерпретацию surrogate.

---

# 47. Acceptance test representation

Representation valid для task, если:

1. stable numerical solution;
2. conservation acceptable;
3. port response acceptable;
4. observation response acceptable;
5. errors below declared tolerance;
6. validity domain documented.

---

# 48. World-0 relation

Будущий World-0 `пласт→толща→поверхность` является одним reduced representation class.

Microkernels там естественно correspond to mined-layer elements.

Но это не означает, что все будущие worlds обязаны использовать такой partition.

---

# 49. Research candidate: sign structure

Для конкретного World-0 influence matrix \(K\) sign pattern необходимо получить high-fidelity check.

Не принимать cooperative assumptions заранее.

---

# 50. Research candidate: family observability

Partition size может оптимизироваться не только по field error, но и по family discrimination objective.

Это пока candidate.

---

# 51. Статус v0.2

```text
Microkernel as representation                  ACCEPTED_DRAFT
Task-dependent partition                       ACCEPTED_DRAFT
Ports                                          ACCEPTED_DRAFT
Hard split rules                               ACCEPTED_DRAFT
External equivalence                           ACCEPTED_DRAFT
Dynamic vs observational aggregation separation ACCEPTED_DRAFT
Dynamic split/merge                            CANDIDATE
Exact thresholds                               EXPERIMENT_REQUIRED
SKRU1 final partition                          UNKNOWN
```

---

# 52. Короткое правило

> **Microkernel нужен там, где внутреннюю физику можно скрыть, сохранив необходимое внешнее поведение выбранного PhysicalWorld для конкретной задачи.**

Если скрытие изменяет required response больше tolerance — `SPLIT`.

Если нет — `MERGE` допустим.
