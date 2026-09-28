# Связанный многомасштабный framework физических миров и полево-ядерных представлений ВКМ / СКРУ-1

**Рабочее название:** Coupled Multiscale Physical-World / Field–Kernel Framework  
**Версия:** 0.2  
**Статус:** рабочая исследовательская спецификация; не доказанная новая теория  
**Заменяет:** `COUPLED_FIELD_KERNEL_FRAMEWORK_V0_1_RU.md`

---

# 0. Что изменено относительно v0.1

Версия 0.2 исправляет четыре принципиальных смешения v0.1:

1. **реальный СКРУ-1 ≠ PhysicalWorld ≠ WorldRepresentation ≠ ObservationWorld;**
2. **microkernel — единица редуцированного представления, а не фундаментальная сущность реального мира;**
3. **physical hypothesis, representation choice, forecast method и epistemic rule — разные классы выбора;**
4. известные математические результаты больше не маркируются как потенциально «наши теоремы».

Исследовательские утверждения о World-0, \(D_{\mathrm{obs}}\), монотонности, временной функции, low-pass свойствах поверхности и дизайне наблюдательной сети сохраняются только как `RESEARCH_CANDIDATE`.

---

# 1. Эпистемический статус

Этот документ не утверждает существование новой фундаментальной физики оседаний.

В нём разделяются:

- **KNOWN_PHYSICS** — известные физические законы;
- **KNOWN_MATHEMATICS** — известные математические результаты;
- **FRAMEWORK_CHOICE** — архитектура исследования;
- **PHYSICAL_HYPOTHESIS** — физическая гипотеза конкретного мира;
- **REPRESENTATION_CHOICE** — способ численного/редуцированного представления;
- **FORECAST_METHOD** — алгоритм, конкурирующий за прогноз;
- **EPISTEMIC_RULE** — правило допуска, переноса, валидации;
- **RESEARCH_CANDIDATE** — проверяемое утверждение, ещё не принятое как результат;
- **UNKNOWN** — неизвестное.

Главная дисциплина проекта:

> Нельзя превращать удобную вычислительную конструкцию в утверждение о природе только потому, что она хорошо работает.

---

# 2. Пять различных объектов

## 2.1 Реальный СКРУ-1

Реальный СКРУ-1 существует один раз.

Обозначим:

\[
\mathcal R_{\mathrm{SKRU1}}
\]

Его полное внутреннее состояние недоступно.

Мы видим только ограниченные измерения.

---

## 2.2 WorldSpec

WorldSpec хранит доступное знание:

```text
FACT
RANGE
DISCRETE_SET
UNKNOWN
MODEL_CHOICE
ANALOGUE
TRANSFER
```

WorldSpec не является физическим миром.

Это **неполная evidence-constrained спецификация пространства допустимых гипотез**.

---

## 2.3 PhysicalWorld

> **PhysicalWorld — полностью доопределённая и исполнимая физическая гипотеза о том, каким мог бы быть объект.**

Обозначение:

\[
w\in\mathcal W
\]

Для мира \(w\) определены:

- геометрическая реализация;
- геологическая реализация;
- material fields;
- constitutive laws;
- parameter sets;
- initial state;
- boundary conditions;
- mining history;
- backfill history;
- physical couplings;
- все discrete choices;
- все UNKNOWN, необходимые для вычисления;
- provenance каждого доопределения.

PhysicalWorld не должен содержать неразрешённый UNKNOWN, который блокирует solver execution.

---

# 3. Пространство миров

WorldSpec порождает:

\[
\mathcal W_{\mathrm{admissible}}
\]

— множество физически и эпистемически допустимых миров.

Важно:

\[
\mathcal R_{\mathrm{SKRU1}}
\not\equiv
w_i
\]

Мы не утверждаем, что конкретный generated world является СКРУ-1.

Рабочее допущение ансамблевого подхода:

> пространство миров должно быть достаточно богатым, чтобы реальные наблюдения не противоречили всем его допустимым представителям.

Это допущение должно проверяться.

---

# 4. Family of worlds

Семья:

\[
\mathcal W_f\subset\mathcal W
\]

объединяет миры с одной физической гипотезой/семейством законов и различающимися допустимыми параметрами/геометрией/историей.

Пример:

```text
creep family
damage family
backfill-coupled family
structural family
```

Имена PW-A…PW-L могут сохраниться как удобные labels, но после ревизии типов.

---

# 5. Четыре класса осей

Нельзя смешивать их в одном наборе world axes.

## 5.1 PHYSICAL_HYPOTHESIS

Различает PhysicalWorld.

Например:

- конкретная constitutive family;
- damage law;
- pillar mechanics;
- backfill mechanics;
- structural/fault hypothesis;
- hydro coupling;
- initial stress hypothesis.

---

## 5.2 REPRESENTATION_CHOICE

Не делает новый физический мир.

Примеры:

- fine OGS FEM;
- ANSYS;
- reduced mesomodel;
- microkernel graph;
- effective layer;
- surrogate;
- mesh resolution.

Один PhysicalWorld может иметь несколько representations.

---

## 5.3 FORECAST_METHOD

Метод пытается прогнозировать trajectories PhysicalWorld.

Примеры:

- нормативная функция времени;
- Knothe-like method;
- variable modulus method;
- IMM;
- ML predictor;
- regression;
- neural model.

Forecast method не является truth generator только потому, что содержит физические параметры.

---

## 5.4 EPISTEMIC_RULE

Примеры:

- LAB→MASSIF transfer;
- ANALOGUE→SKRU1 transfer;
- provenance;
- calibration restrictions;
- validation independence;
- compatibility constraints.

Эти правила не являются законами природы.

---

# 6. WorldRepresentation

> **WorldRepresentation — вычислительное представление заданного PhysicalWorld для конкретной задачи.**

Обозначение:

\[
\rho_{\mathcal T,H,\varepsilon}(w)
\]

зависит от:

- задачи \(\mathcal T\);
- горизонта \(H\);
- допустимой ошибки \(\varepsilon\);
- доступного solver;
- вычислительного бюджета.

Примеры:

```text
FULL_3D_FEM
MESO_LAYER_OVERBURDEN_SURFACE
MICROKERNEL_GRAPH
COARSE_BLOCK_MODEL
SURROGATE_MODEL
```

---

# 7. Microkernel

Microkernel относится к `WorldRepresentation`.

> **Microkernel — локальная редуцированная подсистема конкретного представления PhysicalWorld, имеющая достаточное состояние и типизированные интерфейсы для воспроизведения внешнего поведения в заданной задаче.**

Microkernel не является утверждением, что природа «состоит из microkernels».

Физические объекты реального мира:

```text
rock
pillar
chamber
backfill
fault
fluid
layer
```

Microkernel — способ их редуцированного моделирования.

---

# 8. Physical state

В полном мире существуют физические поля:

\[
X(\mathbf r,t)
\]

возможные компоненты:

\[
\{
\mathbf u,
\boldsymbol\varepsilon,
\boldsymbol\sigma,
D,
p,
S,
T,
\rho,
\phi,
k,\ldots
\}
\]

Конкретный набор зависит от активной физической гипотезы.

---

# 9. Material fields и state fields

Нужно различать:

## PROPERTY / PARAMETER

\[
E_0,\nu,\rho_0,\theta_{\mathrm{creep}}
\]

если они фиксированы.

## DYNAMIC STATE

\[
p(t),D(t),S(t),\phi(t)
\]

если они эволюционируют.

## DERIVED PROPERTY

\[
E_{\mathrm{eff}}=E(D,S,T,\ldots)
\]

Не дублировать их как независимые факты.

---

# 10. Typed physical couplings

Целостность мира не означает полный граф.

Примеры:

```text
pressure → effective stress
damage → permeability
permeability → flow
flow → pressure
```

Coupling должен иметь:

- from field;
- to field;
- directionality;
- law;
- validity;
- parameters;
- provenance.

---

# 11. Multiplex physical topology

Одна пространственная система может иметь разные topology:

```text
mechanical graph
hydraulic graph
thermal graph
structural/contact graph
```

Mechanical adjacency не обязана совпадать с hydraulic connectivity.

---

# 12. ObservationWorld

ObservationWorld вынесен за пределы PhysicalWorld.

> **ObservationWorld — спецификация того, как физическая trajectory превращается в наблюдаемые данные.**

Обозначение:

\[
o\in\mathcal O
\]

Содержит:

- observation modality;
- station/benchmark geometry;
- epochs;
- reference system;
- error covariance;
- nuisance parameters;
- missingness;
- processing;
- available_from.

---

# 13. Observation operator

Для representation trajectory:

\[
X_w(t)
\]

получаем:

\[
y=H_o[X_w]+\epsilon
\]

где:

\[
\epsilon\sim\mathcal E(R_o)
\]

Примеры:

```text
OW-LVL
OW-UG
OW-GNSS
OW-INSAR
```

GPR, gravity, seismic, EM остаются platform extensions, если не входят в core дипломной проверки.

---

# 14. Реальные и синтетические observation datasets

Нужно различать:

```text
REAL
SYNTHETIC
SEMI_SYNTHETIC
```

SyntheticObservationDataset обязательно содержит:

```text
world_id
representation_id
observation_world_id
solver_run_id
noise_model
seed
```

---

# 15. Causal state vs probe fields

## Causal state

Влияет на physical evolution.

## External/reference

Задаёт внешнее воздействие/фон.

## Probe

Используется для измерения.

Например GPR electromagnetic field обычно не должен автоматически становиться causal driver геомеханики.

---

# 16. Гравитация

Гравитация имеет две разные роли.

## Mechanical body force

\[
\rho\mathbf g
\]

## Observation/potential field

\[
\nabla^2\Phi=4\pi G\rho
\]

Эти две роли не надо смешивать в одном relation type.

---

# 17. Full physical evolution

Абстрактно:

\[
\mathcal F_w[X]=0
\]

или:

\[
\partial_tX=
F_w(X,\nabla X,\Theta,B)
\]

Это интерфейс, а не конкретная новая физическая формула.

---

# 18. Reduced representation

Для representation:

\[
x(t)=\mathcal R[X(t)]
\]

желательно:

\[
\dot x=
f(x,u,m,\theta)
\]

с memory/internal state \(m\), если это требуется.

---

# 19. External equivalence

Два representations одного мира могут считаться эквивалентными для задачи, если:

\[
d_Y(
Y_{\rho_1(w)},
Y_{\rho_2(w)}
)
<
\varepsilon_{\mathcal T}
\]

по требуемым physical ports/observables.

Это criterion representation quality, а не критерий истинности мира.

---

# 20. Kernel terminology

Нужно различать:

## Microkernel

единица reduced representation.

## Response kernel

математический оператор влияния.

## Memory kernel

оператор временной памяти.

Одинаковое слово `kernel` не означает один и тот же объект.

---

# 21. Known mathematical foundations

Следующие результаты не являются нашей математической новизной:

- Volterra expansions;
- conservation/telescoping balances;
- Loewner monotonicity of information;
- comparison principles;
- Bayesian inverse theory;
- conformal prediction;
- spatial filtering;
- homogenization;
- Mori–Zwanzig projection.

Их можно использовать только с корректными assumptions/citations.

---

# 22. Наблюдаемая различимость

Рабочая research quantity:

\[
D_{\mathrm{obs}}(w_a,w_b;O)
\]

должна учитывать observation covariance.

Для Gaussian errors естественный кандидат:

\[
D_M^2
=
(\mu_a-\mu_b)^T
R^{-1}
(\mu_a-\mu_b)
\]

но конкретная форма ещё не frozen.

Для family discrimination рассматривается не только расстояние отдельных representative worlds, но и расстояние ближайших совместимых представителей.

---

# 23. Indistinguishability

Если:

\[
D_{\mathrm{obs}}\ll \text{decision threshold}
\]

два PhysicalWorld могут быть физически разными, но неразличимыми данной observation system.

Это не ошибка модели.

Это ограничение информации.

---

# 24. Ensemble coverage

До обучения forecasting method нужно проверить:

> способны ли admissible worlds вообще воспроизводить реальные наблюдения в пределах observation/model discrepancy?

Если нет, world space неполно.

Нельзя исправлять это ML-моделью поверх плохого ensemble.

---

# 25. Inverse crime

Нельзя считать сильным доказательством ситуацию:

```text
generator family A
→ synthetic data
→ algorithm trained on A
→ algorithm validated on A
```

Future validation должна позволять:

- held-out physical families;
- different solver;
- different resolution;
- explicit model discrepancy.

---

# 26. Prior over worlds

\[
\pi(w)
\]

не назначается автоматически.

Если объективного prior нет, допустимо работать с:

\[
\mathcal W_{\mathrm{NROY}}
\]

— множеством not-ruled-out-yet worlds.

Family weights должны быть маркированы как `MODEL_CHOICE`, если они субъективны.

---

# 27. World-0 — research candidate

Для первого будущего executable mesoworld принят как кандидат:

```text
MINED LAYER
     ↓
OVERBURDEN
     ↓
SURFACE
```

Один возможный reduced model:

\[
\sigma_i=
\sigma_i^0-\sum_jK_{ij}v_j
\]

\[
\dot v_i
=
h_iA_i\phi(
\sigma_i-P_i(v_i,z_i)
)
\]

\[
s(x,t)
=
\sum_iG(x,\xi_i)a_iv_i(t)
\]

Это НЕ фундаментальное определение PhysicalWorld.

Это `WorldRepresentation` для будущего `World-0`.

---

# 28. Research candidates — не frozen results

Следующие направления признаны полезными, но пока не являются каноническими результатами.

## RC-T0

Conditional coverage transfer при явно принятой exchangeability assumption.

Exchangeability не считается доказанной по одному СКРУ-1.

## RC-T1

Comparison/monotonicity result для cooperative class при проверенной sign structure \(K\).

Не переносить на несовместимые world families автоматически.

## RC-T2A

Norton-like local creep + compliant overburden может дать analytically derived time response; exponential Knothe-like law возникает как специальный/предельный случай.

## RC-T4

Surface displacement acts as spatial low-pass filter.

Использовать `practical/effective unobservability`, а не strict nullspace, если transfer function не равна нулю.

## RC-T5

Observation discrimination / network design с полной covariance \(R\).

---

# 29. Research hypotheses

## G1

Разные механизмы памяти могут быть практически неразличимы на коротком окне и расходиться на длинном горизонте.

## G3

Изменение spatial form increments может потенциально предшествовать очевидному ускорению максимальной амплитуды.

## G4

Неразличимость physical families на фактической сети может доминировать над instrumental noise.

## G5

Sign structure influence matrix \(K\) для одно- и многопластовых representations должна быть получена из high-fidelity simulations, а не постулирована.

Все эти пункты: `TEST_REQUIRED`.

---

# 30. Что НЕ фиксируется как результат

Не принимаются без доказательства/валидации:

- «microkernel — физическая частица мира»;
- «все поля обязательно двусторонне связаны»;
- «два corner worlds всегда ограничивают весь ensemble»;
- «damage всегда сохраняет cooperative structure»;
- «surface has mathematically exact invisible subspace»;
- «geometry объясняет LAB→MASSIF ×2–3»;
- «exchangeability synthetic worlds and real mine proven»;
- «T6 stability result уже доказан для evolving damage».

---

# 31. Diploma core vs extensions

Для core специальной части приоритет:

```text
leveling
underground surveying/convergence
GNSS
InSAR
```

Расширения платформы:

```text
seismic
GPR
gravity
electrical
hydro
```

Они не удаляются из общего framework, но не обязаны быть реализованы в дипломном алгоритме.

---

# 32. Forecast semantics

Forecast должен уметь возвращать не только point value.

Например:

\[
\mathcal S_H(x)
=
\{
s_w(x,t_0+H):
w\in\mathcal W_{\mathrm{consistent}}
\}
\]

или статистическое summary этого множества.

---

# 33. Regime semantics

Latent regime \(Z\) остаётся reduced/inferred variable:

\[
Z=\Psi(X,y_{0:t})
\]

но не объявляется фундаментальным полем природы.

Будущая интерпретация может включать family consistency/weights.

---

# 34. Design of observations

Future research question:

> какая observation system быстрее/надёжнее различает физически важные family alternatives или сужает forecast envelope?

Это две разные задачи:

```text
discrimination
estimation
```

их оптимальные сети могут отличаться.

---

# 35. Статус v0.2

```text
Real object vs PhysicalWorld separation       ACCEPTED_DRAFT
PhysicalWorld definition                      ACCEPTED_DRAFT
ObservationWorld separation                   ACCEPTED_DRAFT
WorldRepresentation separation                ACCEPTED_DRAFT
Microkernel as representation                 ACCEPTED_DRAFT
Four axis classes                             ACCEPTED_DRAFT
D_obs with covariance                         RESEARCH_DIRECTION
World-0 mesomodel                             RESEARCH_CANDIDATE
RC-T0/T1/T2A/T4/T5                            RESEARCH_CANDIDATE
G1/G3/G4/G5                                   TEST_REQUIRED
Mathematical novelty claim                    NOT_ESTABLISHED
Physical novelty claim                        NOT_ESTABLISHED
```

---

# 36. Короткая схема

```text
REAL SKRU-1
     ↑ evidence
WorldSpec
     ↓ completion
PhysicalWorld w ∈ W
     ↓ choose representation
WorldRepresentation ρ(w)
     ↓ solve
Physical trajectory Xw(t)
     ↓ ObservationWorld O
SyntheticObservationDataset
     ↓
forecast / discrimination / validation
     ↕
real observations
```

Это является рабочей архитектурой theory v0.2.
