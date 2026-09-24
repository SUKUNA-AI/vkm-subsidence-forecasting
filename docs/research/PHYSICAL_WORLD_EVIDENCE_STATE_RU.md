# Физическая ветка исследования ВКМ: каноническое состояние, доказательная база и следующий этап

Дата фиксации: 25 сентября 2026 года.

Этот документ фиксирует текущее понимание проекта после:

- первого исторического model baseline;
- исправления и заморозки scenario v2.1;
- заморозки representation v2.1 R1;
- R1 — исследования neural comparators;
- R2 — adversarial source/data/physics audit;
- repository cleanup;
- добавления нового корпуса геологических, геомеханических, маркшейдерских и геофизических материалов по Верхнекамскому месторождению.

Документ НЕ заменяет frozen receipts и historical commits. Его задача — дать одну актуальную карту исследования и объяснить, что считать доказанным, что является лабораторным stress-test, а что должно перейти в будущую физическую ветку.

---

## 1. Текущее состояние репозитория

Основной repository:

`SUKUNA-AI/vkm-subsidence-forecasting`

Проверенный current main после cleanup:

`1bdfa50e52064fea7f9ed29ff82df847b63f72cb`

Последовательность последних зафиксированных milestone:

- `c5da110` — R1, neural comparator review;
- `2794c877` — R2, adversarial source and physical evidence audit;
- `f9e811b3` — удаление superseded research artifacts;
- `1bdfa50e` — canonical repository state после cleanup.

Отдельный private evidence repository:

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

Он предназначен для книг, статей, диссертаций, source PDFs, GIS/Excel/source archives и будущего physical-evidence registry. Model outputs, evaluator truth и benchmark predictions туда не переносятся.

---

## 2. Что является исторической отправной точкой

Первый полный эксперимент, где одновременно существовали B1/B5/B6/B7, реально оценённый IMM, MAE и coverage — Gate B3, commit:

`ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4`

Это историческая отправная точка алгоритмической ветки.

Исторический B7 получил temporal MAE 6.545 мм/год и coverage95 0.962, но полный screening не прошёл. Эти числа нельзя переносить на v2.1, physical worlds или будущую field evaluation.

Смысл historical baseline:

- доказать, что forecasting pipeline, B1/Kalman/IMM и uncertainty evaluation вообще были реально реализованы;
- сохранить первый законченный эксперимент;
- иметь исходную точку для истории дипломной работы.

Он НЕ является текущим доказательством качества модели на реальном СКРУ-1.

---

## 3. Что такое v2.1 и зачем он остаётся

Current authoritative synthetic release:

`SKRU1_SCENARIO_SIMULATION_V2_1`

Current authoritative representation:

`SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`

v2.1 — это:

**publication-envelope-conditioned factorial / adversarial stress benchmark**.

Он нужен для контролируемых вопросов вида:

- что произойдёт с B1, Kalman, IMM, GRU при acceleration;
- как алгоритм ведёт себя при reactivation;
- путает ли изменение измерительного процесса со сменой физического режима;
- как влияет missingness;
- как ведёт себя алгоритм на held-out mechanisms;
- как меняются MAE, coverage, interval width, false transitions и detection delay.

v2.1 НЕ является:

- полевой валидацией;
- цифровым двойником СКРУ-1;
- восстановленной истинной историей СКРУ-1;
- геомеханически откалиброванным массивом;
- доказательством природной частоты temporal families.

R2 подтвердил, что сам benchmark можно использовать как stress laboratory с ограничениями. Его не требуется перестраивать только ради того, чтобы сделать его похожим на физическую модель.

Известные обязательные оговорки v2.1:

1. семь amplitude series происходят фактически из четырёх профильных линий и не являются семью независимыми пространственными объектами;
2. принадлежность этих профилей именно СКРУ-1 не доказана с требуемой строгостью;
3. `reflector_seasonal` — чистый adversarial stress condition, а не реалистичная сезонная модель InSAR;
4. равные доли temporal families — engineering design;
5. текущая temporal/spatial generator logic не является геомеханическим forward solver.

---

## 4. Почему появилась идея latent worlds

Реальные публикации дают неполные наблюдения:

- отдельные профильные линии;
- отдельные карты скоростей;
- интервальные оседания;
- отдельные временные ряды;
- неполный календарь съёмок;
- разные методы наблюдений.

Из одной накопленной карты или двух крайних дат невозможно однозначно восстановить единственную временную историю массива.

Поэтому вместо утверждения:

`мы восстановили настоящую скрытую историю`

используется более честная постановка:

`существует множество латентных миров, совместимых с доступным evidence`.

В v2.1 latent world — инженерная synthetic construction. В будущей physical branch latent world должен стать конкретной геомеханической гипотезой, порождённой физическим forward solver.

---

## 5. Главная проблема старого world generator

Текущие temporal families полезны для stress-testing, но они задают форму оседания сверху:

`T(t) -> settlement`.

Во многих мирах пространственная часть по существу масштабирует одну и ту же temporal shape.

Это означает, что большое число rows, points и observation replicas не равно большому числу независимых физических процессов.

Для physical worlds требуется обратная логика:

`geometry + geology + material state + mining + rheology + boundaries -> mechanics -> settlement`.

То есть временная кривая должна быть результатом механики, а не заранее выбранной функцией `smooth_acceleration`, `exponential_decay` или `reactivation`.

---

## 6. Как должен выглядеть physical world

Базовый future world contract:

```text
WORLD
├── geology realization
│   ├── stratigraphy
│   ├── lithology
│   ├── layer thickness
│   ├── folding/fracturing class
│   └── local anomalies
│
├── geometry
│   ├── mine horizons
│   ├── panels/blocks
│   ├── chambers
│   ├── pillars
│   └── interfaces
│
├── geophysical fields
│   └── Vp(x,y,z) and/or other measured fields
│
├── mechanical properties
│   ├── E
│   ├── nu
│   ├── density
│   ├── UCS
│   ├── cohesion
│   ├── friction angle
│   └── long-term strength
│
├── rheology
│   ├── constitutive law
│   ├── creep parameters
│   └── scale/provenance
│
├── mining history
│   ├── extraction sequence
│   ├── excavation dates
│   └── geometry evolution
│
├── backfill
│   ├── method
│   ├── fill fraction
│   ├── residual gap
│   └── lag
│
├── initial stress
├── boundary conditions
├── interfaces/contact behaviour
│
├── forward solver
│   └── OpenGeoSys
│
├── latent outputs
│   ├── u(x,y,z,t)
│   ├── stress
│   ├── strain
│   ├── damage/failure indicators
│   └── surface settlement
│
└── observation operators
    ├── leveling
    ├── InSAR/SAR
    ├── GPR
    └── other geophysical/survey data
```

Каждый numeric parameter обязан иметь provenance:

- `SITE_SPECIFIC`;
- `VKM_REGIONAL`;
- `OTHER_VKM_SITE`;
- `METHOD_GENERAL`;
- `ENGINEERING_ASSUMPTION`.

И отдельно scale:

- `LAB`;
- `MASSIF`;
- `CALIBRATED_EFFECTIVE_MODEL`.

Лабораторный E нельзя автоматически выдавать за effective continuum modulus FEM-модели.

---

## 7. Самые ценные новые материалы

### 7.1. Лебедева — главный blueprint physical branch

Диссертация О.О. Лебедевой по деформационным процессам междушахтных целиков рассматривает районы СКРУ-1–СКРУ-2 и СКРУ-2–СКРУ-3.

Научно важная цепочка:

`геологические колонки + акустический каротаж + лабораторные испытания + маркшейдерские оседания -> геолого-геомеханическая модель -> FEM -> surface settlement`.

Особенно ценна связь скорости продольной волны `Vp` с механическими характеристиками. Это позволяет рассматривать spatial geophysical field как носитель пространственной изменчивости E/UCS/c/phi, а не задавать одну константу на весь слой.

В работе также показан подход к построению крупной 3D геологической модели с геостатистическим восстановлением spatial variability и дальнейшей конечно-элементной оценкой деформаций.

Для нас Лебедева — не готовая independent truth, потому что часть модели откалибрована по наблюдаемым оседаниям. Но это очень сильный методический blueprint, набор priors, case-study и evidence source.

### 7.2. Кудряшов — geology backbone

Монография А.И. Кудряшова `Верхнекамское месторождение солей`, 2013 должна стать основным региональным geological source вместо попыток вытаскивать geology из вторичных ВКР.

Она покрывает:

- стратиграфию;
- литологию;
- тектонику;
- гидрогеологию;
- трещиноватость;
- складчатость;
- малые структуры;
- карст;
- механизмы деформации соляных пород.

Она задаёт regional geology envelope, но сама по себе не даёт полной site-specific 3D geometry СКРУ-1.

### 7.3. Физико-механические свойства соляных пород ВКМ

Учебное пособие по физико-механическим свойствам соляных пород ВКМ — ключевой источник для распределений прочности, деформируемости, длительной прочности и creep behaviour.

Главный вывод для generator design: свойства массива сильно вариативны. Нельзя строить world как набор единичных констант по слоям.

Особенно важно различать:

- лабораторную прочность и модуль;
- массивные свойства;
- effective/calibrated параметры континуальной модели.

### 7.4. Соловьёв–Секунцов — технология, закладка и устойчивость

Практикум `Разработка калийных месторождений` даёт:

- особенности рудников ВКМ;
- способы вскрытия;
- системы разработки;
- технологию подготовительных/очистных работ;
- закладочные работы;
- методики расчёта устойчивости;
- отдельные сведения по СКРУ-1.

Особенно ценно появление физически мотивированных временных механизмов: степень нагружения, creep, критическая деформация, время потери устойчивости междупластья/целиков.

Это позволяет интерпретировать regime change не как абстрактный `fast/slow`, а как переход физического состояния массива.

### 7.5. Беляков–Беликов 2022 — FEM reference problem

Статья по прогнозу целостности водозащитной толщи на БКПРУ-4 содержит полноценную plane-strain FEM постановку, visco-elasto-plastic behaviour сильвинита, explicit time-dependent deformation и опубликованные параметры модели.

Она не является donor-источником site-specific параметров СКРУ-1.

Её правильная роль — reference benchmark для OpenGeoSys:

`можем ли мы открытым solver stack воспроизвести качественно/количественно опубликованную Abaqus-постановку?`

### 7.6. Георадар ВКМ

Новая работа по GPR-гипсометрии пластов на ВКМ показывает, что георадар может давать независимое evidence о:

- геометрии границ пластов;
- положении слоёв;
- сложной складчатости;
- условиях проходки;
- потенциально состоянии/нарушенности массива.

Это не прямой sensor оседания.

Его правильная роль:

`GPR -> constraint on hidden geometry/material state -> physical world -> surface deformation`.

В более поздней high-fidelity ветке возможна связка:

`mechanical world -> electromagnetic parameters -> gprMax -> synthetic radargram`.

### 7.7. Долгие временные наблюдения и InSAR

Материалы Бабаянца, Гусева–Епина–Цветкова и другие наблюдения по ВКМ/Березникам показывают, что реальная temporal dynamics часто выглядит как:

- продолжительный quasi-steady background;
- медленное изменение скорости;
- локальные acceleration episodes;
- режимные изменения на отдельных участках.

Это не оправдывает равновероятный набор synthetic temporal families, но даёт qualitative/quantitative temporal evidence для проверки будущих physical worlds.

### 7.8. Геодинамически активные зоны

Работы по lineament/geodynamic zones полезны как spatial hazard/heterogeneity evidence.

Их нельзя превращать в произвольное правило вида `active_zone -> E *= 0.5`.

Правильная роль — отдельная world stratum или расширение uncertainty priors там, где evidence указывает на повышенную неоднородность/трещиноватость.

---

## 8. Что изменилось по physical readiness после нового корпуса

Оценка R2 `MORE EVIDENCE REQUIRED` была корректной для тогдашнего корпуса. После добавления новых источников readiness становится выше:

| Компонент | Состояние сейчас |
|---|---|
| Regional geology / stratigraphy | STRONG |
| Site-specific SKRU-1 geology | PARTIAL |
| Mining technology | GOOD regional / PARTIAL site |
| Material priors | STRONG regional |
| Mine-specific material priors | PARTIAL |
| Rheology / creep forms | PARTIAL-STRONG prior |
| Initial stress | REGIONAL PRIOR AVAILABLE |
| Backfill | PARTIAL |
| Local mining chronology | PARTIAL / fragmented |
| Surface temporal evidence | GOOD regional, PARTIAL site-specific |
| GPR/geophysics | GOOD VKM-specific method evidence |
| Full SKRU-1 3D geometry | MISSING/PARTIAL |
| Raw borehole/geophysical grids | MISSING |
| Site-specific boundary conditions | MODEL ASSUMPTION REQUIRED |

Следствие:

полный 3D digital twin СКРУ-1 пока преждевременен;

но evidence-backed 2D/2.5D physical ensemble вдоль выбранного участка/профиля уже научно реалистичен.

---

## 9. Кандидат на первый physical case-study

Наиболее привлекательный первый объект — район междушахтного целика СКРУ-1–СКРУ-2 / профильной линии №1.

Причины:

- есть direct SKRU linkage;
- есть mining/history context;
- есть долговременная профильная линия;
- есть опубликованные скорости оседаний;
- есть существующая геомеханическая работа для сопоставления;
- есть потенциальная возможность построить 2D/2.5D plane-strain reference model до полноценной 3D модели.

Первый physical world не должен пытаться воспроизвести весь шахтный field.

Правильный v0:

1. один профиль;
2. несколько key layers;
3. evidence-backed geometry envelope;
4. gravity/near-hydrostatic initial stress prior;
5. simplified mining/backfill chronology;
6. creep-capable material formulation;
7. surface settlement output;
8. comparison с независимыми опубликованными observations, которые не использовались для tuning конкретного parameter set.

---

## 10. Роли OpenGeoSys, gprMax и PhysicsNeMo

### OpenGeoSys

OpenGeoSys должен быть основным open-source forward solver для первой физической ветки.

Его задача:

`world parameters -> numerical mechanics -> displacement/stress/strain fields`.

Он должен заменить ручное задание settlement curve в physical branch.

### gprMax

Опциональный solver наблюдательного GPR-layer.

Роль:

`geometry + EM properties -> synthetic GPR response`.

Использовать только после того, как станет понятна связь mechanical/material state с dielectric/electrical parameters.

### PhysicsNeMo

Не источник неизвестной физики.

Не нужен на первом этапе.

Потенциальная роль после накопления большого набора verified OpenGeoSys simulations:

`mesh/world parameters -> fast neural surrogate of solver`.

То есть:

`evidence -> OGS worlds -> training corpus -> PhysicsNeMo surrogate -> mass world generation`.

---

## 11. Как должны генерироваться физические миры

Не выбирать один best-fit digital twin.

Из-за неидентифицируемости surface settlement лучше использовать ensemble:

```text
parameter priors / uncertainty ranges
        ↓
Sobol / Latin Hypercube / geostatistical realizations
        ↓
OpenGeoSys forward simulation
        ↓
physical/numerical validity checks
        ↓
comparison with empirical constraints
        ↓
accepted / weighted ensemble
```

Это позволяет честно сохранять несколько механически допустимых объяснений одних и тех же наблюдений.

World diversity должна появляться из:

- geological realizations;
- layer geometry;
- material variability;
- rheology;
- mining chronology;
- backfill state;
- initial stress uncertainty;
- interfaces/folding/damage scenarios.

Noise replicas и observation missingness являются отдельным уровнем и не увеличивают число независимых физических миров.

---

## 12. Что всё ещё нужно найти

Приоритетные пробелы:

1. исходные/более точные планы и chronology выбранного участка СКРУ-1;
2. borehole columns / acoustic logs / цифровые geophysical grids, если доступны;
3. site-specific или максимально близкая calibration реологии для СКРУ-1;
4. raw tables профильной линии №1 и других usable SKRU profiles;
5. полный текст актуальных `Указаний по защите рудников ПАО Уралкалий`;
6. настоящая книга Борзаковского–Папулова `Закладочные работы на Верхнекамских калийных рудниках`, 1994;
7. документы/таблицы по фактической закладке, chronology и extraction geometry;
8. источники, позволяющие обосновать boundary-condition family и local initial stress uncertainty;
9. GPR data beyond interpreted figures, если возможно получить raw radargrams/processed sections;
10. исходные TAB/Excel/GIS materials, упомянутые в старой reconstruction, если они реально существуют вне derived GPKG.

---

## 13. Что считать результатом предыдущей работы

Предыдущая работа не является выброшенной.

Она дала:

- forecasting task semantics;
- historical baseline;
- B1/Kalman/IMM implementations;
- uncertainty metrics;
- next-planned target semantics;
- data/reconstruction provenance discipline;
- sealed evaluator boundary;
- world-disjoint validation design;
- stress-testing infrastructure;
- adversarial audit process;
- понимание того, какие claims нельзя делать.

То, что удалено cleanup-коммитом, в основном являлось superseded execution/report machinery и остаётся доступно в Git history.

Правильное разделение теперь такое:

`historical model experiment` — история алгоритмической разработки;

`v2.1` — controlled stress laboratory;

`physical branch` — evidence-backed geomechanical world ensemble.

---

## 14. Private resources repository

Private repository:

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

Он должен стать единой библиотекой evidence.

Рекомендуемая локальная копия:

`E:\vkm-subsidence-forecasting_resourses\`

Не размещать private repository внутри `E:\Диплом` как обычную вложенную Git repository. Для временного inbox новых файлов использовать ignored directory основного проекта:

`E:\Диплом\work\resource_inbox\`

После review источник копируется из inbox в private resources repository, получает canonical filename, SHA-256 и запись в source registry.

Основные классы ресурсов:

- primary SKRU/VKM sources;
- dissertations;
- books/monographs;
- journal articles;
- regulations/guidance;
- GPR/InSAR/seismic/geophysics;
- GIS/maps;
- original Excel/ZIP/source archives;
- curated physical evidence tables.

Main repository остаётся code + frozen stress lab + derived research contracts/reports.

---

## 15. Следующий научный этап

Следующий большой этап — не model training.

Последовательность:

1. перенести и зарегистрировать evidence corpus в private resources repository;
2. проревьюить оставшиеся `REVIEW_MANUALLY` Excel/ZIP containers;
3. сделать один consolidated physical evidence model: geology -> geometry -> materials -> rheology -> mining/backfill -> initial stress -> observations;
4. выбрать первый 2D/2.5D SKRU case-study;
5. формально заморозить `WORLD_PHYSICS_CONTRACT_V1`;
6. воспроизвести published/simple FEM reference case в OpenGeoSys;
7. только после solver-validation построить ensemble worlds;
8. затем сравнивать B1/Kalman/IMM/R1 neural comparators на physical ensemble;
9. IMM improvements проводить после baseline, а не подгонять solver/worlds под IMM;
10. PhysicsNeMo рассматривать только как поздний surrogate layer.

---

## 16. Главный scientific claim на текущий момент

Проект прошёл от попытки прогнозирования на слабой reconstruction к трёхуровневой научной системе:

```text
REAL EVIDENCE
     ↓
CURRENT SYNTHETIC STRESS LAB v2.1
     ↓
ALGORITHM ROBUSTNESS
```

и параллельно развивается более сильная ветка:

```text
REAL GEOLOGY / GEOMECHANICS / GEOPHYSICS
     ↓
EVIDENCE-BACKED PHYSICAL WORLD ENSEMBLE
     ↓
OPEN SOURCE FORWARD SOLVER
     ↓
SURVEY / INSAR / GPR OBSERVATION OPERATORS
     ↓
FORECASTING ALGORITHMS
```

Текущий предел claims:

- v2.1 уже годится как adversarial algorithm laboratory;
- полноценный physical digital twin СКРУ-1 пока не доказан;
- новый корпус источников уже достаточен для проектирования первого ограниченного evidence-backed 2D/2.5D physical ensemble;
- окончательная physical parameterization должна строиться только через explicit provenance и uncertainty, без скрытого переноса regional/lab значений в site-specific truth.
