# Квитанции Corpus Platform v0

Публичные копии квитанций (только ID, числа и статусы; без текста корпуса, адресов и путей хоста). Полные
квитанции лежат в `$VKM_DATA_ROOT/receipts/` на CORE и в рабочих каталогах агентов.

| Файл | Что подтверждает |
|---|---|
| [model_owners_tables_cpu_2026-10-04.json](model_owners_tables_cpu_2026-10-04.json) | Source-bound text/visual owner contracts, table continuation/compatibility и CPU checks; runtime/image/GPU/MCP49 и full corpus admission не выполнены |
| [table_cursor_compat_2026-10-04.json](table_cursor_compat_2026-10-04.json) | Hosted Linux regression сохранён как FAIL; исправлена optional continuation compatibility, 228 targeted PASS каждой ОС и 626 Linux World PASS; missing source не разрешает canonical reads |
| [control_restore_2026-10-04.json](control_restore_2026-10-04.json) | Реальный independent copy/restore 15 control files, 34 470 bytes; source/backup/restore hashes совпали, originals unchanged. Это не full corpus restore и не typed bootstrap backup |
| [search_metrics_2026-09-30.json](search_metrics_2026-09-30.json) | topic/retrieval before-after и controlled component runs на 574daaac; frozen truth сохранена, конфигурация не менялась; новую независимую разметку visual hits владелец отложил до Astra-review ([отчёт](../SEARCH_METRICS_2026-09-30_RU.md)) |
| [platform_integrity_2026-09-30.json](platform_integrity_2026-09-30.json) | read-only smoke22tools/61calls на574daaac; отдельные FAIL/NOT_RUN, без изменений CORE/EDGE |
| [architecture_drawio_2026-09-30.json](architecture_drawio_2026-09-30.json) | обновление девяти схем, JSON sources, SVG и отдельная страница A/B/C; byte-identical rebuild |
| [figure_readings_v2.json](figure_readings_v2.json) |411sourceobjects, GLM bulk, per-record hashes/status, gold accuracy отдельно от agreement; цели99%/97% не объявлены достигнутыми |
| [geometry_skru1_v1.json](geometry_skru1_v1.json) |135B/Cobjects,409GeoJSONlayers, source frames/DXF/UNKNOWN, diagnostic registration; метрическая приёмка открыта |
| [qgis_mcp_2026-09-30.json](qgis_mcp_2026-09-30.json) | реальный PyQGIS/GDAL и stdio15tools, synthetic checks, source archive341layers/207165features;1938invalid topology candidates явно отделены от coordinate preservation |
| [session_verification_2026-09-30.json](session_verification_2026-09-30.json) | финальная проверка world/targeted/QGIS, canonical35PASS/7missingrefs, git diff/leakage; Windows limitations и SKIP сохранены явно |
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
| `bibliography_v3_dryrun.json` | правила `bib_rules_v3` (агент B3): пробный прогон по копии снимка без коммитов — записи без авторов по причинам, доли полей v2 → v3, стабильность ID, размеченная выборка из 100 записей |
| `late_interaction_encode_final.json` | late interaction (агент L): кодирование mLateOn всех единиц, проверки §64 |
| `late_interaction_deploy.json` | развёртывание late: pack, горячая замена, smoke, латентность, включение по умолчанию |
| `retrieval_v1_followups.json` | продолжение V1 (агент L): библиографический маршрут на стенде V1, Q5, `object_label`, формат pack, задание обновления dense + late |
| `nav_sections.json` | навигационный слой, разделы (агент N1): методы, покрытие страниц, проверки вложенности, ручная выборка |
| `nav_formulas.json` | навигационный слой, формулы (агент N2): номера, блоки «где…», символы, ссылки, параметры-кандидаты, QA |
| `nav_concepts.json` | навигационный слой, понятия (агент N3): термины, связи, сообщества Leiden, CPU/GPU-паритет, ручная проверка 10 тем |
| `nav_duplicates.json` | навигационный слой, дубликаты и перепечатки (агент U): виды, первичные копии, пороги, CPU/GPU-паритет, ручная проверка 30 кластеров |
| `nav_object_duplicates.json` | NAV §9 (агент U2): повторы рисунков, таблиц и формул — виды по типам объектов, sha256 датасетов, пороги, тайминги, ручная проверка 30 групп каждого вида (ID и вердикты), прокси полноты, команда полной сборки на CORE |
| `nav_deploy.json` | сборка всего навигационного слоя по снимку и выкладка на CORE: датасеты и sha256, образ API, монтирование, smoke API и MCP |
| `topic_benchmark_v1.json` | тематический бенчмарк «от А до Я» (агент Q, [benchmarks/topic_v1](../../../benchmarks/topic_v1/RESULTS_V1.md)): предрегистрация, 117 тем / 351 запрос / 886 страниц-свидетельств, метрики систем, приёмка, причины промахов |
| `retrieval_v1_h_review.json` | ревью H 60 пул-меток V1: согласие (κ взв. 0,91 / 0,95, α 0,95), 7 расхождений на разбор, протокол слепого ревью |
| `nav_graph.json` | граф NAV в Neo4j (агент G), сухой прогон по полной сборке NAV снимка: число узлов и рёбер по правилам проекции, учёт каждой строки датасетов, preflight P1–P6, ID DOCUMENT сверены с каноном; загрузку и проверки N1–N7 выполняет координатор на CORE |
| `nav_topics.json` | NAV, темы (агент T): векторы и агрегаты разделов, дерево тем 3 уровней без LLM, отсев устаревших векторов, GPU/CPU, QA 20 тем и пяти предметов |
| `topic_dossier.json` | досье темы `reconstruct_topic` (агент D, v2): пакет каталогов (коммит, файлы, строки, sha256); пробный прогон NAV_ONLY и полного пути с перефразами на копии снимка — формулировки, ярусы, счётчики, тайминги, ID разделов и процессов, пробелы по классам; диагностика списка страниц на трудных группах TOPIC_BENCHMARK_V1 (только ID и числа) |
| `cad_v1_smoke.json` | `vkm-cad` v1 на WORKSTATION без GUI: точки → TIN → горизонтали → мульда → профиль → лист → PDF, DXF ↔ DWG, импорт PDF, каналы scr/lisp/csharp, резерв на Python; журнал проб (до и после исправления лицензии, две аварии ядра) |
| `cad_layout_fix.json` | `cad_layout_sheet` в заданиях ACAD (агент CADFIX, 29.09): авария Core Console — `Viewport.On` в транзакции, удалившей видовой экран листа (`AcDbViewport::setIsOn`, консоль C3D переживает); 18 проб с ID прогонов и последними этапами, исправление, проверки обоих путей видового экрана, живые тесты (C3D и ACAD, 28 прогонов OK), инцидент дерева процессов (повторный PID: завершён чужой процесс) и его исправление, выдача листа линии 12 (sha256 PDF) |
| `nav_parameters.json` | NAV §8 (агент P): параметры-кандидаты — счётчики, sha256, итоговая выборка QA по полям (ID и вердикты), прокси полноты по evidence |
| `nav_tables.json` | NAV §10 (агент TB): структурированные таблицы — счётчики, sha256, детерминизм, ручная проверка 40 таблиц (ID и вердикты), параметры до и после (выборка P, новые кандидаты, прокси полноты), команда сборки на CORE |
| `evening_deploy_final.json` | итог ночи 28.09: списки литературы v3, снимок 738eebee, навигационный слой из 19 датасетов, граф NAV, обновление векторов, образы, smoke 38 инструментов MCP |
| `retrieval_v2.json` | бенчмарк V2 (агент V2): предрегистрация, 11 текстовых и 2 визуальные модели на RTX 5070 Ti, проверка 0, пул `LLM_AGENT_V2`, метрики, правило решения, паритет Q8_0 и выполнимость на RX580; ничего не развёрнуто |
| `deploy_20260929_graph_tables.json` | выкладка 29.09 (вечер): граф NAV 1.1 в Neo4j — таблицы, значения параметров, пары словаря, группы дубликатов (127 543 узла, 2 251 414 связей, N1–N11 PASS); 43 инструмента MCP, smoke 45/45; графовые стадии поиска выключены по умолчанию |
| `deploy_20260929_nav_tables_dictionary.json` | выкладка 29.09: таблицы, параметры v2, дубликаты объектов и словарь RU↔EN в NAV снимка 738eebee, перезагрузка графа NAV, образ b39370f; `rerank_text` на mLateOn, текстовый реранкер v3.5 остановлен; smoke MCP 39/39 |
| `mcp_connection_close_deploy.json` | зависания MCP с рабочей станции (закрытие простаивающего соединения не доходит до Windows-клиента): диагностика, `Connection: close` в ответах MCP, смена образа api/mcp/mcp-admin, smoke 40/40 |
| `visual_route_deploy.json` | визуальный маршрут включён 29.09: гейт RX580 PASS (p50 108 мс, p95 144 мс, VRAM 3,4 ГиБ с dense и late), индекс `vkm-pagevis` 26 092 страницы, слот `rx580.json`, smoke: вопросы о рисунках APPLIED, текстовые не задеты |
| `visual_route_v1.json` | визуальный маршрут (агент VIS): visual-артефакт страниц Qwen3-VL (26 092, подпись `ef2f297d…`), индекс `<prefix>-pagevis`, башня запроса F16 + патч 0005 (паритет на CPU, гейт RX580 не запускался), детектор P 0,76 / R 0,864, nDCG@10 визуальных V +0,085, текстовых ≈ 0; пакет развёртывания; ничего не развёрнуто |
| `nav_term_dictionary.json` | NAV, словарь терминов RU ↔ EN (DE), синонимы и аббревиатуры (агент TR): 6 541 пара по методам и отношениям, сиды `REVIEWED_BY_AGENT`, ручная проверка 459 пар (ID и вердикты), sha256 векторов и датасета; измерение TERM_DICTIONARY_V1 (предрегистрация, досье и гибрид, разведочный вариант без поздней стадии) и выбранные значения по умолчанию |
| `figure_digitization_v0.json` | оцифровка графиков (агент FD, [отчёт](../../implementation_work/AGENT_FD_FIGURE_DIGITIZATION.md)): кандидаты, импорт AutoCAD (задание, отказы, время), сверка вершин DXF с вершинами PDF, прогон маршрутов A и B по 320 рисункам и R по 55 растрам (покрытие, согласие, время), витрина и проверки точности, sha256 набора `figure_series`; значений и текста корпуса нет |
| `graph_search_v1.json` | графовые стадии гибридного поиска G1–G5 (агент GS, [benchmarks/graph_search_v1](../../../benchmarks/graph_search_v1/RESULTS.md)): копии, связность раздела, синонимы, цитирования, темы без смены моделей; предрегистрация, выбор на dev, решение на test (сочетание всех пяти: R@50 тем +0,021, Холм 0,046), 30 меток `LLM_AGENT_GS`, задержка, что развернуть; ничего не развёрнуто |
| `nav_graph_tables_dictionary.json` | граф NAV `nav-graph/1.1` (агент G2), сухой прогон по выложенной сборке NAV снимка 738eebee: таблицы (`NavTable`), значения параметров (`ParameterValue`), словарь терминов (рёбра `TRANSLATES_TO` / `SYNONYM_OF` / `ABBREVIATION_OF`, 607 терминов только из словаря), группы повторов (`ObjectDupGroup`, `DUP_MEMBER_OF`) — 127 543 узла и 2 251 414 рёбер (+15 621 / +48 262), учёт каждой строки каждого датасета, термины свойств, ID DOCUMENT сверены с каноном, проверки N8–N11 на данных; инструменты MCP `get_table_structured`, `find_tables`, `copies_of_object`, `shared_formulas`; загрузку выполняет координатор на CORE |
| `chart_models_v1.json` | модели графиков против помощников оцифровки (агент CH, [результаты](../../../benchmarks/chart_models_v1/RESULTS.md)): выбор модели (карточки, числа, ссылки), обслуживание на RTX 5070 Ti (образы и веса по sha256, пик видеопамяти), предрегистрация, 19 рисунков (9 из 33 дополнительных прошли проверку истины наложением), подписи осей Qwen3.5-9B / Granite 4.1 4B / Tesseract, ряды против истины и маршрута R, гибрид, время, разведочные анализы; дополнение `glm_ocr_addendum` — рабочий GLM-OCR конвейера как чтец подписей (кропы маршрута R, весь рисунок, JSON-схема; маршрут R с другим чтецом; поправка к H на R5); значений и текста корпуса нет |
| `topic_pool_labels_t1.json` | пул-разметка TOPIC_BENCHMARK_V1 (агент LB, [RESULTS_POOL_T1](../../../benchmarks/topic_v1/RESULTS_POOL_T1.md)): предрегистрация до первой метки, пул глубины 10 (9 058 пар, 9 040 единиц) размечен целиком (`LLM_AGENT_T1`, CANDIDATE), метки по оценкам и кодам, judged@k, метрики всех систем на истинах V / P / P3, проверки и порядок систем, приёмка, апостериорная диагностика, отступления; ревью H не проведено |
| `nav_figure_series.json` | NAV-часть `figure_series` (агент FD2, [FIGURE_SERIES.md](../FIGURE_SERIES.md)): маршрут A как детерминированная стадия сборки по 1 230 графикоподобным рисункам снимка 738eebee и помеченный растровый набор R — статусы, флаги, выборка без подозрительной калибровки, сравнение с прогоном FD, визуальная проверка (ID и вердикты), sha256 датасетов и двух сборок, репетиция импорта, шаги выкладки на CORE; значений и текста корпуса нет |
| `deploy_20260929_figure_series.json` | развёртывание оцифровки графиков на CORE: часть NAV `figure_series` (агент FD2) импортирована в NAV снимка 738eebee байт в байт (sha256 шести датасетов, проверка канона), api/mcp на образе коммита 9d049dc (45 инструментов чтения), ночные скрипты (smoke-4); MCP smoke 45/45 PASS; откат |
| `figure_readings_v1.json` | машинное чтение сложных рисунков (агент QV, код [benchmarks/figure_readings_v1](../../../benchmarks/figure_readings_v1/)): ВКР Филатовой (VKM-SRC-023, 64 изображения из DOCX в исходном разрешении, EMF через LibreOffice), рисунки корпуса из поиска данных (рендер 200 dpi) и два файла пользователя — классы изображений, Qwen3.5-9B (llama.cpp) и второй чтец таблиц GLM-OCR (режим таблиц конвейера), совпадение по ячейкам, зацикливания и повторы, хэши моделей и промптов, время, пик видеопамяти; все чтения `AUTO_EXTRACTED_UNREVIEWED` / `VLM_EXTRACTED`, значений, подписей и текста нет |
| `intake_2026-09-29.json` | приёмка 29.09 (агент IN, [заметка](../INTAKE_2026-09-29.md)): 20 источников VKM-SRC-252…271 (17 файлов пользователя и 2 статьи CC BY; выпуск журнала разрезан на 2 статьи; 252 — внутренний документ предприятия, в PUBLIC только id и нейтральное описание), SHA-256, конвертация `.doc` → DOCX и выделение страниц с проверками, реестр работ (ред. 3) и изменение атрибуции стр. 1 VKM-SRC-106 (до и после), прогон конвейера (652 страницы, 19 COMPLETE + 1 PARTIAL, сбой по памяти и продолжение), решение по слою CP1251 источника 266 с постраничной проверкой, репетиция снимка на локальной CANONICAL-копии (валидатор PASS), все части NAV (строки против 738eebee), сухой прогон графа NAV, векторы страниц, выкладка на CORE: 29.09 публикация и сверка (снимок `snap-20260929T175107Z-574daaac`, 271 источник, PASS), 30.09 NAV и его граф (N1–N11 PASS), dense + late (209 259, equal), индекс страниц (26 744), перезапуск api/mcp, MCP smoke 45/45 PASS |
## Recovery 04.10.2026: explicit control closure

- [ops_restore_2026-10-04.json](ops_restore_2026-10-04.json): actual independent copy
  и полный restore 37 ранее выявленных operational scripts, 61 367 bytes.
- [encrypted_configs_restore_2026-10-04.json](encrypted_configs_restore_2026-10-04.json):
  authenticated encrypted copy/restore 7 host configs, 70 934 original bytes;
  plaintext и recovery identity на EDGE не передавались. Off-host key storage
  подтверждено владельцем, не проверялось независимо.

Это все 44 кандидата первоначального bounded control inventory. Полный
UNBACKED_UNIQUE inventory, canonical restore и typed bootstrap backup остаются
незавершёнными. [Контракт и ограничения](../../development/FROZEN_RECOVERY_FILESET_RU.md).

Дополнительно сохранены и восстановлены 28 PRIVATE review/provenance records:
[aggregate receipt](review_provenance_restore_2026-10-04.json). Содержимое,
source hashes и private locators этого набора не публикуются; EDGE хранит ciphertext.
Все 28 исходных файлов перепроверены после restore под retained handles.
Это отдельный bounded file set; полный canonical restore и bootstrap qualification
по-прежнему NOT_RUN/NOT_QUALIFIED.

[recovery_docx_cpu_2026-10-04.json](recovery_docx_cpu_2026-10-04.json) содержит
точные hashes public code и synthetic CPU tests для frozen recovery и DOCX
grid/format admission; actual corpus processing и scientific review NOT_RUN.

[originals_restore_2026-10-04.json](originals_restore_2026-10-04.json): actual
encrypted copy и independent authenticated restore 272 зафиксированных R2 originals,
2 122 899 923 bytes, девять batches и отдельный recovery control. Включены три
native DOC companions. Все restored disk hashes и final source guards проверены;
PUBLIC receipt содержит только агрегаты и approved bindings. R1/R3, atomic
current generation, полный UNBACKED_UNIQUE и bootstrap/scientific admission
этой квитанцией не закрываются.

[evidence_versions_restore_2026-10-04.json](evidence_versions_restore_2026-10-04.json):
actual encrypted copy и independent authenticated restore 1 453 captured R1 byte
versions, 149 547 309 bytes, 23 batches и отдельно восстановленный recovery control.
Вместе с 28 точными R0 versions покрыты все 1 481 записи выбранного набора.
Это фиксированные expected bytes физических mutable файлов; atomic generation,
весь UNBACKED_UNIQUE и bootstrap/scientific admission остаются не доказанными.
Receipt содержит только whitelist-агрегаты и approved bindings, без private paths,
индивидуальных source hashes, payload и ключа.

[native_owner_cpu_2026-10-04.json](native_owner_cpu_2026-10-04.json): точные hashes
PUBLIC native placement/owner source и synthetic CPU JUnit. Declared platform
NOT_RUN отделены от PASS; selections пересекаются и не суммируются. Проверены
optional text → actual late owner, узкий status gate и обе actual ApiService factory
передачи. Cached image/model/inference/full 49 tools и production switch NOT_RUN.

[native_owner_hosted_ci_2026-10-04.json](native_owner_hosted_ci_2026-10-04.json):
все три hosted jobs exact commit `bd81589` завершились SUCCESS. World 648 PASS;
Linux corpus 3832 PASS / 2 NOT_RUN; Windows 2779 PASS / 134 NOT_RUN. Исключённые
runtime selections учтены отдельно. Квитанция не распространяется на параллельный
незакоммиченный first-LIVE код, actual model load, 49-tool acceptance или switch.

[edge_owner_image_2026-10-04.json](edge_owner_image_2026-10-04.json): actual EDGE
software image из exact `bd81589`, 472 raw Git wheel members, pinned inputs и
проверенного native cache. Сборка завершилась с exit 0 за 47,663 s; реальные
cgroup limits и ABI проверены. Actual model load/inference, 49 tools и switch
остаются NOT_RUN. Старый сервис этой сборкой не останавливался.

[edge_owner_shadow_attempt_2026-10-04.json](edge_owner_shadow_attempt_2026-10-04.json):
actual SHADOW attempt FAILED после успешного CUDA preflight. Loaded identity,
placement и inference не получены; стадия отказа UNRESOLVED. Первичный fallback
восстановил старый retained container; main agent отдельно подтвердил exact
container/image, новый PID и native health 200. Ошибка повторной recovery сохранена
как FAILED, не заменена PASS. CORE/LIVE/full49 не выполнялись.

[first_live_protective_cpu_2026-10-05.json](first_live_protective_cpu_2026-10-05.json):
164 PASS Windows и 164 PASS Linux, независимо повторены main agent по exact LF
source closure. Опасный Compose-create удалён; production блокируется до effects.
Статус FIRST_LIVE_NOT_READY: требуется отдельный безопасный native-create adapter
и actual runtime qualification. Synthetic PASS не доказывает сохранность originals.

[edge_owner_e0_diagnosis_2026-10-05.json](edge_owner_e0_diagnosis_2026-10-05.json):
read-only диагностика попытки 04.10 (docker inspect, procfs, loopback health,
SHA-256 throughput; никаких container effects). Повторная recovery: механизм
воспроизведён на точном коде v4 — Docker отдаёт retained Mounts в недетерминированном
порядке (27/200), v4 сравнивал упорядоченный список (29/200 ValueError); наиболее
вероятная причина, исправлено в контроллере v5. Внешний waiter ждал 55 с после exit
candidate. Startup exit 2: по таймингу наиболее вероятен дедлайн 180 с доказательства
загрузки при живом child; конкретный fence NOT_ESTABLISHED — его покажет bounded
диагностика нового образа. GPU drill, model load и switch этим receipt не выполнялись.

[edge_owner_shadow_attempt_2026-10-05.json](edge_owner_shadow_attempt_2026-10-05.json):
повторная bounded SHADOW попытка на образе с диагностикой (commit `5ee07e4`) и
контроллере v5. FAILED: candidate exit 2 через 185.7 с, но теперь стадия
**наблюдена**: listener поднят за ~150 мс, затем witness endpoint 4579 раз отвечал
503 до дедлайна 180 с, child жив, лишних mapped-библиотек нет. Корень выведен из
точного upstream `4da6337`: CPU-веса через mmap получают buffer type с device NULL,
нативный witness getter требует device и отвечает 503. Не исключено: незавершённая
загрузка с тем же кодом 503. Исправленный контроллер обнаружил exit сразу, вернул
exact retained OLD (новый PID, health 200, простой 187 с); повторная recovery —
verified no-op. Identity/placement/inference не получены; LIVE/switch NOT_RUN.

[first_live_engine_create_cpu_2026-10-05.json](first_live_engine_create_cpu_2026-10-05.json):
L0 — узкий фиксированный Engine-create adapter (allowlist операций Engine API,
профиль из approved полей без значений Env, post-create inspect до start,
write-ahead journal, lost-ACK без повторного create, удаление только journal-owned
ID, проверки daemon/binding до парковки оригиналов, Compose не вызывается).
388 PASS Windows и 388 PASS Linux по JUnit; независимый challenger — 2 раунда,
открытых MUST_FIX нет. FIRST_LIVE_NOT_READY: L1 (реальный Engine) NOT_RUN,
activation невозможна до замены Compose-proof.

[edge_owner_shadow_attempt_2026-10-05_v7.json](edge_owner_shadow_attempt_2026-10-05_v7.json):
диагностический bounded drill v7 (commit `7be32d2`, классификация тела 503). Наблюдено:
сервер отвечал `Loading model` только ~1 с после spawn (загрузка модели завершается),
затем 4521 ответ нативного witness getter `loaded_witness_unavailable` до дедлайна.
Класс причины — исключение getter — **наблюдён**; место броска (device NULL у
mmap CPU buffer) выведено из исходника. OLD восстановлен контроллером (health 200),
повторная recovery — verified no-op. LIVE/switch NOT_RUN.
