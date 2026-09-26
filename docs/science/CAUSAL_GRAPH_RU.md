<!-- Опубликовано из PRIVATE 11_evidence_vnext/canonical/PHYSICS_CAUSAL/CAUSAL_GRAPH_RU.md (синтез Phase 1). Дословные цитаты источников — только в PRIVATE. -->
# Причинный граф мира ВКМ / СКРУ-1: от геологии до маркшейдерского наблюдения (поток PHYSICS_CAUSAL, фаза 1)

Статус: **концептуальный граф** (Phase 1). Граф описывает, *что на что влияет и через какое наблюдение это видно*;
он не является моделью решателя и не содержит чисел состояния. Файлы: `causal_graph_nodes.csv` (110 узлов),
`causal_graph_edges.csv` (241 ребро), `causal_graph.json` (node-link, без цитат), `causal_graph_validation.json`
(проверки networkx 3.7). Определения — `scripts/graph_defs.py`, сборка и проверка — `scripts/build_graph.py`.

## 1. Устройство графа

### 1.1 Слои основной цепи

`geology(0) → geometry(1) → material_state(2) → initial_state(3) → mine_design(4) → actual_excavation(5) →
backfill(6) → physical_evolution(7) → deep_displacement(8) → overburden_response(9) → surface(10) →
observation(11)`.

Ветви: hydro, thermal, interfaces, damage, dynamics, GPR/EM, seismic, InSAR, nuisance (немайнинговые сигналы),
information (документы, нормативы, журналы).

### 1.2 Классы узлов

| Класс | Число | Правило |
|---|---|---|
| `physical` | 75 | физическое состояние/процесс; **обязан достигать `surface_displacement`** |
| `obs_medium` | 2 | свойства среды, нужные только наблюдению (ЭМ, акустика); обязаны достигать наблюдения |
| `surface_derived` | 2 | наклоны/кривизна/деформации и трещины/суффозия — ниже по течению от смещения поверхности |
| `nuisance` | 8 | немайнинговые сигналы (солеотвалы, тепловое расширение зданий, промерзание, земляные работы, нестабильность опорных реперов, атмосфера, снег, фоновые движения); входят только в наблюдения |
| `information` | 4 | нормативы/районирование, маркшейдерские планы, журналы закладки, журналы событий (информационное время) |
| `observation` | 19 | модальности наблюдений; **обязаны быть стоками** |

У каждого узла: `layer`, `scenario` (NORMAL / ACCIDENT), `branch`, `state_variables`, `worldspec_link` (класс схемы
`vkm_world`), `process_ids` (связь с матрицей PC-xx), статус, область, масштаб, уверенность, source_ids, локатор,
vn_ids, цитата.

### 1.3 Типы рёбер

| Тип | Число | Смысл |
|---|---|---|
| `physical` | 153 | физическая причинность (в т.ч. геологического времени) |
| `observation` | 64 | латентное состояние → измерение (операторы OO-xx) |
| `engineering` | 18 | проектные решения: норматив/геометрия → проект → факт выемки/закладки |
| `information` | 6 | физическое событие → запись о нём (информационное время, доступность) |

Сила ребра: STRONG 126, MODERATE 86, WEAK 21, HYPOTHESIS 8. Статус механизма ребра: FACT 208, UNKNOWN 12,
DERIVATION 11, ANALOGUE 5, MODEL_CHOICE 4, ENGINEERING_ASSUMPTION 1. Поле `feedback=yes` у 7 рёбер —
запаздывающие обратные связи (см. §4).

## 2. Основная цепь нормального сценария СКРУ-1

Центральный путь (все звенья с доказательствами в `causal_graph_edges.csv`):

```
GEO_STRAT → GM_SEAMS → MD_LAYOUT → AE_GEOMETRY → PE_PILLAR_LOAD → PE_PILLAR_CREEP → PE_CONVERGENCE
          → DD_SEAM_LEVEL → DD_OVERBURDEN_FIELD → OR_BENDING → surface_displacement → OBS_LEVELLING / OBS_INSAR
```

* **геология → геометрия**: литология и складчатость задают глубину/мощность/гипсометрию пластов (КрII, АБ, Вс, Вк) и
  мощность ВЗТ; выщелачивание сводов утоняет ВЗТ.
* **геометрия → проект**: высота камер по мощности пласта; сохранение ВЗТ → камерная система с жёсткими МКЦ;
  проектная ширина целиков по γH и допустимой C (ребро `IS_SIGMA_V → MD_LAYOUT`, engineering).
* **проект → факт**: отклонения комбайна, подрезка «коржей», хронология выемки, наложение пластов, фактическая
  закладка с задержкой.
* **факт → эволюция**: Q=γH(a+b)/b → C → установившаяся ползучесть МКЦ (натурно — БКПРУ-4) → конвергенция.
* **эволюция → глубинные смещения**: конвергенция = опускание кровли выработанного слоя; суммирование по пластам
  (η0=0,9ΣS_i/(a+b)); закладка — через смыкание и отпор.
* **толща → поверхность**: изгиб подработанной толщи/ВЗТ формирует форму и ширину мульды; локальные надстройки —
  разломная зона (разлом №98 СКРУ-1) и суперпозиция через межрудничный целик СКРУ-1/СКРУ-2.
* **поверхность → наблюдение**: нивелирование (u_z относительно опорного репера), горизонтальные измерения, ГНСС,
  InSAR (проекция на LOS относительно опорной точки), гидростатика (только разности внутри здания).

Кратчайшие пути нормального сценария (без аварийных узлов; вычислены networkx):

| Источник | Путь до `surface_displacement` |
|---|---|
| GEO_STRAT | GEO_STRAT → MS_ELASTIC → DD_OVERBURDEN_FIELD → surface_displacement |
| AE_CHRONO | AE_CHRONO → BF_ACTUAL → BF_GAP → BF_CONTACT → DD_SEAM_LEVEL → DD_OVERBURDEN_FIELD → surface_displacement |
| GEO_GEOTHERM | GEO_GEOTHERM → IS_TEMP → PE_SALT_CREEP → DD_OVERBURDEN_FIELD → surface_displacement |
| GEO_FAULTS | GEO_FAULTS → OR_FAULT_LOCAL → surface_displacement |
| AE_NEIGHBOUR | AE_NEIGHBOUR → OR_INTERMINE → surface_displacement |
| MS_MOISTURE | MS_MOISTURE → MS_STRENGTH → OR_VZT_DAMAGE → OR_BENDING → surface_displacement |
| GM_VZT | GM_VZT → OR_VZT_DAMAGE → OR_BENDING → surface_displacement |

Прямые родители `surface_displacement`: DD_OVERBURDEN_FIELD, OR_BENDING, OR_FAULT_LOCAL, OR_INTERMINE (нормальный
сценарий), H_EFF_STRESS (гипотеза, немайнинговая консолидация), H_SINKHOLE и D_DYNAMIC_FAILURE (аварийный сценарий).
Наибольшая посредническая центральность (betweenness, граф без обратных связей): DD_OVERBURDEN_FIELD,
surface_displacement, PE_PILLAR_LOAD, DD_SEAM_LEVEL, OR_VZT_DAMAGE — это «узкие горла», через которые проходит
большинство причинных путей: степень нагружения целиков и сжатие выработанного слоя.

## 3. Ветви

* **Гидро**: GEO_AQUIFERS → H_HEADS → (гипотеза) H_EFF_STRESS → поверхность; аварийная ветвь OR_VZT_DAMAGE → H_WCF →
  H_INFLOW → H_DISSOLUTION / H_FLOODING → H_SINKHOLE → поверхность; H_DISSOLUTION → PE_PILLAR_DEGR (растворение МКЦ).
  BF_BRINE (рассолы закладки) → MS_MOISTURE и PE_PILLAR_DEGR (нормальный сценарий, приконтурно).
* **Термо**: GEO_GEOTHERM/GEO_CLIMATE → IS_TEMP → скорость ползучести (гипотеза Аррениуса, HYPOTHESIS);
  GEO_CLIMATE → T_MINE_CLIMATE → MS_MOISTURE (сезонная конденсация); GEO_CLIMATE → N_FROST_SOIL, N_BUILDINGS_THERMAL,
  N_SNOW, N_ATMOSPHERE → наблюдения.
* **Интерфейсы**: GEO_CLAY → MS_INTERFACE → PE_INTERFACE → PE_ROOF / PE_INTERSEAM / PE_CONVERGENCE; GEO_CLAY →
  MS_STRENGTH (снятие торцевого трения целика), MS_ANISO, MS_SCALE, MS_EM («коржи» — отражатели GPR).
* **Повреждение**: PE_PLASTIC/PE_PILLAR_CREEP → PE_DAMAGE → PE_PILLAR_DEGR → (обратная связь) PE_PILLAR_LOAD;
  PE_TERTIARY; OR_VZT_DAMAGE (ограничение/шлюз аварии).
* **Динамика**: GEO_GAS → D_GDP → PE_ROOF (локально); PE_TERTIARY/PE_PILLAR_DEGR/MS_STRENGTH → D_DYNAMIC_FAILURE →
  поверхность и OBS_SEISMIC_MON; AE_NEIGHBOUR → D_DYNAMIC_FAILURE (СКРУ-2, 1995).
* **GPR**: MS_EM, GM_SEAMS, PE_DAMAGE, BF_ACTUAL → OBS_GPR (GPR ≠ оседание).
* **Сейсмика**: MS_ACOUSTIC, GM_HORIZONS, GEO_FAULTS, OR_VZT_DAMAGE → OBS_SEISMIC_SURVEY; MS_ACOUSTIC, MS_DENSITY,
  GEO_STRAT → OBS_LOGS; PE_DAMAGE, D_DYNAMIC_FAILURE → OBS_SEISMIC_MON.
* **InSAR**: surface_displacement → OBS_INSAR, плюс 8 мешающих узлов (солеотвалы, здания, промерзание, земляные
  работы, опорная точка, атмосфера, снег, фон) — у OBS_INSAR 9 входящих рёбер, больше, чем у любой другой модальности.
* **Информация**: AE_CHRONO/AE_GEOMETRY → INFO_MINE_PLANS; BF_ACTUAL → INFO_BACKFILL_LOG; D_GDP/H_INFLOW/
  D_DYNAMIC_FAILURE → INFO_EVENT_LOG; INFO_NORMATIVE → проектные узлы (engineering).

## 4. Обратные связи

Семь рёбер помечены `feedback=yes` (запаздывающая во времени причинность): смыкание закладки → нагрузка целиков и
конвергенция; релаксация и пластичность → перераспределение напряжений; деградация МКЦ → нагрузка (положительная
обратная связь третичной стадии); разрушение междупластья → нагрузка; повреждённость ВЗТ → изгиб. С ними граф
содержит 36 простых циклов; **без них граф ацикличен** (проверка C6) — это позволяет «развернуть» его по времени
(t → t+Δt) в будущей Phase 2.

## 5. Проверки networkx (`causal_graph_validation.json`)

| Проверка | Результат |
|---|---|
| C1: все `physical` узлы достигают `surface_displacement` | выполнено (0 нарушений) |
| C2: все `observation` узлы — стоки и имеют ≥1 вход | выполнено |
| C3: `obs_medium` узлы достигают наблюдения | выполнено |
| C4: `surface_derived` узлы ниже по течению от смещения поверхности | выполнено |
| C5: `nuisance` узлы входят только в наблюдения | выполнено |
| C6: граф без обратных связей — DAG | выполнено |
| C7: слои 0→1→…→11 связаны последовательно | выполнено (11/11 переходов) |
| C8: все ссылки узлов/рёбер и все vn_id существуют | выполнено (0 ошибок) |
| C10: физические узлы нормального сценария достигают поверхности **без** аварийных узлов | выполнено |
| C9: покрытие процессов матрицы | все 72 процесса PC-01…PC-72 упомянуты в узлах/рёбрах |

Итог: `VALID = true`.

## 6. Связь с WorldSpec и утечкой информации

* Каждый узел указывает класс схемы `vkm_world` (`worldspec_link`): геология — `geology.*`, выемка — `mining.objects`,
  `chronology.events`, закладка — `mining.backfill.BackfillRecord`, наблюдения — `observations.catalog.*`.
* Наблюдения — стоки. **Обратная связь «наблюдение → решение»** (например, районирование по опасности → закладка;
  калибровка моделей по оседаниям) в граф намеренно не включена: это информационный процесс, который описывается
  потоком MONITORING_LIFECYCLE и правилами доступности `available_from`. Модели, откалиброванные по наблюдённым
  оседаниям (метод переменных модулей E^p=E^0/[1+Ψ(t)]; снижение E у Лебедевой), в графе представлены узлом
  DD_SEAM_LEVEL со статусом MODEL_CHOICE и не могут служить независимой проверкой прогноза.
* Аварийные узлы (`scenario=ACCIDENT`) отделены: нормальный прогноз строится на подграфе без них (C10), а переход
  в аварийный сценарий — отдельный gate.

## 7. Ограничения

* Граф качественный: сила рёбер — экспертная категория по типу доказательства, а не оценка чувствительности.
* Несколько рёбер имеют статус HYPOTHESIS (температура → скорость ползучести; поровое давление → ВЗТ;
  консолидация покровных отложений → поверхность; GPR → полнота закладки) — они сохранены, чтобы UNKNOWN оставался
  видимым, а не превращался в ноль.
* Узел `surface_displacement` объединяет вертикальную и горизонтальные компоненты; разделение по компонентам и по
  поддержке (точка/пиксель) выполнено в операторах наблюдений (`observation_operator_design.csv`).
