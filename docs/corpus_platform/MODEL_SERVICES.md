# Модельные сервисы EDGE: text и visual реранкеры

VKM Corpus Platform v0, агент F. Решения: CP-18 (с REVISED), H-13, H-41 (отклонён для v0), H-45, H-10
([журнал координатора](../implementation_work/COORDINATOR_DECISIONS.md)). Проект: [отчёт F, ред. 2](../implementation_work/AGENT_F_RERANKER_DEPLOYMENT.md).
Состояние на 28.09.2026: развёрнуто на EDGE; parity PASS, acceptance PASS 9/9 — см. §7.

Оценки реранкеров — сигнал поиска, а не evidence: слой `SERVICE`, `review_status = NOT_APPLICABLE`, в канон не
пишутся. Оценки сравнимы только внутри одного ответа и одной конфигурации (`model_config_sha256`, `placement`).

## 1. Что где работает

| Роль | Контейнер | Адрес | Кто управляет |
|---|---|---|---|
| text-rerank | `careerops-reranker` (существующий, KEEP) | `127.0.0.1:18082` (сервис слушает и все интерфейсы — техдолг D-F7) | не VKM: не останавливать, не пересоздавать, не менять конфиг/образ/кэш |
| visual-rerank | `vkm-rerank-m0` (llama-server) | только `127.0.0.1:18083` | compose-проект `vkm-rerank` |
| gateway | `vkm-rerank-gateway` (FastAPI) | LAN-адрес EDGE `:18084` и `127.0.0.1:18084` | compose-проект `vkm-rerank` |

VKM API и MCP на CORE видят только gateway. Токен — заголовок `X-VKM-Rerank-Token`.

## 2. Модели и пины

| Роль | Модель @ revision | Файл, sha256 | Квант | Лицензия |
|---|---|---|---|---|
| text | `jinaai/jina-reranker-v3.5` @ `e8a93f33f0b22108f8c2364f8484ce3422552fbc` | `model.safetensors` `50c684b7…dfb6fe`; `tokenizer.json` `4e95945a…455803` | fp16, без квантизации | CC BY-NC 4.0 |
| visual, LLM | `jinaai/jina-reranker-m0-GGUF` @ `61490ce6a4799192781ab7beab4ee4661675488c` | `jina-reranker-m0-Q6_K.gguf` `115f0be6…ace6d5` (1 272 737 984 B) | Q6_K | CC BY-NC 4.0 |
| visual, голова | тот же | `mlp_weights.npz` `b7bea0fb…e36448` | f32 | CC BY-NC 4.0 |
| visual, mmproj | конвертация из `jinaai/jina-reranker-m0` @ `94bfe0aeb2d4dd7978362699cddd5893d4e0adc8` | `mmproj-jina-reranker-m0-Q8_0.gguf` `91628a15…4337b9` (712 893 536 B) | Q8_0 | CC BY-NC 4.0 |
| visual, токенизатор запроса | `jinaai/jina-reranker-m0` @ `94bfe0ae…` | `tokenizer.json` `091aa759…67b8a` | — | CC BY-NC 4.0 |

- Веса не в git и не распространяются. Снимки с проверенным LFS sha256 — в каталоге моделей WORKSTATION
  (`jinaai__jina-reranker-m0/<rev>`, `jinaai__jina-reranker-m0-GGUF/<rev>`, `jinaai__jina-reranker-v3.5/<rev>`
  (только код и токенизатор), `derived/jina-reranker-m0-mmproj/llama.cpp-4da6337767f9`), receipt
  `MODELS_RECEIPT_F.json`. На EDGE — копии в `$VKM_EDGE_ROOT/models/…` (sha256 сверены после копирования).
- mmproj: `convert_hf_to_gguf.py --mmproj --outtype q8_0` на закреплённом llama.cpp; в локальной копии `config.json`
  `architectures` заменено на `Qwen2VLForConditionalGeneration`, `auto_map` удалён (чекпойнт не менялся).
- llama.cpp: commit `4da6337767f973e2b4d0797e5b323d77d8565e4a` (b11223) + патч D-F1
  `0001-qwen2vl-skip-lm-head-for-embeddings.patch` (sha256 `9a0abe9f…008f16`, §6).

## 3. Размещение и VRAM (GTX 1650 Mobile, 4096 MiB, полезно 3716)

| Величина | MiB |
|---|---:|
| text-сервис до пробы / после P_text (запрос 4091 токен, high-water mark до рестарта сервиса) | 1250 / 1674 |
| бюджет m0 по формуле CP-18 `3716 − P_text − 128` | 1914 |
| m0 при `-ngl 20` после прогрева максимальным изображением | 1852 |
| GPU занято / свободно после развёртывания | 3530 / 186 |

Размещение m0: `mmproj=GPU; llm_layers_gpu=20/28; output=CPU` (выходной слой не считается вовсе — патч D-F1).
При включённом по умолчанию op offload матричные умножения CPU-слоёв на больших батчах выполняются на GPU с
подкачкой весов, поэтому число слоёв на GPU почти не влияет на скорость: 30 пар (5 запросов × 6 изображений) — 231 с
при `-ngl 20` и 236 с при `-ngl 12`. `-ngl` — это компромисс «запас VRAM ↔ ничего»; при нехватке запаса снижать до 16
или 12 (m0 ≈ 1700 / 1550 MiB).

## 4. Контракт `vkm.rerank/1`

Модели pydantic — `src/vkm_corpus/retrieval/models.py`; клиент для VKM API — `retrieval/client.py`
(`RerankClient.from_settings()`: `VKM_RERANK_URL`, `VKM_RERANK_TOKEN_FILE`).

| Метод, путь | Вход | Лимиты |
|---|---|---|
| `POST /v1/rerank/text` | `{query, candidates: [{id, text}], top_n?, truncate_to_tokens?, request_id?}` | ≤ 24 кандидата (иначе 413, пачки не склеиваются); весь listwise-промпт ≤ 4096 токенов по `tokenizer.json` сервиса (иначе 413 с числом токенов по кандидатам) |
| `POST /v1/rerank/visual` | `{query, candidates: [{id, image_base64}], top_n?, request_id?}` | ≤ 8 изображений (иначе 413); PNG/JPEG/WebP ≤ 10 MiB, ≤ 50 Мпикс; запрос ≤ 224 токена |
| `GET /health` | — | без токена; состояния бэкендов |
| `GET /status` | — | с токеном; роли, пины, placement, лимиты, лицензии; адресов нет |

Ответ: `model_id`, `model_revision`, `quant`, `placement`, `backend_version`, `weights_sha256`,
`model_config_sha256`, `candidate_ids` + `scores` (по рангу), `results[]` (`rank`, `score`, `input_index`,
`input_text_sha256`, `text_char_range`, `truncated`, `n_tokens` | `source_image_sha256`, `image_sha256`,
`pixel_sha256`, `image_size_px`, `image_tokens`), `latency_ms{total, preprocess, queue, backend}`,
`n_tokens_total`, `query_sha256`, `input_sha256`, `warnings`.

Ошибки: `RERANK_UNAUTHORIZED` 401, `RERANK_INPUT_INVALID` 422, `RERANK_IMAGE_DECODE_FAILED` 422,
`RERANK_PAYLOAD_TOO_LARGE` 413, `RERANK_BACKEND_BUSY` 503 (+`Retry-After`), `RERANK_BACKEND_UNAVAILABLE` 503,
`RERANK_BACKEND_TIMEOUT` 504, `RERANK_BACKEND_ERROR` 502. Вход (текст, base64) в ошибках не возвращается и не
логируется.

Изображения нормализует gateway (одна функция `images.normalize_image` и для эталона, и для сервиса): EXIF → RGB
(прозрачность на белом) → длинная сторона ≤ 1024 px без увеличения → Qwen2-VL `smart_resize` (кратно 28,
3136…602112 px, ≤ 768 визуальных токенов) → PNG. Промпт m0: `**Document**:\n<изображение>\n**Query**:\n{query}` +
токен оценки `<|box_end|>`; оценка `sigmoid(MLP(h_last) − 2.65)` считается в gateway.

Text-бэкенд: gateway берёт `expected_runtime` из `/readyz`, передаёт `token_budget = 4096` (сервис сам точно
пересчитывает и отказывает до инференса). Счёт токенов gateway = счёт сервиса (промпт воспроизведён байт-в-байт).

## 5. Конкурентность

- Два процесса, два CUDA context, без MPS и без общего lock между text и visual.
- Text-сервис: `ThreadingHTTPServer`, но инференс под одним `threading.Lock` (исходники CareerOps, commit `41bc243`) —
  параллельных проходов нет, VRAM не растёт от параллельности, поэтому ограничения в gateway нет (H-41).
- Visual: llama-server с одним слотом (`-np 1`); gateway пропускает 1 запрос в работу и держит очередь ≤ 8, дальше
  503 `RERANK_BACKEND_BUSY`.
- Таймауты: gateway → text 300 с, → visual 300 с; клиент VKM API → gateway 330 с для обоих (H-13: visual ≥ 120 с).
  Text-сервис исполняет запросы по одному, поэтому всплеск текстовых запросов ждёт в его очереди: при первом
  acceptance таймаут 60 с дал 504 на 6-м и следующих запросах всплеска (≈ 10 с на запрос под нагрузкой visual).
  Ориентиры по времени: text 24 коротких пассажа ≈ 5 с (≈ 10 с при параллельном visual), 4K токенов ≈ 17 с;
  visual ≈ 6.5–8 с на изображение.
- Если нужен быстрый отказ вместо ожидания, в gateway есть выключенные по умолчанию `VKM_RERANK_TEXT_MAX_INFLIGHT` и
  `VKM_RERANK_TEXT_MAX_QUEUE` (503 `RERANK_BACKEND_BUSY`); включать только решением координатора (H-41).

## 6. Развёртывание, запуск, перезапуск

Каталоги EDGE (`VKM_EDGE_ROOT`): `models/` (веса, mmproj, голова, токенизаторы; только чтение), `secrets/`
(`rerank_gateway_token`, root:10001, 0440), `rerank/deploy/` (`compose.yml` = `infra/edge/compose.yml` и host-local
`.env` по образцу `infra/edge/.env.example`, 0600), `rerank/receipts/`, `rerank/acceptance/`, `rerank/logs/`.

```bash
cd "$VKM_EDGE_ROOT/rerank/deploy"
docker compose --env-file .env -f compose.yml up -d            # запуск / применение изменений
docker compose --env-file .env -f compose.yml ps
docker compose --env-file .env -f compose.yml restart gateway    # перезапуск gateway (m0 не трогается)
docker compose --env-file .env -f compose.yml up -d --force-recreate m0   # после смены VKM_RERANK_M0_NGL
docker compose --env-file .env -f compose.yml down               # остановка только VKM-сервисов
```

- `careerops-reranker` в этом проекте не упоминается; `docker compose down` его не затрагивает.
- После каждого старта llama-server gateway сам прогревает его изображением 896×672 (768 токенов): встроенный прогрев
  llama.cpp (`--no-warmup`) выключен, потому что резервировал бы буфер ViT под 46×46 = 2116 токенов.
- Токен для VKM API на CORE координатор копирует из `secrets/rerank_gateway_token` в секреты CORE
  (`VKM_RERANK_TOKEN_FILE`); в git — только `.env.example`.
- Образы (`infra/edge/build_images.sh`): `vkm/llama-server-cu132-sm75:b11223-4da6337767f9-p1`
  (id `sha256:456889f5…`), `vkm/rerank-gateway:0.1.0` (id `sha256:106974ad…`); базы закреплены digest-ом в Dockerfile. С 28.09 работает
  `vkm/rerank-gateway:0.1.1` (id `sha256:73ef20f2…`): при разрешённом усечении шлюз подгоняет кандидатов под
  измеренную разметку listwise-промпта v3.5 (497–625 токенов при n=24) вместо ответа 413 (находка 7 бенчмарка J);
  пересоздан только контейнер шлюза, text-сервис и m0 не перезапускались.
  **EDGE — ноутбук**: компиляция llama.cpp только с `BUILD_JOBS=6` под `nice -n 10`, без лишних пересборок (слой с
  патчем отдельный); тяжёлую сборку лучше делать на WORKSTATION и переносить `docker save <tag> | ssh edge docker load`.
  Первая сборка шла с 12 потоками (CPU 100 %, Tctl ≈ 99.5 °C) — это больше не повторяется.
- Архив (H-10): оба собственных образа сохранены `docker save` в архив WORKSTATION
  (`vkm_edge_images_2026-09-28/`, sha256 в `image_archive_SHA256SUMS.txt`); веса — в каталоге моделей WORKSTATION.
  Восстановление: `ssh edge docker load < <архив>.tar`.

## 7. Проверки

- Быстро: `curl http://127.0.0.1:18084/health` на EDGE; `vkm-corpus rerank status` (с `VKM_RERANK_URL` и токеном).
- Parity (блокирующий до развёртывания m0): эталон — transformers 4.57.3, bf16, RTX 5070 Ti, «медленный» процессор
  Qwen2-VL (transformers 5.x этот чекпойнт не загружает: старые имена тензоров); 5 русских запросов × 6
  синтетических изображений. Порог: Spearman ≥ 0.9 и `max|Δ| ≤ 0.05` по каждому запросу, top-1 совпадает.
  EDGE (`-ngl 20`, Q6_K + mmproj Q8_0): **PASS**, `max|Δ|` 0.0254, среднее 0.0067, min ρ 0.943, top-1 5/5.
  Смена раскладки (20 → 12 слоёв) меняет оценки до 0.0065 — выше предрегистрированных 1e-3 (информационно):
  оценки сравнимы только при одинаковом `placement`.
- Acceptance (§31/§56, 9 пунктов) на EDGE:
  `sudo -n cat "$VKM_EDGE_ROOT/secrets/rerank_gateway_token" | python3 acceptance.py run --fixtures … --out … --token-stdin
  --expected-placement "mmproj=GPU; llm_layers_gpu=20/28; output=CPU"`; P_text — `python3 acceptance.py ptext`.
  Receipts копируются на WORKSTATION в `work/corpus_platform/impl/edge_receipts/`.

Результат acceptance 28.09 (прогон 2, окно 01:28–01:41 UTC): **PASS 9/9**.

| # | Проверка | Числа |
|---|---|---|
| 1 | оба загружены | `/health` ok/ok; оба PID в `nvidia-smi`: text 1674 MiB, m0 1868 MiB |
| 2 | VRAM в покое | занято 3546 из 3716, свободно 170 MiB (порог 128) |
| 3 | text | top-3 5/5; повтор Δ = 0; перестановка кандидатов — top-3 тот же (полный порядок listwise-модели меняется); 25 кандидатов → 413; 14 182 токена → 413 в gateway (сервис не вызывался); счёт токенов gateway = сервиса (1415–1428); 5.2 с на 24 пассажа |
| 4 | visual | top-1 5/5 (6 изображений ≈ 46 с); A: 0.884 против 0.294; B: перестановка id Δ = 0; C: дубль Δ = 2.7e-4; D: «Рис. 1» — побеждает содержание; E: пустой лист ниже; F: 422/422/422/422/413/413; 8 изображений — 61.5 с; placement в `/status` верный |
| 5 | параллельно | 3 text-вызова (10.2–10.6 с) завершились внутри visual-вызова 78 с; оба PID активны в одной секунде `pmon` |
| 6 | всплеск | 12 (8 text + 4 visual) → 12 × 200 за 106 с; 16 (10 + 6) → 16 × 200 за 148 с; без 503/5xx |
| 7 | нет OOM | минимум свободной VRAM 170 MiB, максимум занятой 3546; строк OOM/CUDA error нет |
| 8 | нет перезагрузки | `careerops-reranker` (StartedAt 2026-09-10T10:58:55Z, RestartCount 0, PID неизменен), m0 и gateway — без изменений; тот же экземпляр llama-server |
| 9 | латентность | p50/p95/max клиента с учётом всплесков: text 31.6 / 94.2 / 104 с, visual 46.3 / 132.6 / 148 с; одиночный text 5.2 с, изображение ≈ 7.7 с; GPU 39–53 °C, ≤ 28.6 W, без троттлинга |

Прогон 1 (8/9) упал на всплеске: 504 у 6-го и следующих text-запросов при таймауте gateway 60 с — после этого
таймауты text подняты до 300/330 с (§5).

Receipts (`work/corpus_platform/impl/edge_receipts/`): `MODELS_RECEIPT_F.json` (скачивание, LFS sha256),
`mmproj_SHA256SUMS.txt`, `deploy_*.json` (образы, пины, placement, VRAM, пробы памяти), `ptext_*.json`,
`parity/*.json` (эталон, EDGE, раскладка), `backend_ops/`, `memprobe/`, `acceptance_*.json` (оба прогона),
`image_archive_SHA256SUMS.txt` (архив `docker save` собственных образов на WORKSTATION, H-10).
- Тесты PUBLIC: `tests/corpus/test_rerank_*.py` (без сервисов), живые — `test_rerank_live.py` (маркеры `services`,
  `gpu`; без `VKM_RERANK_URL` → NOT_RUN).
- «No reload»: `docker inspect -f '{{.State.StartedAt}} {{.RestartCount}} {{.State.Pid}}'` трёх контейнеров до и
  после; у m0 новый старт виден по новому `media_marker` в `/props`.

## 8. Отклонения от проекта F (ред. 2) и почему

| Было в проекте | Стало | Основание (измерено 28.09) |
|---|---|---|
| патч D-F1 не нужен (выход на CPU) | патч D-F1 применён (разрешён CP-18) | upstream в режиме `--embeddings` считает lm_head на всех токенах ubatch; op offload переносит его на GPU: +782 MiB compute buffer (логиты 593 + копия output-весов 191). Без патча m0 не помещается рядом с v3.5 даже при `-ngl 0`/`24`/`28` (OOM) |
| ≈24/28 слоёв на GPU | 20/28 | реальные 43 MiB на слой, ViT-буфер 97 MiB (резерв под 2116 токенов), ленивые выделения CUDA ≈ 135 MiB, P_text 1674; при 21–22 слоях запас < 150 MiB |
| `-np 2 -c 2048 -ub 1024` | `-np 1 -c 1024 -ub 512` | −90 MiB; один слот — детерминированные оценки независимо от соседних запросов |
| прогрев llama-server | `--no-warmup` + прогрев gateway | встроенный прогрев mtmd резервирует ViT под 2116 токенов |
| документ обрезается до 160 токенов | без обрезки по умолчанию; `truncate_to_tokens` по запросу, иначе 413 с числом токенов | H-13: скрытая обрезка портит listwise-ранжирование; диапазон использованного текста возвращается |
| лимит «1 запрос в работе» к text | лимита нет | H-41: сервис сам сериализует инференс |
| эталон transformers 5.x | transformers 4.57.3 | 5.17 не загружает веса чекпойнта (MISSING) и даёт случайные оценки |

## 9. Риски и техдолг

- D-F7: `careerops-reranker` слушает все интерфейсы, ufw выключен — прямые LAN-клиенты обходят лимит 4096 токенов и
  могут поднять VRAM v3.5 выше 1674 MiB; тогда запаса m0 не хватит (снизить `-ngl`).
- VRAM text-сервиса не возвращается до его рестарта (allocator PyTorch) — high-water mark фиксируется P_text.
- TU117 без tensor cores, лимит мощности 50 W: латентность visual ≈ 6.5–8 с на изображение; 8 изображений ≈ 1 мин.
- Лицензия обеих моделей CC BY-NC 4.0: некоммерческое использование, веса не распространяются.
- Поддержка m0 + изображений в llama.cpp не официальная (свой mmproj, патч): держит только parity-тест; при смене
  commit llama.cpp parity повторяется до развёртывания.
