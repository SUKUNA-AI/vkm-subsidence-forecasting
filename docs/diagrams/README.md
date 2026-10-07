# Схемы проекта (draw.io)

Технические схемы проекта ВКМ / СКРУ-1. Каждая схема собрана из спецификации `specs/<имя>.spec.json` инструментом
`vkm_drawio`: спецификация → `.drawio` → `.svg` (экспорт draw.io Desktop, модель встроена в SVG). Все числа и связи
взяты из документов, кода, каталогов и квитанций репозитория. Источники указаны в подвале каждой схемы и в таблице ниже.
Схемы — производная документация: они не заменяют ни evidence, ни канон.

## Схемы

| Схема | Что показывает | Основные источники |
|---|---|---|
| [science_chain](science_chain.svg) | научная цепочка после reset 26.09 и решений D-18/D-19: корпус → evidence → WorldSpec → PhysicalWorld → представления (Ansys, OGS, World-0) → ObservationWorld → нейросеть → предрегистрированная проверка | [CLAUDE.md](../../CLAUDE.md), [PHASE1_DESIGN_DECISIONS_RU.md](../governance/PHASE1_DESIGN_DECISIONS_RU.md), [WORLD_KERNELS_DESIGN_RU.md](../worldspec/WORLD_KERNELS_DESIGN_RU.md) |
| [data_layers](data_layers.svg) | слои научной системы данных L0…L4 и NAV: что где живёт, статусы объектов, гейт AUTO ≠ REVIEWED, логические графы Neo4j | [README.md](../../README.md) §2, [THEORY_TO_SCIENTIFIC_DATA_SYSTEM_CONTRACT_V0_2_RU.md](../theory/THEORY_TO_SCIENTIFIC_DATA_SYSTEM_CONTRACT_V0_2_RU.md), [schema.py](../../src/vkm_corpus/graph/schema.py) |
| [platform_hosts](platform_hosts.svg) | хосты WORKSTATION, CORE, EDGE и их сервисы на 07.10 (только роли, без адресов) | [CORPUS_PLATFORM_ARCHITECTURE.md](../corpus_platform/CORPUS_PLATFORM_ARCHITECTURE.md), [DEPLOYMENT.md](../corpus_platform/DEPLOYMENT.md), [evening_deploy_final.json](../corpus_platform/receipts/evening_deploy_final.json) |
| [platform_data_flow](platform_data_flow.svg) | поток данных: реестр → STAGING → reconcile → проекции, векторы (lab_refresh), NAV, пакет каталогов → VKM API → MCP | [OPERATIONS.md](../corpus_platform/OPERATIONS.md), [NAVIGATION_LAYER.md](../corpus_platform/NAVIGATION_LAYER.md) |
| [document_pipeline](document_pipeline.svg) | путь документа: фазы prepare / visual / ocr / commit, маршруты страниц, PP-DocLayoutV3, GLM-OCR, стоп CP-22, импорт слоя OCR v2 (PADDLEOCR_VL), публикация и снимок 2d71e9e8 | [PIPELINE.md](../corpus_platform/PIPELINE.md) |
| [hybrid_search](hybrid_search.svg) | поиск: BM25 + dense jina-v5-nano + RRF + late mLateOn, маршрут библиографии, page visual retrieval на CORE, visual rerank m0 на EDGE, измеренные числа | [README.md](../../README.md), [MODEL_SERVICES.md](../corpus_platform/MODEL_SERVICES.md), [RESULTS_V1.md](../../benchmarks/retrieval_v1/RESULTS_V1.md) |
| [nav_layer](nav_layer.svg) | навигационный слой: N1–N11, их входы и зависимости, сборка на WORKSTATION → nav.duckdb → NavStore и граф NAV, числа снимка 2d71e9e8 | [NAVIGATION_LAYER.md](../corpus_platform/NAVIGATION_LAYER.md) |
| [graph_schema](graph_schema.svg) | схема графа Neo4j: метки и типы рёбер слоёв DOCUMENT и NAV (`nav-graph/1.1`: таблицы, значения параметров, словарь терминов, повторы объектов), рёбра между слоями | [schema.py](../../src/vkm_corpus/graph/schema.py), [nav_schema.py](../../src/vkm_corpus/graph/nav_schema.py) |
| [topic_dossier](topic_dossier.svg) | досье `reconstruct_topic`: формулировки → RRF → два яруса → разделы, формулы, понятия, источники, каталоги, UNKNOWN; бюджет и конверт | [NAVIGATION_LAYER.md](../corpus_platform/NAVIGATION_LAYER.md) §5, [MCP_TOOLS.md](../corpus_platform/MCP_TOOLS.md) |
| [mcp_servers](mcp_servers.svg) | MCP-серверы vkm-corpus (49, в том числе журнал evidence), vkm-corpus-admin, vkm-cad (27), vkm-drawio(8), локальный vkm-qgis(15) и группы инструментов | [MCP_TOOLS.md](../corpus_platform/MCP_TOOLS.md), [README.md](../../README.md) |
| [causal_subsidence](causal_subsidence.svg) | причинная цепь оседаний: группы процессов PC-01…PC-72, ветви закладки, гидро и термо, ключевые рёбра CE-xxx причинного графа | [physics_coverage_and_execution_matrix.csv](../../catalogues/physics/physics_coverage_and_execution_matrix.csv), [causal_graph_edges.csv](../../catalogues/causal/causal_graph_edges.csv), [CAUSAL_GRAPH_RU.md](../science/CAUSAL_GRAPH_RU.md) |
| [initial_stress](initial_stress.svg) | гипотезы начальных напряжений IS-H, значения λ по методам и школам, сценарии IS-SC-A…E, UNKNOWN для СКРУ-1 | [INITIAL_STRESS_RU.md](../science/topic_dossiers/INITIAL_STRESS_RU.md) |
| [backfill_mechanics](backfill_mechanics.svg) | механика закладки PC-37…PC-40 в цепи оседаний: известно, оспаривается, неизвестно; гипотезы DB-H и сценарные диапазоны DB-S | [BACKFILL_MECHANICS_RU.md](../science/topic_dossiers/BACKFILL_MECHANICS_RU.md) |
| [solver_ladder](solver_ladder.svg) | лестница решателя из 10 ступеней и роли Ansys, OGS + MFront, MATLAB, Civil 3D, PyTorch, gprMax с текущим статусом | [CLAUDE.md](../../CLAUDE.md), [README.md](../../README.md), [PHASE2_PLAN_OCT_NOV_2026_RU.md](../planning/PHASE2_PLAN_OCT_NOV_2026_RU.md) |
| [civil3d_pipeline](civil3d_pipeline.svg) | конвейер Civil 3D в vkm-cad: таблица точек → COGO → TIN → горизонтали → мульда → профиль → лист → PDF; решение CP-43 | [MCP_TOOLS.md](../corpus_platform/MCP_TOOLS.md) §4.1, [AGENT_CAD_V1.md](../implementation_work/AGENT_CAD_V1.md) |
| [benchmarks](benchmarks.svg) | бенчмарки retrieval_v0 → v1 → v2 → topic_v1: постановка, главные числа, решения | [README.md](../../README.md), [benchmarks/](../../benchmarks/) |
| [phase2_timeline](phase2_timeline.svg) | план Phase 2 на октябрь–ноябрь: периоды, работы, гейты, решения пользователя и риски | [PHASE2_PLAN_OCT_NOV_2026_RU.md](../planning/PHASE2_PLAN_OCT_NOV_2026_RU.md), [WORLD_KERNELS_DESIGN_RU.md](../worldspec/WORLD_KERNELS_DESIGN_RU.md) §9 |

## Цвета

Палитра одна для всех схем; легенда каждой схемы уточняет значение цвета для неё.

| Цвет | Значение по умолчанию |
|---|---|
| зелёный | источник, evidence, реальные наблюдения |
| голубой | производное: канон, проекции, NAV, индексы (AUTO_EXTRACTED_UNREVIEWED / DERIVATION) |
| оранжевый | модель, мир, сценарий, синтетика (MODEL_CHOICE) |
| жёлтый | сервис, инструмент, шаг процесса |
| красный | UNKNOWN, пробел, конфликт, гипотеза |
| серый пунктир | приостановлено, в работе или будущее |
| сиреневый | агент Claude |

## Как пересобрать

```bash
python -m vkm_drawio.cli create --root public --path <имя>.drawio --spec docs/diagrams/specs/<имя>.spec.json --overwrite
python -m vkm_drawio.cli export --root public --path <имя>.drawio --format svg --overwrite
```

Тест `tests/corpus/test_drawio_xml.py` сверяет каждый `.drawio` с пересборкой из спецификации побайтно. Правила корня
`public` ([MCP_TOOLS.md](../corpus_platform/MCP_TOOLS.md) §5): без машинных путей, адресов, секретов и встроенных
растров.

30.09.2026: девять актуальных платформенных схем обновлены через vkm-drawio; источники specs синхронизированы и проходят byte-identical rebuild. Страница A/B/C отдельно: [SVG](platform_data_flow_abc.svg), editable page2 в [drawio](platform_data_flow.drawio). Массовый OCR далее GLM, Qwen retained comparison; source geometry не повышается до evidence. Solver/ML остаются на паузе. [Отчёт](../corpus_platform/WORK_SESSION_FIGURES_GEOMETRY_2026-09-30_RU.md).

07.10.2026: восемь схем (путь документа, поток данных, хосты, NAV, MCP-серверы, гибридный поиск, слои данных, схема графа) переведены на снимок OCR v2 `snap-20261007T103222Z-2d71e9e8`; источники чисел — квитанции [deploy_20261006_ocr_v2_core.json](../corpus_platform/receipts/deploy_20261006_ocr_v2_core.json) и [deploy_20261007_readd_053_230.json](../corpus_platform/receipts/deploy_20261007_readd_053_230.json).
