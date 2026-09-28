# Квитанции Corpus Platform v0

Публичные копии квитанций (только ID, числа и статусы; без текста корпуса, адресов и путей хоста). Полные
квитанции лежат в `$VKM_DATA_ROOT/receipts/` на CORE и в рабочих каталогах агентов.

| Файл | Что подтверждает |
|---|---|
| `canary_metrics.json` | canary (CP-23): K-01…K-19 — страницы без потерь, идемпотентность, детерминизм layout, аномалии OCR |
| `canary_ocr_grid.json` | сетка конкурентности и dpi GLM-OCR на холодных срезах 044 |
| `canary_scenario_b.json` | сценарий B: выборки CER и доля букв, решения о переOCR встроенных слоёв |
| `canary_cp22_stops.json` | аварийные остановки CP-22 на canary и их разбор |
| `docx_023_render.json` | закреплённый рецепт рендера DOCX 023 (LibreOffice 24.2, профиль шрифтов) |
| `edge_rerank_acceptance.json` | приёмка реранкеров на EDGE 9/9 (§31, §56) и parity m0 |
| `edge_rerank_deploy.json` | развёртывание шлюза и m0: образы, пины, размещение |
| `reconcile_canary.json` | reconcile снимка canary |
| `mcp_acceptance_60_canary.json` | сценарий §60 через MCP (7 шагов) на снимке canary |
| `reconcile_final.json` | reconcile итогового снимка `snap-20260928T113713Z-fa0aa127` (снимок PASS; проекции граф/поиск пересобраны отдельно) |
| `graph_projection_final.json` | итоговая проекция Neo4j: счётчики, проверки C1–C16, дайджест |
| `search_projection_final.json` | итоговая проекция OpenSearch: индексы, счётчики, поток документов |
| `rebuild_demo_final.json` | §46: удаление и пересборка DuckDB, Neo4j и OpenSearch из канона |
| `bibliography_final.json` | списки литературы (CP-38, CP-41): записи, разбор полей, связи с работами, CITES, ручная проверка |
| `late_interaction_encode_final.json` | late interaction (агент L): кодирование mLateOn всех единиц, проверки §64 |
| `late_interaction_deploy.json` | развёртывание late: pack, горячая замена, smoke, латентность, включение по умолчанию |
| `nav_sections.json` | навигационный слой, разделы (агент N1): методы, покрытие страниц, проверки вложенности, ручная выборка |
| `nav_formulas.json` | навигационный слой, формулы (агент N2): номера, блоки «где…», символы, ссылки, параметры-кандидаты, QA |
| `nav_concepts.json` | навигационный слой, понятия (агент N3): термины, связи, сообщества Leiden, CPU/GPU-паритет, ручная проверка 10 тем |
| `nav_deploy.json` | сборка всего навигационного слоя по снимку и выкладка на CORE: датасеты и sha256, образ API, монтирование, smoke API и MCP |
| `topic_benchmark_v1.json` | тематический бенчмарк «от А до Я» (агент Q, [benchmarks/topic_v1](../../../benchmarks/topic_v1/RESULTS_V1.md)): предрегистрация, 117 тем / 351 запрос / 886 страниц-свидетельств, метрики систем, приёмка, причины промахов |
