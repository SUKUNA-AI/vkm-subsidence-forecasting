# AGENT K — RX580: реализация (эмбеддинги, derived-артефакты, сервис CORE)

Дата: 28.09.2026. Код — `src/vkm_corpus/embeddings/`, `src/vkm_corpus/retrieval_service/`; инфраструктура —
`infra/core/rx580/`; тесты — `tests/corpus/test_embeddings_*.py`, `tests/corpus/test_retrieval_service_*.py`.
Решения: CP-26 (владение), CP-31 (бэкенд, keepalive). Бэкенд и его обоснование — [BACKEND_RESEARCH](AGENT_K_RX580_BACKEND_RESEARCH.md).

## 1. Архитектура

```
canonical objects ──text rule──► embedded text ──SpecTokenizer (pinned tokenizer.json)──► token ids
      │                                                                                        │
      │ text_hash                                              llama-server (GGUF, Vulkan RX580│/ CUDA RTX)
      ▼                                                                                        ▼
re-embed plan (§46) ◄── derived artifacts (Parquet, §39–43) ◄── postprocess (pool/heads/MRL/int8/L2)
                              │
                              ├──► OpenSearch k-NN projection (rebuildable without GPU, §44)
                              └──► token store (MaxSim, late interaction)

query ──► retrieval service (CORE): SpecTokenizer ─► resident llama-servers (dense + late, two processes)
          ─► BM25 + k-NN (OpenSearch) ─► RRF ─► MaxSim (late) ─► hits + trace ─► (EDGE reranker)
```

Принципы: backend никогда не токенизирует (ids приходят из закреплённого `tokenizer.json`, поэтому токенизация RX580 и
эталона совпадает по построению); одна и та же постобработка (`postprocess`) у сервиса, воркера и parity-харнеса;
две резидентные модели = два независимых процесса `llama-server` без общей блокировки.

## 2. Пакет `vkm_corpus.embeddings`

| Модуль | Назначение |
|---|---|
| `specs` | `EncoderSpec`/`ColbertSpec` 14 кандидатов: ревизия, лицензия, архитектура, pooling, нормализация, префиксы, Matryoshka, ColBERT-правила, патчи |
| `tokenize` | `SpecTokenizer`: dense (`prefix + text`, specials пост-процессора, усечение с сохранением specials), ColBERT PyLate (маркер на позицию 1, pad-расширение) и Stanford (плейсхолдер `". "` → маркер, `<mask>`-расширение), маска документа без пунктуации, контекстные спаны pplx-context |
| `postprocess` | pooling (cls/mean/last), Matryoshka + L2, `tanh_int8` pplx, ColBERT-токены (+ голова шлюза), BGE-M3 sparse, чанки late chunking, MaxSim, косинус |
| `signature` | `EmbeddingConfig` (§37) и `QueryConfig` (§38), `embedding_signature(object_id, text_hash, config)`; бэкенд в подпись не входит |
| `artifacts` | раскладка `derived/embeddings/<kind>/<model>/<revision>/<config-hash>/`, неизменяемые части `part-<writer>-NNNNN.parquet`, манифест на писателя, чтение текущего вида, проверки §64 |
| `reembed` | план §46: unchanged / new / changed / orphaned |
| `worker` | протоколы `JobQueue`/`TextSource`/`DocumentEncoder`, `InMemoryJobQueue` (семантика PostgreSQL), `LlamaDocumentEncoder`, `EmbeddingWorker` |
| `llama` | клиент `llama-server` (stdlib `http.client`, keep-alive на поток, ids → векторы; токенные матрицы — base64 float32 по патчу 0003, обычный JSON тоже принимается) |
| `gpu` | sysfs/fdinfo: VRAM/GTT устройства и процесса, busy, runtime PM, время движков GPU |
| `parity` | метрики §30 и gate |
| `bench` | харнес матрицы моделей (`model`), residency A–D, D_bg, D_bg_sep (`pair`) и A/B двух бинарников (`ab`) |
| `reference` | эталон HF fp32 (WORKSTATION) + сверка с официальной библиотекой |
| `summarize`, `fakes`, `fake_server`, `cli` | таблицы отчётов и вердикты §31; тестовые двойники; `vkm-corpus embed specs\|signature\|plan\|validate\|encode` (до регистрации группы — `python -m vkm_corpus.embeddings.cli …`) |

### 2.1 Подписи (§37, §38)

`EmbeddingConfig`: model_id, model_revision, weights_file, **weights_sha256**, quantization, mode, dimension, pooling,
normalization, output_transform, document_instruction, text_rule, max_len, tokenizer_sha256, heads_sha256,
storage_precision, late-настройки, pipeline_version. `config_signature = sha256("vkm-emb-config-v1|" + canonical JSON)`
называет каталог артефакта. Тест проверяет изменение подписи от **каждого** поля.

`QueryConfig`: model, revision, weights_sha256, quantization, query_instruction, dimension, pooling, normalization,
tokenizer_sha256, max_len, output_transform, late-настройки запроса (маркер, query_maxlen, expansion token, attend),
heads_sha256, pipeline_version.

Бэкенд (Vulkan RX580 / CUDA RTX / torch) и воркер пишутся в каждую строку, но в подпись не входят: одинаковые веса на
разных бэкендах, прошедших parity-gate, дают взаимозаменяемые векторы (§31). Следствие (DN-K2): финальное кодирование
RTX + RX580 параллельно — один и тот же GGUF на обоих (llama.cpp CUDA на RTX).

### 2.2 Derived-артефакты (§39–43, §64)

Схемы: общие поля `object_id, source_id, page_id, object_type, text_hash, content_sha256, model_id, model_revision,
config_hash, embedding_signature, worker, backend, created_at`; dense/visual — `dimension, precision, quant|artifact_sha,
vector`; sparse — `token_ids, weights`; multivector — `token_count, dimension, precision, vectors` (плоский
`list<halffloat|float>` token×dim) и `token_ids` оставленных позиций. Векторы пишутся из numpy
(`ListArray.from_arrays`), float16 хранится как Parquet FLOAT16.

Проверки перед импортом (`validate`): ожидаемое число = текущие строки, дубликаты (объект + text_hash), пропуски,
лишние id, одна подпись конфигурации, подпись каждой строки, размерность, конечность и норма векторов (L2-конфигурации),
неотрицательные веса sparse, sha256 частей по манифестам. Писатели разных GPU не делят счётчики и манифесты
(`_manifest-<writer>.json`), поэтому параллельная запись RTX и RX580 безопасна.

### 2.3 Re-embed (§46)

Ключ — `text_hash` встраиваемого текста (sha256 UTF-8, после text rule). Совпал хеш и подпись → инференса нет; изменился
текст → только этот объект; объект исчез из канона → `orphaned` (история, в проекцию не идёт). Смена любой части
конфигурации = новая подпись = новый каталог; старый не трогается.

### 2.4 Очередь заданий (§34–36) — интерфейс для координатора

Состояния `PENDING → CLAIMED_RTX5070 | CLAIMED_RX580 → DONE | FAILED`; атомарный захват в PostgreSQL:

```sql
UPDATE ops.embed_job SET state = 'CLAIMED_RX580', worker = $1, attempts = attempts + 1, heartbeat_at = now()
 WHERE job_id = (SELECT job_id FROM ops.embed_job
                  WHERE config_signature = $2 AND (state = 'PENDING'
                        OR (state LIKE 'CLAIMED_%' AND heartbeat_at < now() - interval '10 minutes'))
                  ORDER BY job_id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING job_id, object_ids;
```

Планирование pull-based: задание = небольшой пакет объектов одной подписи; более быстрый GPU просто забирает больше
заданий (минимум wall-clock без центральной модели пропускной способности). Повторно захваченное задание идемпотентно:
воркер отбрасывает объекты, уже встроенные с тем же `text_hash` и подписью. Реализация `JobQueue` на PostgreSQL —
координатор (`vkm_corpus.ops`); протокол и тестовый двойник — здесь.

## 3. Сервис `vkm_corpus.retrieval_service` (CORE, §56)

| Эндпоинт | Содержание |
|---|---|
| `GET /health` | по каждой модели: loaded, resident (живой процесс + VRAM ≥ 80 % ожидаемого), vram/gtt MiB (fdinfo), pid, starts/restarts, keepalive; устройство: VRAM total/used, runtime PM; статус ok/degraded/down |
| `GET /model-info` | spec, веса и sha256, квант, размерность, **query signature**, лицензия, parity-вердикт (receipt) |
| `POST /embed/query` | `role` dense/late/both; both — dense и late кодируются одновременно (`asyncio.gather`) |
| `POST /search/dense` | k-NN OpenSearch (cosine), фильтры, trace dense_rank |
| `POST /search/hybrid` | BM25 + k-NN → RRF (k = 60), trace bm25/dense/fused ranks |
| `POST /search/late` | кандидаты (заданные или hybrid top-N) → MaxSim по токенным векторам derived-артефакта; hybrid и late-кодирование запроса идут параллельно |
| `GET /metrics` | Prometheus text: запросы, латентность по эндпоинтам и моделям, VRAM, resident, restarts, keepalive |

Резидентность: `ResidencyManager` поднимает по одному `llama-server` на модель (последовательно, для учёта VRAM),
прогревает, следит (перезапуск упавшего с учётом `restarts` = reload count), keepalive (CP-31). Живые модели никогда не
выгружаются. «Resident» в `/health` = процесс жив, отвечает и его веса в VRAM (DRM fdinfo): VRAM ≥ 80 % измеренного
значения из конфигурации (`expected_vram_mib`, MODEL_MATRIX), а без него — ≥ 80 % VRAM сразу после прогрева и GTT не
больше VRAM. Модель, вытесненная runtime PM или ушедшая в GTT через BAR 256 MiB, видна как не резидентная; host-буферы
(staging, выходы) тоже числятся в GTT, поэтому GTT — только эвристика перелива, а не порог.
Дочерние `llama-server` запускаются с `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1` (только device-local VRAM; иначе вторая
модель уходит в GTT, BACKEND_RESEARCH §2.1a) и с `--cache-ram 0` (без prompt cache сервера: у causal Qwen3 он занимал
до 8 GiB RSS, §2.1b); бинарник образа содержит патч 0004 (без буфера логитов — 1–2 GiB pinned-памяти на процесс).
Опциональный bearer-токен (`VKM_RX580_TOKEN_FILE`), `/health` и `/metrics` без токена. Точка входа образа —
`python -m vkm_corpus.retrieval_service serve` (работает и до регистрации группы CLI).

Стоимость late-interaction (для инженерной таблицы J, §23; FACT, CORE): MaxSim в сервисе (numpy + OpenBLAS, один
BLAS-вызов на кандидата) — ≈ 6 мс на 100 кандидатов × 150 токенов × 128 измерений, ≈ 46 мс на 1000 (объединение
кандидатов в один вызов оказалось медленнее). Хранение: на canary-пробе в среднем 202 документных токена на объект у
mLateOn, 128 у jina-colbert-v2 (после маски пунктуации), 194 у pplx-late; при fp16 и 128 измерениях — ≈ 32–52 KB на
объект против 3 KB у dense-вектора 768 × fp32 (множитель ×11–17).

## 4. Развёртывание (для координатора)

- Образы: `vkm-rx580-llama:4da6337767f9-vk` (`infra/core/rx580/Dockerfile --target runtime`; патчи 0001–0004,
  `llama-server` sha256 `e7ad70c2b4d8…`) и `vkm-rx580-retrieval:0.1.0` (`infra/core/rx580/Dockerfile.service`,
  контекст — корень репозитория); оба собраны на CORE 28.09.
- Compose-фрагмент: `infra/core/rx580/compose.rx580.yml` (сервис `rx580-retrieval` на 127.0.0.1:8790 во внутренней сети
  `vkm_internal`; воркер `rx580-embed-worker` в профиле `jobs`); `.env` хоста: `VKM_RX580_ROOT`, `VKM_RX580_UID`,
  `VKM_RX580_GID`, `VKM_RENDER_GID`, `VKM_VIDEO_GID`, `VKM_SECRETS_DIR` (+ необязательные `VKM_RX580_IMAGE`,
  `VKM_RX580_KEEPALIVE_S`).
- До слияния с compose сервис запущен вручную (`docker run`, контейнер `vkm-rx580-retrieval`, `--restart
  unless-stopped`, `--memory 4g`, только 127.0.0.1:8790, keepalive 2 с) с временной парой granite-311m-r2 + mLateOn
  (Q8_0, обе одобрены §31); после слияния этот контейнер заменяется сервисом compose (`docker rm -f
  vkm-rx580-retrieval`).
- Конфигурация: `infra/core/rx580/rx580.example.json` (пара моделей — пример, не выбор); sha256 заполняет
  `vkm-corpus embed signature`.
- CLI: в `vkm_corpus/cli.py` `GROUPS` нужны `"embed": "vkm_corpus.embeddings.cli"` и
  `"retrieval": "vkm_corpus.retrieval_service.cli"` (файл координатора); extra `retrieval-rx580` в `pyproject.toml`
  (tokenizers, pyarrow + corpus-services) — DN-K1.

## 4a. Проверка на CORE (28.09.2026)

- **Сервис.** `vkm-rx580-retrieval` (образ 0.1.0, 0001–0004), пара granite-311m-r2 + mLateOn Q8_0: `/health` — `ok`,
  обе модели `resident` (VRAM 172 + 171 MiB после старта, 188 + 188 MiB после кодирования документов; GTT 32–38 MiB
  на процесс), `restarts` 0; `/model-info` отдаёт подписи запросов и вердикты §31 из квитанции parity. Live-тесты
  `pytest -m "gpu and services" tests/corpus/test_retrieval_service_live.py` через SSH-туннель — **2/2 PASS**
  (8192 MiB, обе резидентны, конкурентные dense + late без перезапусков).
- **Латентность через API** (WORKSTATION → туннель → сервис → два `llama-server`; включает сеть):
  `/embed/query` dense p50/p95 13,6/19,8 мс, late 13,7/16,5 мс, `role=both` 16,2/16,6 мс (оба кодирования идут
  параллельно: 16 мс вместо 27 последовательно), 8 клиентов `both` — 207 запросов/с (414 кодирований/с) при
  p50/p95 36/53 мс.
- **Сквозной прогон derived-артефактов** (`python -m vkm_corpus.embeddings.cli encode`, тексты canary-пробы,
  1788 объектов, через резидентные `llama-server` сервиса, артефакты только в каталоге K на CORE):

  | Прогон | Выход | Заданий | Закодировано | Время, с | Объектов/с | §64 | Строк | Пропуски / плохие векторы |
  |---|---|---|---|---|---|---|---|---|
  | 1 | dense granite-311m-r2 (768) | 28 | 1788 | 43,3 | 41,3 | OK | 1788 | 0 / 0 |
  | 1 | multivector mLateOn (128 × токен) | 28 | 1788 | 60,0 | 29,8 | OK | 1788 | 0 / 0 |
  | 2 | оба | 0 | **0** (план: unchanged 1788) | 0 | — | OK | 1788 | 0 / 0 |

  Второй прогон подтверждает §46 (тот же текст и подпись → 0 инференса). Скорость ниже стендовой (≈ 85 док/с):
  один клиент, потоки `-t 2` сервиса, длинные OCR-тексты и запись Parquet в том же процессе; массовое кодирование —
  отдельный воркер (CONCURRENCY_RESULTS §4). Размер: 191 MB на 1788 объектов в двух конфигурациях.

## 4b. Эталонные кодировки BGE-M3 для J (FlagEmbedding, CPU fp32; 28.09, решение пользователя)

Задача координатора (после поправки пользователя): BGE-M3 **не** на RX580 (карта — production-пара и stage-2), а
официальный FlagEmbedding `BGEM3FlagModel` в полной точности на CPU CORE — эталонная замена планируемому fp16.

- Модель: `BAAI/bge-m3` @ `5617a9f61b028005a4858fdac845db406aefb181` (закреплённый снимок, sha256 в `RECEIPT.json`
  снимка): `pytorch_model.bin` b5e0ce34…, `colbert_linear.pt` 19bfbae3…, `sparse_linear.pt` и `tokenizer.json` — в
  квитанции прогона.
- Среда: одноразовый образ `vkm-rx580-flagref:1` (`infra/core/rx580/research/Dockerfile.flagref`: python 3.13-slim
  по digest, torch 2.14.0+cpu, FlagEmbedding 1.4.2, transformers 4.57.6; `pip freeze` — в квитанции), контейнер
  `--cpus 12 --memory 10g`, `nice -n 10`, 12 потоков; пакеты хоста не ставились.
- Вход: юниты `vkm-units-v1` снимка canary `snap-20260928T073057Z-a2696925` — те же тексты, что кодирует J
  (`docs.jsonl` J, 13 172 юнита, 1 966 465 токенов XLM-R после усечения до 512; 386 юнитов упираются в 512), и
  189 запросов бенчмарка (`benchmarks/retrieval_v0/queries.jsonl`, до 128 токенов, как `max_query_tokens` J).
- Выходы одного прохода, как в FlagEmbedding: dense (CLS, L2, 1024), sparse (`relu(sparse_linear)`, максимум по
  token id, без cls/eos/pad/unk), multi-vector (`colbert_linear` по всем токенам кроме CLS, L2, 1024 на токен;
  хранение float16). Три конфигурации derived-артефактов с подписями, в которые входят веса и головы (sha256),
  `max_len`, нормализация, правило текста и backend `flagembedding-cpu-fp32` (новое необязательное поле подписи:
  пустое значение не меняет прежние подписи GGUF-конфигураций, DN-K2).
- Модуль `vkm_corpus.embeddings.m3_flag` (идемпотентен: уже закодированное по подписи пропускается, §46; проверки
  §64 — multi-vector проверяется векторизованно по частям), `export-lab-cache` — копия в раскладке `VectorCache`
  лаборатории J (ключи — sha256 текста). Тесты: `test_embeddings_m3_flag.py`.

<!-- M3FLAG:BEGIN -->
Итог прогона (CORE, 28.09, 12:57–13:42 MSK; квитанция `/srv/vkm-rx580/lab/bge-m3-flag/run/RECEIPT.json`):

| Выход | Подпись конфигурации | Строк | Проверки §64 | Объём |
|---|---|---|---|---|
| dense (CLS, L2, 1024, float32) | `fe00573df5698ba837e9941ad24e9d6dfabf0eb45acb29ac664881737e650912` | 13 172 | OK | 63 MB |
| sparse (lexical weights, float32) | `d83f099ecc885d0759fa68f6e43157c14e48fe6839e80eb9d21efe5f0948abe4` | 13 172 | OK | 6,6 MB |
| multi-vector (1024 на токен, float16) | `c69b8760f638e7eab33c5e85de3eefc7c27ccfe15c08d81033c6c67c81bd51d4` | 13 172 (1 953 293 токенов) | OK | 3,5 GB |

- Артефакты: `/srv/vkm-rx580/lab/bge-m3-flag/root/derived/embeddings/<kind>/BAAI__bge-m3/<rev>/<подпись>/`
  (не production-корень данных); запросы — `run/queries/queries.npz` (подписи запросов dense `63720dd6…`, sparse
  `1813621c…`, multi-vector `777c53bf…`).
- Производительность CPU fp32 (12 потоков, nice 10): 2654 с на 13 172 юнита — 4,96 юнита/с, 741 токен/с (по частям:
  ≈ 580 ток/с на самых коротких юнитах, ≈ 1000 ток/с на средних, ≈ 680 ток/с на длинных — до 512 токенов); загрузка модели 0,6 с; 189 запросов — 8,3 с;
  RAM контейнера ≈ 1,7 GB. Для сравнения: BGE-M3 на RX580 — Q8_0 ≈ 3300 ток/с, F16 ≈ 2550 ток/с (MODEL_MATRIX).
- Сверка J: его кодировщик лаборатории (fp32 CPU, transformers) на 189 запросах дал те же выходы, что FlagEmbedding
  (dense cos min 1,000000, те же числа токенов и token cos min 1,000000, разница sparse по L1 — 0).
<!-- M3FLAG:END -->

## 5. Тесты

| Файл | Что проверяет |
|---|---|
| `test_embeddings_signature.py` | детерминизм; чувствительность подписи документа и запроса к каждому полю; бэкенд вне подписи |
| `test_embeddings_reembed.py` | same hash + signature → 0 inference; только изменённые; orphaned; дубли |
| `test_embeddings_artifacts.py` | round trip dense/sparse/multivector(fp16)/visual/int8; §64: дубли, пропуски, лишние, NaN, норма, checksum, чужая подпись, неизменяемость частей, параллельные писатели |
| `test_embeddings_tokenize.py` | правила dense/PyLate/Stanford, маска пунктуации, BGE-M3 CLS, контекстные спаны, реестр спецификаций |
| `test_embeddings_postprocess.py` | pooling, MRL, int8, ColBERT, sparse, MaxSim, Spearman/Kendall/nDCG, parity gate |
| `test_embeddings_worker.py` | два устройства на одной очереди без двойной работы, идемпотентный повтор, повтор/отказ, область подписи |
| `test_embeddings_encode_cli.py` | `embed encode` через фейковый `llama-server`: 3 задания, артефакты проходят §64, повторный прогон — 0 инференса |
| `test_embeddings_m3_flag.py` | эталон BGE-M3 (FlagEmbedding): подписи с backend, юниты, проверка multi-vector по частям, экспорт в кеш лаборатории |
| `test_embeddings_summarize.py` | таблицы отчётов; вердикты §31 (dev + canary, production-бэкенд, исследовательские варианты не считаются) |
| `test_retrieval_service_contract.py` | все эндпоинты с фейковыми бэкендами, trace, токен, метрики, **отсутствие глобальной блокировки** |
| `test_retrieval_service_residency.py` | реальные дочерние процессы (фейковый llama-server): две резидентные модели, keepalive, перезапуск и счётчик |
| `test_retrieval_service_config.py` | конфигурация, переопределения окружением, регистрация CLI |
| `test_retrieval_service_live.py` | маркеры gpu + services: живой сервис на CORE (8 GiB, обе модели resident, конкурентные запросы) |

## 6. Воспроизведение измерений (§58)

1. WORKSTATION (WSL): закреплённые снимки HF (`huggingface_hub.snapshot_download(revision=…)`, sha256-receipt на
   модель) → конвертация `convert_hf_to_gguf.py` (пин b11223 + патч 0001) в F32 → `llama-quantize` Q8_0 / Q6_K / Q5_K_M /
   F16 (официальные GGUF — только jina-v5 small/nano retrieval) → `SHA256SUMS` рядом с GGUF.
2. Эталон: `python -m vkm_corpus.embeddings.reference --key K --model-dir <snapshot> --docs parity_docs.jsonl
   --queries parity_queries.jsonl --out ref/K` (fp32 CPU; `--check 48` — сверка с официальной библиотекой).
3. CORE: `llama-server` из образа `vkm-rx580-llama:4da6337767f9-vk` (или dev-сборка того же дерева);
   `python -m vkm_corpus.embeddings.bench model --key K --gguf F --quant Q --ref ref/K --out R.json` и
   `… bench pair --dense K:F --late K:F --ref-dense … --ref-late … --out R.json`; проверка патча, который не должен
   менять числа, — `… bench ab --bin-a OLD --bin-b NEW --key K --gguf F --ref ref/K --out R.json` (выходы на одних ids
   должны совпасть побитно); таблицы — `python -m vkm_corpus.embeddings.summarize <results>` (`--parity`,
   `--verdicts` — вердикты §31 по dev- и canary-пробам).
4. Все результаты содержат конфигурацию и argv сервера, переменные Vulkan, путь и sha256 бинарника, вариант бэкенда
   (`vk-mesa25`, `canary`, `mesa26`, `hip`, `bulk`) и время; probe-тексты и векторы — runtime-данные вне git.

## 7. Проекция в OpenSearch (для E и J; поля — после выбора победителя, CP-26)

OpenSearch — пересобираемая проекция derived-артефактов (§44): индексатор читает `iter_current_vectors(<config dir>,
expected)` и пишет `knn_vector` без GPU. Рекомендуемое поле (OpenSearch 3.8, плагин k-NN из дистрибутива):

```json
{"vector": {"type": "knn_vector", "dimension": 768, "space_type": "cosinesimil",
            "method": {"name": "hnsw", "engine": "faiss", "parameters": {"m": 16, "ef_construction": 128}}}}
```

Для int8-хранения (pplx native) — `"data_type": "byte"`; для binary — `"data_type": "binary"` и `space_type: hamming`.
В `_meta` индекса — `config_signature`, число векторов и sha256 манифестов derived-каталога: проекция сверяется с
артефактом (§64) и пересобирается из него. Токенные векторы ColBERT в OpenSearch не кладутся: MaxSim считается сервисом
по derived multivector-артефакту (или его mmap-упаковке) для кандидатов гибридного поиска.
