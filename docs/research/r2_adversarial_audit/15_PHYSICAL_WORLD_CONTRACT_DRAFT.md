# R2 · Черновик контракта физического мира (DRAFT, не фиксируется)

Статус: **DRAFT**. Схема не замораживается, потому что аудит показал нехватку ключевых сущностей (исходная геометрия, хронология, K0, температура, свойства по слоям). Миры не генерируются.

## 1. Черновая схема

```yaml
world_id: string                     # уникальный, с версией схемы
schema_version: "draft-r2"
status: DRAFT | SIMULATED | REJECTED | ACCEPTED_COMPATIBLE
geometry:
  crs: local_reconstructed | projected   # + неопределённость привязки, м
  domain_extent_m: [x_min, x_max, y_min, y_max, z_min, z_max]
  dimensionality: 2D_profile | 2.5D | 3D
  mining_zones: [ {zone_id, polygon_ref, depth_roof_m, extracted_thickness_m, omega, chamber_w_m, pillar_w_m, load_C, provenance} ]
stratigraphy:
  layers: [ {name, top_m, thickness_m, lithology, provenance, tier} ]
material_parameters:
  per_layer: { layer_name: {rho, E, nu, c, phi, tensile, provenance, prior_id, transfer_risk} }
rheology:
  law_family: norton | burgers_mc | doering_kiehl | lubby2 | bgra | double_power
  parameters: { ... , provenance, lab_or_massif, stress_range_valid }
  pillar_degradation: {model, time_scale_years, provenance}
  backfill: {A_fill, B_shrinkage, model, provenance}
mining_sequence:
  events: [ {zone_id, t_start, t_end, action: excavate|backfill|interseam_failure, provenance: SOURCE|ASSUMED} ]
initial_state:
  sigma_v: gravity_integral
  K0: {value_or_prior, provenance}
  temperature: {profile_or_prior, provenance}
boundary_conditions:
  lateral: roller|fixed|far_field; bottom: ...; top: free
  provenance: ENGINEERING_ASSUMPTION
parameter_provenance: [ {parameter, tier, source_id, location, citation_admissibility} ]
solver: {name: OpenGeoSys, version, process, constitutive_backend: builtin|MFront}
solver_version: string
mesh_identity: {mesh_sha256, element_type, resolution_m, convergence_study_ref}
latent_outputs:
  surface_displacement: {grid_or_profiles, times, units: mm, sign: positive_down}
  derived: [rate_mm_y, tilt_mm_m, curvature, horizontal_strain]
constraint_compatibility:
  checks: [ {constraint_id, observable, operator, tolerance_basis, result, margin} ]
assumption_register: [ {assumption_id, statement, tier: 4, alternatives} ]
uncertainty_register:
  source_evidence: [...]      # A — незнание значения
  world_variability: [...]    # B — реальная изменчивость между мирами
  numerical: [...]            # C — сетка/сходимость
  observation: [...]          # D — шум измерений
```

Недостающие сущности, из-за которых схема не фиксируется: полный список зон с геометрией (MER-03), колонки слоёв (MER-04), свойства по слоям (MER-05), реология массива (MER-06, MER-13), K0 (MER-07), температура (MER-10), усадка закладки (MER-11), реальный оператор нивелирования (MER-02, MER-14).

## 2. Концепция генерации (§34), без расчётов

```text
parameter priors (10_PHYSICAL_PARAMETER_PRIORS; каждый с provenance)
  → Sobol / Latin Hypercube по независимым осям неопределённости
  → OpenGeoSys прямой расчёт (гомогенизированный слой отработки, сценарная хронология)
  → поле смещений поверхности (latent)
  → операторы наблюдения (нивелирование, радар: сезон, LOS, термика — только для радара)
  → сравнение с эмпирическими ограничениями совместимости
  → принятый/взвешенный ансамбль
```

Принципы:

- **Не выбирать один best-fit мир.** Обратная задача по поверхностным данным, скорее всего, неидентифицируема (11_WORLD_MODEL_IDENTIFIABILITY) — сохраняется ансамбль совместимых миров.
- **Допуски не назначаются сейчас.** Они должны следовать из неопределённости ограничений (границы чтения, длительность интервалов, атрибуция), а не выбираться под желаемую долю принятых миров.
- Кандидаты ограничений совместимости: огибающие SUP01 (с явной атрибуцией и длительностью как диапазоном), L≈1.54H и типовые S(z) (EXT03), η_ok = 0.9ωmp (EXT03), масштаб накопленного оседания к ~2022 (SRC01, до 4.3 м), почти линейные многолетние ряды (SRC05, SUP01 — только качественно).

## 3. Четыре типа неопределённости (§35) — не смешивать

| Тип | Смысл | Примеры | Где хранится |
|---|---|---|---|
| A. Source/evidence | Мы не знаем значения | E слоёв, K0, длительность «2015–2016», атрибуция линий | uncertainty_register.source_evidence; prior |
| B. World/physical variability | Параметр реально различается между допустимыми мирами/зонами | UCS по пробам (CV 14–70%), C по блокам, годы отработки | ансамбль миров |
| C. Numerical solver | Аппроксимация | размер элементов, шаг по времени, гомогенизация | mesh_identity, convergence study |
| D. Observation | Шум и операторы измерений | σ нивелирования, радарный шум ≈20 мм/год за сезон, термика крыш | операторы наблюдения |

## 4. Не подгонять физику под v2.1 (§36)

- v2.1 — объект критики, а не эталон. Физическая модель не выбирается по тому, что она воспроизводит семейства v2.1.
- Если физические миры покажут преобладание почти стационарных режимов над старыми выработками (как намекают SRC05 рис.6б и SUP01 p.14), то семейства v2.1 с короткими эпизодами/ступенями остаются **чистыми stress-тестами**, а не физической моделью.
- Условие `reflector_seasonal` v2.1 нельзя переносить в операторы физических миров без исправления сезона (ноябрь–апрель) и формы термического эффекта.
