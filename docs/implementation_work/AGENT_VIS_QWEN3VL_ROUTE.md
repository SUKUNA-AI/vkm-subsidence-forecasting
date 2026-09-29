# AGENT VIS — визуальный маршрут поиска: векторы изображений страниц Qwen3-VL-Embedding-2B

Дата: 29.09.2026. Ветка `claude/agent-vis-qwen3vl-route-2026-09-29` (от `claude/corpus-platform-v0-2026-09-28`, база
`649253e`). Решение пользователя: строить маршрут по итогам V2 ([RESULTS_V2.md](../../benchmarks/retrieval_v2/RESULTS_V2.md)
§4–§6, §10). Квитанция — [visual_route_v1.json](../corpus_platform/receipts/visual_route_v1.json), оценка —
[visual_route_v2.json](../../benchmarks/retrieval_v2/visual_route_v2.json). **На CORE и EDGE ничего не развёрнуто и не
записано**: всё ниже — код, артефакты рабочей станции и команды для координатора (§7).

## 0. Итог

| Задача | Статус |
|---|---|
| 1. Векторы страниц + формат артефакта + индекс | формат и индекс готовы; артефакт собран из векторов страниц V2 (26 092 страницы, 198 MiB); **новое кодирование снимка `…738eebee` не выполнено** — классификатор разрешений отклонил чтение данных корпуса (копия DuckDB с CORE и STAGING производителя), §2 |
| 2. Башня запроса на RX580 (F16 GGUF, гейт, латентность) | слот, патч llama.cpp 0005, эталон и гейт готовы; служебный путь проверен на CPU (cos ≥ 0,999997 к fp32); **гейт на RX580 не запускался** (нужна запись на CORE), §4 |
| 3. Маршрут в сервисе (детектор, RRF, трасса, API, MCP, CLI) | готово, выключатель `VKM_HYBRID_VISUAL_ROUTE` (по умолчанию выключен), §5 |
| 4. Оценка на стенде V2 | визуальные V +0,085 (p 0,039), P +0,043 (p 0,072); текстовые V +0,001, P −0,003, без маршрута Δ = 0; детектор P 0,76 / R 0,864, §6 |
| 5. Пакет развёртывания | команды — §7; файлы — рабочий каталог VIS на WORKSTATION, суммы в `deploy/SHA256SUMS` |

## 1. Что построено

| Файл | Что |
|---|---|
| `src/vkm_corpus/search/intent.py` | `visual_intent`: слова-картинки RU/EN, ложные друзья исключены; зафиксирован в `00e1b84` до оценки |
| `src/vkm_corpus/embeddings/specs.py`, `tokenize.py`, `signature.py` | спецификация `qwen3-vl-emb-2b` (семейство `visual`, шаблон запроса), ids из шаблона чата; необязательные поля подписи `image` и `template` (пустые не меняют ни одну прежнюю подпись) |
| `src/vkm_corpus/embeddings/page_images.py` | visual-артефакт страниц: конфигурация, список «что кодировать», пути превью, проверка sha256 файла, цикл записи вокруг любого `encode(images)` |
| `src/vkm_corpus/embeddings/visual_gate.py` | гейт RX580 для башни: паритет с эталоном, ранжирование по страницам, VRAM (fdinfo), p50/p95, вердикт |
| `src/vkm_corpus/retrieval_service/{config,encoders,app,residency}.py` | роль слота `visual`, `/embed/query` с `role = visual`, размещение слота `placement: cpu` (запасной вариант) |
| `src/vkm_corpus/search/page_vectors.py` | проекция `<prefix>-pagevis`: проверки §64 против CURRENT, сборка, смена алиаса, откат, точный и HNSW-поиск страниц |
| `src/vkm_corpus/search/hybrid.py`, `api/{app,service,backends,topic}.py`, `mcp/servers.py`, `search/cli.py` | маршрут, поле `visual_route`, трасса и стадия, статус, CLI `build-page-vectors`, `rollback-page-vectors`, `hybrid --visual-route`, `hybrid-smoke --expect-route visual` |
| `infra/core/rx580/patches/0005-qwen3vl-embeddings-no-lm-head.patch` | llama.cpp: в контексте эмбеддингов qwen3vl не считает lm_head |
| `infra/core/rx580/rx580.example.json` | пример слота `visual` |
| `infra/workstation/visual_route/*.py` | артефакт из векторов V2; эталон гейта; кодирование недостающих страниц на RTX |
| `benchmarks/retrieval_v2/scripts/visual_route_v2.py` | оценка маршрута по опубликованным метрикам V2 |

## 2. Векторы страниц и формат артефакта

Формат — уже заложенный в `vkm_corpus.embeddings.artifacts` вид `visual` (§43 постановки), без нового хранилища:

```
derived/embeddings/visual/Qwen__Qwen3-VL-Embedding-2B/<revision>/<config-hash>/
    config.json                      EmbeddingConfig: mode visual, text_rule vkm-page-preview-v1, image{…}
    part-<writer>-NNNNN.parquet      object_id = page_id, text_hash = artifact_sha = sha256 превью, vector float32[2048]
    _manifest-<writer>.json          части, строки, sha256 (пишется последним)
    artifact_receipt.json            источник, проверки, размер
```

- Вход страницы — сохранённое `PAGE_PREVIEW` (JPEG, длинная сторона 1024 px) как есть. Его адрес `sha256:<hex>`
  (`preview_artifact_id`) — `text_hash` строки. Сменилось превью — новый хэш, страница кодируется заново (§46).
- Подпись: модель, ревизия, sha256 весов и токенизатора, `quantization BF16`, `pooling last`, L2, инструкция документа
  модели по умолчанию, `image.preprocessor_config_sha256`, `backend` = sentence-transformers CUDA bf16 (RTX 5070 Ti).
  Подпись `ef2f297d…`.
- Собран из векторов V2 (`page_artifact_from_v2.py`): 26 092 страницы снимка `…5d669f09`, 51 часть, 198 MiB (zstd),
  проверка §64 OK. Скорость кодирования V2 — 9,82 стр./с на RTX 5070 Ti, 26 092 страницы ≈ 44 мин.
- **Почему не новое кодирование.** Для снимка `snap-20260928T193550Z-738eebee` нужен список страниц с превью. Классификатор
  разрешений отклонил и копирование DuckDB с CORE («PII Data Handling»), и чтение STAGING на рабочей станции
  («Production Reads»). По правилу этот путь остановлен. В индексе страниц CORE 26 483 страницы — столько же, сколько
  в снимке V2 (26 092 с превью + 391 EPUB), но совпадение превью по хэшам проверяется только на CORE:
  `search build-page-vectors --plan-only` сверяет каждую строку со страницами CURRENT и при расхождении пишет
  `--missing-out` (ID и хэши, без текста) для `encode_pages.py`. Это кодирование читает STAGING — нужно согласие
  пользователя.

## 3. Индекс векторов страниц: OpenSearch k-NN

Выбран OpenSearch, а не memmap в сервисе RX580.

- Одна векторная запись на страницу — та же форма, что у dense-индекса.
- Фильтры страниц гибридного поиска (источник, работа, область, годы, `available_until`) применяются внутри поиска, а не
  после top-N. Схлопывание дублей — по `dup_group_id`.
- Получаем алиас, откат и `search status` так же, как у dense.
- API уже ходит в OpenSearch. Сервис RX580 для этого канала остаётся только кодировщиком запроса.
- Pack (memmap) нужен late-стадии потому, что MaxSim по матрицам токенов OpenSearch не умеет. Для одного вектора
  страницы такой причины нет.

Индекс `<prefix>-pagevis-m1-<build>`, алиас `<prefix>-pagevis`. `knn_vector` на 2048 измерений, inner product (векторы
L2), HNSW lucene как у dense (OpenSearch на CORE — 3.8.0, Lucene 10.5, лимит lucene 16 000 измерений). Поиск по
умолчанию **точный**: `script_score` + `knn_score` по отфильтрованным страницам, как точный косинус V2. `hnsw` —
переключатель `VKM_HYBRID_VISUAL_SEARCH`. Оценка размера: 26 092 × 2048 × 4 Б ≈ 214 MB векторов плюс граф. Число на
CORE не измерено.

## 4. Башня запроса

| Проверка | Результат |
|---|---|
| ids сервиса (`SpecTokenizer`, шаблон спецификации) = шаблон чата модели + `<\|endoftext\|>` | 189 / 189 |
| служебный путь (`QueryEncoder` → llama-server CPU, F16) ↔ векторы F16 V2 | cos 1,0 (мин. 0,9999998) |
| F16 GGUF ↔ fp32-эталон (sentence-transformers fp32), гейт на CPU-заместителе | cos ср. 0,9999988, мин. 0,9999970; top-10 по 26 092 страницам 0,9995, top-50 0,9992, Spearman 0,99992 |
| патч 0005 (без lm_head) ↔ без патча | векторы побитно равны; CPU p50 138,7 → 117,5 мс, p95 193 → 165 мс |
| латентность CPU (i7-14700KF, 16 / 8 потоков) | p50 118 / 131 мс, p95 166 / 195 мс (49 токенов в среднем, макс. 113) |
| fp32-эталон ↔ bf16 GPU (V2) | cos ср. 0,99975, мин. 0,99945 — то же, что F16 GGUF ↔ bf16: расхождение V2 — шум bf16 эталона |

**RX580 не измерялся.** Гейту нужны GGUF и эталон на CORE — это запись, а задача её запрещает. Гейт готов:
`python -m vkm_corpus.embeddings.visual_gate`. Пороги зафиксированы до запуска:
- cos ≥ 0,999 (мин. ≥ 0,995);
- top-10 и top-50 ≥ 0,90, Spearman ≥ 0,95;
- VRAM устройства вместе с резидентными dense и late ≤ 7 168 MiB;
- p95 ≤ 1 000 мс.

Оценки, не измерения:

- **VRAM.** Веса без эмбеддингов ≈ 2 694 MiB. Благодаря `-ot token_embd\.weight=CPU` и патчу 0005 связанная копия
  выхода (593 MiB) не попадает в VRAM. KV при `-c 2048` ≈ 224 MiB, compute ≈ 0,1–0,3 GiB. Слот ≈ 3,0–3,2 GiB, устройство
  с nano (224) и mLateOn (188) — ≈ 3,5 GiB из 8.
- **Латентность.** По матрице K Qwen3-0.6B в F16 на RX580 даёт p50 138 мс (у GCN4 нет fp16-арифметики, F16 в 5 раз
  медленнее Q8_0). У башни в 3,2 раза больше параметров без эмбеддингов → ожидаемо ≈ 0,4–0,9 с. Порог p95 1 с может не
  выдержать.

Альтернативы, если гейт не пройдёт:
1. **CPU CORE** (Ryzen 7 5800X, 16 потоков, нагрузка ≈ 0) — тот же GGUF в слоте с `"placement": "cpu"`. Нужен
   `mem_limit` ≥ 8g у `rx580-retrieval`: 3,3 GB весов в RAM. Ожидаемо p50 ≈ 0,2–0,3 с.
2. **Q8_0 на RX580** — тот же гейт с `--gguf …Q8_0.gguf --quant Q8_0`. На CPU паритет не прошёл (0,9979), но CPU
   квантует и активации. На Vulkan отклонение меньше: у nano 0,99967 на CPU против 0,99982 на RX580.
3. **EDGE GTX 1650** — нет: после реранкеров свободно 186 MiB.

## 5. Маршрут в сервисе

- **Детектор** (`visual_intent`) — правила без модели, решение перечисляет признаки. Хватает одного слова:
  - рисунок / рис. / иллюстрация / изображение;
  - схема / чертёж / эскиз;
  - карта / картограмма / планшет / выкопировка;
  - план, разрез;
  - график / диаграмма / гистограмма / номограмма / эпюра;
  - профиль / профильный;
  - радарограмма;
  - фото / снимок;
  - таблица / табл.;
  - изолинии, интерферограмма;
  - геологическая или стратиграфическая колонка;
  - EN-эквиваленты.

  Исключены «технологическая карта», «план мероприятий / развития…», «фотограмметрия», «картина», «картирование»,
  «профилактика», «площадь сечения». Явное `visual_route = true/false` решает вместо детектора.
- **Статусы** `stages.visual_route.status`:
  - `APPLIED`;
  - `NOT_DETECTED`;
  - `OFF` (`visual_route = false`);
  - `DISABLED` — сервер не включил маршрут; при `visual_route = true` это `DEPENDENCY_UNAVAILABLE`;
  - `SKIPPED_NO_PAGE_KIND`;
  - `SKIPPED_LATE_OFF` — измерена E с late, как у L.
- **Схема.** Страницы E (после late) → top-N. Башня запроса на RX580 (`/embed/query`, `role = visual`) → точный поиск
  по `<prefix>-pagevis` с теми же фильтрами → top-N страниц, дубли схлопнуты. Затем RRF(k = 60) этих двух списков,
  N = `candidates` (100): это E+VIS V2. Слитые страницы занимают позиции PAGE в выдаче; другие виды сохраняют свои
  (H-44); страницы сверх позиций PAGE идут в конец.
- **Трасса.**
  - У страницы: `e_rank`, `vis_rank`, `vis_score` (косинус), `visual_rrf_score`, `final_rank`.
  - У найденной только по изображению: `fused_rank = null`, `index`/`build_id` индекса страниц.
  - Стадия: модель, подпись запроса, сборка, снимок, `new_pages`.
  - Тайминги: `visual_embed`, `visual_knn`.
  - `route`: `visual` или `bibliographic+visual`.
- **Отказы громкие** (включён маршрут):
  - нет слота — `DEPENDENCY_UNAVAILABLE`, стадия `hybrid_visual_embed`;
  - нет индекса — `hybrid_visual`;
  - чужая модель или размерность — `DEPENDENCY_ERROR`.
- **Выключатель.**
  - `VKM_HYBRID_VISUAL_ROUTE=1` в окружении `api`; по умолчанию выключен — в коде и в `.env`.
  - `/v1/status` показывает `visual_route` и `opensearch.page_vectors`.
  - Досье темы (`reconstruct_topic`) передаёт `visual_route = false`: оно собирается из текстовых единиц.

## 6. Оценка (стенд V2)

Детектор зафиксирован коммитом `00e1b84` **до** расчёта ниже. Маршрут для запроса берёт метрики E+VIS, если детектор
сработал, иначе — метрики E. Метрики по запросам взяты из опубликованного `results_v2.json`. Проверка 0: Δ E+VIS − E,
пересчитанная из них, совпала с V2 (0,1086 / 0,0596 / −0,0432 / −0,0935). Вариант `~f16` — векторы башни F16 GGUF,
тот GGUF, что обслуживает слот; служебный путь воспроизводит их с cos 1,0.

**Детектор на 189 запросах V0/V1** (истина — трек запроса):

| Набор | TP | FP | FN | Точность | Полнота |
|---|---|---|---|---|---|
| все 189 | 38 | 12 | 6 | 0,760 | 0,864 |
| покрытые 186 | 36 | 12 | 6 | 0,750 | 0,857 |

- **Пропуски (6)** — визуальные запросы без слова-картинки: «кривые ползучести…», «временные ряды смещений…»,
  «конечно-элементная сетка…», «установка георадара…», «районирование шахтного поля…», «двухходовая камера…».
- **Ложные срабатывания (12)** — текстовые запросы со словом-картинкой: «диаграмма деформирования», «профиль
  оседаний», «планы горных работ», «таблицы интегралов», «снимки со спутника», «интерферограммы»…

Детектор после оценки **не подстраивался**. Перечень — в JSON; тест закрепляет эти решения.

**nDCG@10** (E → маршрут F16; Δ, p, 95 % ДИ):

| Трек | Набор | E | маршрут | Δ | p | ДИ | оракул (E+VIS на всём треке) |
|---|---|---|---|---|---|---|---|
| визуальный (42) | V | 0,349 | 0,434 | **+0,085** | 0,039 | [+0,006; +0,161] | 0,456 (+0,107) |
| визуальный (42) | P | 0,580 | 0,623 | +0,043 | 0,072 | [−0,001; +0,088] | 0,638 (+0,058) |
| текстовый (144) | V | 0,3335 | 0,3345 | +0,001 | 0,88 | [−0,011; +0,014] | — |
| текстовый (144) | P | 0,708 | 0,705 | −0,003 | 0,39 | [−0,009; +0,002] | — |

- **Визуальный трек.**
  - Маршрут сохраняет 80 % прироста V (+0,085 из +0,107 оракула).
  - На направленных запросах (36): V +0,099 (p 0,040), P +0,050 (p 0,069).
  - R@50 на P +0,073 (p 0,0001).
  - Прирост P положителен, но ДИ касается нуля.
- **Текстовый трек.** На 132 ненаправленных запросах Δ = 0 точно (по построению). 12 ложно направленных: V +0,012
  (6/6 побед/поражений), P −0,032 (5/7), оба незначимо. Для сравнения, слияние для всех: −0,045 V и −0,095 P.
- **bf16-векторы запросов** (как в V2) дают то же: визуальный V +0,087 (p 0,036), текстовый P −0,003.

## 7. Пакет развёртывания (координатор; команды из каталога compose на CORE, если не сказано иное)

Обозначения: `<C>` — коммит слияния ветки, `<C12>` — его 12 знаков; `<data>` — корень данных; `<rx580>` —
`VKM_RX580_ROOT`; `<vis>` — рабочий каталог VIS на WORKSTATION (`~/vkm/work/vis`); `<art>` =
`derived/embeddings/visual/Qwen__Qwen3-VL-Embedding-2B/9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda/ef2f297d721747a7598140193ea906403089b6a3f0bc1d6a62ca9a34d891c69f`.

1. **Перенос с WORKSTATION** (владелец и права — как у соседних `dense/`, `multivector/` и `models/*`):
   - `<vis>/data/<art>/` → `<data>/<art>/` (54 файла, 198 MiB; суммы частей — в `_manifest-rtx5070ti-v2.json`);
   - `<vis>/deploy/models/qwen3-vl-emb-2b/` → `<rx580>/models/qwen3-vl-emb-2b/`: GGUF F16 3,4 GB, `hf/` токенизатор,
     `gate_ref/` 110 MB; проверка на CORE: `cd <rx580> && sha256sum -c <копия deploy/SHA256SUMS>`.
2. **Образы из дерева `<C>`** (контекст — корень репозитория; llama.cpp пересобирается из-за патча 0005):
   - `docker build -f infra/core/rx580/Dockerfile --target runtime -t vkm-rx580-llama:4da6337767f9-p5-vk infra/core/rx580`
   - `docker build -f infra/core/rx580/Dockerfile.service --build-arg LLAMA_IMAGE=vkm-rx580-llama:4da6337767f9-p5-vk -t vkm-rx580-retrieval:0.1.4-<C12> .`
   - `docker build -f infra/core/api/Dockerfile -t vkm-corpus-api:0.1.0-<C12> .`
3. **Гейт RX580** (одноразовый контейнер нового образа; сервис не трогается, dense и late остаются резидентными):
   ```
   VKM_RX580_IMAGE=vkm-rx580-retrieval:0.1.4-<C12> docker compose --profile jobs run --rm -T --entrypoint python \
     rx580-embed-worker -m vkm_corpus.embeddings.visual_gate run --key qwen3-vl-emb-2b \
     --gguf /models/qwen3-vl-emb-2b/qwen3-vl-emb-2b-F16.gguf --quant F16 --tokenizer-dir /models/qwen3-vl-emb-2b/hf \
     --ref /models/qwen3-vl-emb-2b/gate_ref --out /cache/visual_gate/f16.json --save-vectors \
     --ctx 2048 --ubatch 512 --parallel 2 --threads 2 --extra-arg=-ot --extra-arg='token_embd\.weight=CPU'
   ```
   Код выхода 0 = PASS (3 = FAIL). В квитанции `/cache/visual_gate/f16.json` — паритет, `resident_after_load`
   (VRAM процесса и устройства, RSS), `query_latency_sequential` (p50/p95). **FAIL по латентности** → тот же запуск с
   `--extra-arg=-ngl --extra-arg=0 --extra-arg=--device --extra-arg=none --threads 8` (CPU CORE) и/или с Q8_0
   (§4); в слот — вариант, прошедший гейт.
4. **Слот в `rx580.json`** (резервная копия рядом): добавить
   ```
   {"role": "visual", "key": "qwen3-vl-emb-2b", "gguf": "/models/qwen3-vl-emb-2b/qwen3-vl-emb-2b-F16.gguf",
    "gguf_sha256": "b1074096f2103ada5ed9ea7f1164384ded4198c645bd6d813e53270ccb1f48e1", "quant": "F16",
    "tokenizer_dir": "/models/qwen3-vl-emb-2b/hf",
    "tokenizer_sha256": "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a", "port": 18303,
    "ctx": 2048, "ubatch": 512, "parallel": 2, "threads": 2, "query_max_len": 512,
    "extra_args": ["-ot", "token_embd\\.weight=CPU"], "expected_vram_mib": <VRAM процесса из гейта>}
   ```
   (при CPU-варианте: `"placement": "cpu", "threads": 8`, без `expected_vram_mib`, `mem_limit: 8g` у
   `rx580-retrieval` в compose).
5. **Сервис RX580**: `.env` (резервная копия) `VKM_RX580_IMAGE=vkm-rx580-retrieval:0.1.4-<C12>`;
   `docker compose up -d --no-deps rx580-retrieval`; `curl -fsS http://127.0.0.1:8790/health` → три модели
   `resident`, `late_store.status = READY`; `docker stats --no-stream rx580-retrieval` — память ниже 4 GiB с запасом
   (иначе `mem_limit: 6g`).
6. **API** (маршрут ещё выключен): `.env` `VKM_API_IMAGE=vkm-corpus-api:0.1.0-<C12>`;
   `docker compose up -d --no-deps api mcp mcp-admin`.
7. **Индекс векторов страниц:**
   - `docker compose --profile jobs run --rm -T vkm-job search build-page-vectors --embeddings /data/<art> --snapshot snap-20260928T193550Z-738eebee --plan-only --missing-out /data/receipts/visual_route/todo.json`
     → `PLAN_ONLY`, `checks_64.ok = true`, `pages.with_preview = 26092`. Если `E_CHECK_FAILED` — `todo.json` (ID и
     хэши превью) на WORKSTATION, с согласия пользователя
     `python infra/workstation/visual_route/encode_pages.py --todo todo.json --out-root <vis>/data` (RTX, ≈ 10 стр./с),
     перенести новые части, повторить;
   - та же команда без `--plan-only --missing-out` → `COMPLETE`, алиас `vkm-pagevis`;
     `… vkm-job search status` → раздел `page_vectors`.
8. **Включение**: в environment сервиса `api` (compose) `VKM_HYBRID_VISUAL_ROUTE: "1"`;
   `docker compose up -d --no-deps api mcp`.
9. **Smoke:**
   - `docker compose exec -T api vkm-corpus search hybrid-smoke --api-url http://127.0.0.1:8000 --late --expect-route visual --query "схема расположения камер и целиков" --query "карта оседаний земной поверхности" --query "геологический разрез соляной толщи"`
     → PASS, в выводе `server_ms.visual_embed`, `visual_knn`, `visual_route.pages_returned`;
   - `… hybrid-smoke --api-url http://127.0.0.1:8000 --late --expect-route default` → PASS (текстовые не задеты);
   - `/v1/status` → `dependencies.visual_route.enabled = true`, `opensearch.page_vectors.matches_canonical_snapshot = true`.
10. **Откат**: `VKM_HYBRID_VISUAL_ROUTE: "0"` + `up -d --no-deps api mcp` выключает маршрут сразу; образы — из копий
    `.env`; слот — из копии `rx580.json`; индекс — `vkm-job search rollback-page-vectors`.

## 8. Тесты

- Новые: детектор (60 с параметрами), маршрут в гибриде (18), векторы страниц и артефакт (7), слот и гейт (7),
  API/MCP/окружение/досье (3), размещение на CPU (1); подписи (+3), реестр спецификаций. Всё на фейках, живых сервисов
  нет.
- WSL, `~/vkm/venv-corpus`, `tests/corpus`: **1038 passed**, 55 skipped, 5 failed — ожидаемые `test_public_hygiene`
  (git в WSL не читает `.git`-файл рабочего дерева); с Windows-python `test_public_hygiene` — 6 passed.
- Тесты API и MCP пропускаются в `venv-corpus` (нет fastapi и mcp). Их гоняли во временном venv вне git:
  `venv-corpus` плюс пакеты из `requirements/corpus-services.lock.txt`. Результат — `tests/corpus`: **1150 passed**,
  46 skipped, 7 failed: те же 5 `test_public_hygiene` и 2 `test_cad_mcp`. Мост `vkm-cad` ветка не трогает; в
  `venv-corpus` эти тесты пропускаются. Все тесты маршрута, API и MCP — зелёные.

## 9. Открытые вопросы

- Гейт RX580 (паритет, VRAM, p50/p95) не измерен; латентность F16 на Polaris — главный риск (оценка 0,4–0,9 с).
- Векторы страниц — снимка V2. Совпадение с CURRENT проверит `--plan-only`. Кодирование недостающих страниц требует
  чтения STAGING — нужно согласие пользователя.
- Детектор оценён на тех же запросах, по которым видны категории (не тексты) визуального трека. Независимой
  выборки нет. Пропуски — запросы без слова-картинки; расширение словаря — задача V3 на отложенных запросах.
- Прирост на P незначим (p 0,072): метки ставились по тексту страниц, страницы только с изображением получают 0 (V2 §4).
- Точный поиск страниц (`script_score` по 26 тыс. векторов 2048) на CORE не измерен; есть переключатель на HNSW.
- Индекс векторов страниц в OpenSearch 3.8 локально не собирался; сборка безопасна (при ошибке алиас не трогается),
  первый шаг — `--plan-only`.
