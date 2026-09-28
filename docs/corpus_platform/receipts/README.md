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
| `topic_dossier.json` | досье темы `reconstruct_topic` (агент D): пакет каталогов (коммит, файлы, строки, sha256); пробный прогон NAV_ONLY и полного пути на копии снимка — счётчики, тайминги, ID разделов и процессов, пробелы по классам |
