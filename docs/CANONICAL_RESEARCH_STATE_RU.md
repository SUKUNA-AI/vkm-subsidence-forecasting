# Каноническое состояние исследования СКРУ-1

Дата исходной консолидации: 24 сентября 2026 года.
Обновление после externalization resources: 25 сентября 2026 года.

Этот документ определяет, какие выпуски и выводы актуальны. Сохранённые frozen
cards и receipts описывают состояние на момент выпуска; ограничения источников
уточнены последующим R2 и новым physical-evidence corpus. Удалённые
промежуточные состояния доступны в Git history, а externalized source/data
containers — в private resources repository.

## 1. Историческая отправная точка

Первый репозиторный запуск базовых моделей завершён в
[`26f6fa5`](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/commit/26f6fa5).
Первый законченный эксперимент, удовлетворяющий полному критерию
**B1/B5/B6/B7 + реально оценённый IMM + MAE/coverage**, — Gate B3, commit
[`ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4`](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/commit/ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4).
Bootstrap `1d9514d` содержит более ранние аудиты реконструкций, а Gate B0/B1
и B2 — предшествующие model runs; в них ещё нет полного сравнения с B7 IMM.
Поэтому они не заменяют выбранную историческую точку.

Постановка: T1, скорость до **следующей плановой кампании**, исторический
`SKRU1_Data_Foundation_v3_2_1`, разбиения `t1_v1`. Исторические manifests
сохранены в `configs/`, а exact historical data/bootstrap/source containers
после externalization находятся в private repository
`SUKUNA-AI/vkm-subsidence-forecasting_resourses` и в Git history.
Исходные features имеют SHA-256
`5d360910e8eeea10a2572bb35d3de4f0b9b316ec5a6ee3b43389c07b09848ab6`;
next-planned targets —
`289912a0b2c79ee1163529c6998d879594062cf0d453ec7665b2bb06f562ee1b`.
Инвентари исходной поставки — исторические снимки, а не перечень current tree.

Сохранены [параметры B3](../configs/gate_b3.yaml),
[run script](../scripts/run_gate_b3.py),
[IMM](../src/skru1/imm_kalman.py),
[итоговый отчёт](reports/GATE_B3_IMM_RU.md),
[manifest результатов](../artifacts/model_selection/t1_b3_v1/artifact_inventory.csv)
и необходимые защищённые результаты B0/B1/B2. Их predictions нужны для
парного сравнения и проверки predecessor hashes; это не копии новых результатов.
Для **точного повторения** следует использовать отдельный checkout commit
`ce8e57e`, его configs, dependencies и `scripts/run_gate_b3.py --help`.
Команда полного исторического запуска в таком отдельном checkout:
`python scripts/run_gate_b3.py --phase all --root .`; в cleanup она не выполнялась.
Текущие файлы реализации позднее получили дополнительные защитные проверки;
версия алгоритма первого запуска однозначно задаётся исходным commit.

Исторический результат B7: temporal MAE **6.545 мм/год**, coverage95 **0.962**.
Полный screening **не пройден**: остались volatile/gap и spatial ограничения.
Статус — `validation_recorded`, без final/field claim. Это не suite-v4 MAE
5.640 и не результаты v2.1. Последующие B4/B5/B6/C0/C1 сохранены в истории,
включая milestone [`2c5eec4`](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/commit/2c5eec4).
Два suite JSON сохранены только потому, что действующий аудитор исторического
B3 проверяет ими более поздние изменения source hashes; они не выбирают
модель для current stress lab.

## 2. Текущие данные и stress lab

Authoritative dataset — **SKRU1_SCENARIO_SIMULATION_V2_1**;
representation — **SKRU1_SCENARIO_REPRESENTATION_V2_1_R1**.
Назначение: **publication-envelope-conditioned factorial / adversarial stress
benchmark**. Это не полевая валидация, не physical digital twin и не
геомеханически откалиброванный набор СКРУ-1.

| Объект | SHA-256 manifest |
|---|---|
| [v2.1](../data/scenario_simulation_v2_1/manifest.json) | `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95` |
| [constraints](../artifacts/reconstruction/scenario_constraints_v2/manifest.json) | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` |
| [representation](../artifacts/splits/scenario_representation_v2_1/representation_manifest.json) | `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d` |

Основа: [исправленная реконструкция](../data/reconstruction_research_v1/README.md),
публикационные оцифровки и [атлас](reports/RECONSTRUCTION_ATLAS_V1_RU.md).
Реконструкция пространственного профиля не восстанавливает историю кампаний.
Все frozen observations, latent generation, targets, splits и representation
сохранены без пересоздания и изменения значений.

v1 scenario release и его adapter/benchmark удалены из current tree.
`scenario_simulation.py` остаётся неизменным общим модулем: v2.1 импортирует
его функции, а manifest закрепляет его hash. Само старое имя не означает
ненужность кода.

v2 хранится как **проверяемый predecessor**, не как второй текущий dataset.
Его manifest закреплён в inputs v2.1. Полный набор из 20 файлов требуется
сохранённому differential validator: он проверяет все outputs и сравнивает
bytes, календарь и перечень исправленных origins. Поэтому его удаление
сломало бы текущую проверку [erratum](reports/SCENARIO_SIMULATION_V2_1_ERRATUM_RU.md).
Ошибка `days_since_previous_observation` остаётся задокументированной:
исправлено 31 283 значения на +1 день; другие научные значения не менялись.
Таблица mismatch IDs сохранена как hash-pinned свидетельство этой проверки.

Часть frozen manifests исторически ссылается на source/data paths, которые
теперь externalized. Их bytes не изменены: exact snapshots сохранены в
private resources repository. `scripts/verify_canonical_repository.py`
разрешает эти зависимости через private snapshot и проверяет исходные hashes.

## 3. R1, R2 и physical branch

**R1**: [neural comparator review](research/NEURAL_COMPARATORS_V2_1_RESEARCH_RU.md),
[decision matrix](research/NEURAL_COMPARATORS_V2_1_DECISION_MATRIX.csv),
[sources](research/NEURAL_COMPARATORS_V2_1_SOURCES.md).
Это проект будущего сравнения, а не выполненное обучение.
`configs/gate_c1.yaml` и `gate_c1_models.py` сохранены как объекты статического
анализа R1; C1 config не является current launch config. Его зависимости
относятся к checkout `c5da110`, где выполнялся обзор.

**R2** — source/data/physics audit:
[verdict](research/r2_adversarial_audit/01_EXECUTIVE_VERDICT_RU.md),
[claims](research/r2_adversarial_audit/03_CURRENT_DATA_CLAIMS_MATRIX.csv),
[machine receipt](../artifacts/research/r2_adversarial_audit/audit_receipt.json).
Вердикт v2.1 — **SUPPORTED_WITH_LIMITATIONS** как factorial stress benchmark.

Обязательные оговорки R2:

- Атрибуция доноров конкретному руднику неоднозначна: Соликамск,
  территория СКРУ-1/СКРУ-2; семь series не равны семи независимым профилям.
- `reflector_seasonal` — **pure stress**; календарь противоречит источникам.
- Доли temporal families — **engineering design**, не частоты природных режимов.
- На момент R2 site-specific geomechanical parameters, initial stress,
  boundary conditions и полная chronology были недостаточны.
- IMM задаёт кинематический prior; геомеханической калибровки он не доказывает.

После R2 собран дополнительный corpus:
Лебедева 2023, Кудряшов 2013, Барях–Асанов–Паньков,
Соловьёв–Секунцов, Беляков–Беликов, GPR 2026, длительные наблюдения,
источники по геодинамическим зонам, InSAR и технологии разработки.
Актуальная интерпретация зафиксирована в
[physical-world evidence state](research/PHYSICAL_WORLD_EVIDENCE_STATE_RU.md).

Это не превращает проект в 3D digital twin. Однако evidence теперь достаточно,
чтобы проектировать первый ограниченный **2D/2.5D physical case** с явным
разделением `SITE_SPECIFIC / VKM_REGIONAL / OTHER_SITE / ASSUMPTION`.

R2 inventories и reviewed hashes фиксируют дерево на `c5da110` и локальные
источники на дату аудита. Упоминания удалённых файлов в этих снимках —
историческая provenance, не обещание их наличия в current tree.

## 4. Что признано obsolete и externalized

Поздние B4/B5/B6/C execution trees, их generated model-selection artifacts,
notebooks, промежуточные отчёты/Word-черновик, дубли bootstrap-инструкций,
старый v1 scenario release и заблокированный v2 adapter удалены ещё первым
cleanup. Все они доступны в milestone
[`c5da110`](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/tree/c5da1103154c29e2ffe9c52bbceacaa032f1690f).

После формирования private evidence archive из current main дополнительно
externalized:

- `inputs/sources/`;
- `inputs/bootstrap/`;
- `SKRU1_ACTUAL_DATA_TABLES_v1/`.

Exact copies находятся в private resources repository, а исходные версии также
доступны в Git history. Это packaging cleanup, а не изменение научных данных.

Таблица решений первого cleanup — `work/repo_cleanup/classification.csv`;
итог — [cleanup receipt](../artifacts/repository_cleanup/canonical_cleanup_receipt.json).
Новая фиксация externalization:
[REPOSITORY_CONSOLIDATION_2026-09-25_RU.md](REPOSITORY_CONSOLIDATION_2026-09-25_RU.md).

Нельзя смешивать исторические B3/B6/C1 scores, reconstructed source values,
v2.1 synthetic values и будущую field evaluation. Не переносить scores на
новый dataset и не заменять пути frozen runner для получения «новых» результатов.

## 5. Рабочие команды и следующий шаг

Безопасная проверка current core:

```powershell
python scripts/verify_canonical_repository.py
```

Для полной проверки externalized bytes:

```powershell
$env:VKM_RESOURCES_ROOT = "E:\Диплом\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

Verifier хеширует payloads как непрозрачные bytes, evaluator truth не парсит,
модели не запускает, отчёт пишет в `work/repo_cleanup/`.

Статус `PASS_CORE_EXTERNAL_UNCHECKED` допустим, если private repository
отсутствует локально: это означает, что main core цел, но external archive
не был повторно хеширован. При доступном private repo ожидается `PASS`.

Reconstruction entrypoints остаются research code, но для полного rebuild
нужны externalized source/data bytes. Не копировать их обратно в Git index:
использовать private repository/work area.

Следующий исследовательский этап — **Physical Evidence Consolidation**:
собрать machine-readable parameter/source registry, после чего зафиксировать
`Physical World v1 contract` и первый 2D/2.5D OpenGeoSys reference case.
Новый model benchmark до этого не запускается.

## 6. Внешние ресурсы и фиксированные названия

Private resources repository:

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

Resources baseline на момент externalization:
`941f457dc62726073f972f14cca9131ee864145d`.

В нём находятся:

- exact snapshots бывших `inputs/sources/` и `inputs/bootstrap/`;
- exact snapshot `SKRU1_ACTUAL_DATA_TABLES_v1/`;
- исходный `SKRU1_ACTUAL_DATA_TABLES_v1.zip`;
- книги, статьи, диссертации, GPR/InSAR/geomechanics corpus;
- Excel/GPKG/source archives;
- `SOURCE_REGISTER.csv` и SHA-256 receipts;
- snapshot ключевой документации main repository.

Raw source binaries впредь добавляются только туда.

Утверждённые названия остаются прежними:

- Диплом: «Горные и маркшейдерские работы при разработке Верхнекамского месторождения».
- Спецчасть: «Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений».
