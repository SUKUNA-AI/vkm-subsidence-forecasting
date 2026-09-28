# AGENT K — RX580: исследование бэкендов (Retrieval Lab, §25)

Дата: 28.09.2026. Агент K (RX580 RETRIEVAL BACKEND ENGINEER). Хост — CORE (роль; адреса и логины не приводятся).
Рабочий корень на CORE — `/srv/vkm-rx580/` (сборки, модели, логи, результаты); Docker-образы с префиксом `vkm-rx580`.
Решение координатора по итогам первой вехи — CP-31 (бэкенд принят; keepalive вместо хостовой настройки PM).

## 0. Итог

| Порядок §25 | Путь | Результат | Статус |
|---|---|---|---|
| 1 | Vulkan / RADV (Mesa 25.0.7, Debian trixie) | RADV POLARIS10, Vulkan 1.4.305; 8 GiB VRAM видны; все операторы графов наших моделей поддержаны | **выбран** |
| 2 | llama.cpp / ggml Vulkan, пин `4da6337767f9` (b11223, 27.09.2026 — HEAD на 28.09, тот же пин, что у F на EDGE) | test-backend-ops Vulkan0: **18834/18834 PASS**; 14 из 14 кандидатов грузятся и считают эмбеддинги на GPU | **выбран** |
| 3 | Патчи недостающей поддержки | 0001 — конвертер (pplx bidirectional Qwen3, jina-colbert-v2, словарь mmBERT); 0002 — Qwen3-энкодеры: без lm_head, без KV-кеша для bidirectional; 0003 — base64-передача токенных матриц; 0004 — без буфера логитов в embeddings-контексте | применены; 0001/0002 — parity против эталона, 0003/0004 — A/B «выходы идентичны» |
| 4 | Собственные Vulkan-ядра | не потребовались: нет неподдержанных операторов; узкие места — отсутствие fp16-арифметики и int-dot у GFX8 (аппаратные) | не нужно |
| 5 | HIP | Debian sid: HIP 6.4.3 + rocBLAS 6.4.4 с ядрами gfx803; llama.cpp собирается и видит RX 580, но test-backend-ops ROCm0 — **134 FAIL** (f32 GEMM, MUL_MAT_ID, OUT_PROD, запись за границы) | **отклонено** (§6–7) |
| 6 | legacy/custom ROCm gfx803 | trixie: HIP 5.7.1, rocBLAS 5.5.1 (gfx803 есть), но llama.cpp b11223 требует HIP ≥ 6.1 | см. §6 |
| 7 | иные бэкенды Polaris | OpenCL (Mesa Clover/rusticl) — ggml OpenCL-бэкенд нацелен на Adreno/Intel; CPU-fallback — не production (§2) | отклонено |

Главные находки: (1) Vulkan-путь работоспособен и точен (Q8_0: gate PASS на двух пробах у 13 из 14 выходов, §0a); (2) патч 0002
экономит до 1,6 GiB VRAM на Qwen3-классе и делает две резидентные модели тривиально помещающимися в 8 GiB;
(3) **runtime PM** хоста (BACO через 5 с простоя) вытесняет VRAM модели в системную память — первый запрос после простоя
≈ 0,93 с вместо ≈ 20 мс; решение — keepalive сервиса (CP-31), хостовая настройка — по желанию пользователя;
(4) **память хоста, а не VRAM, — реальный расход llama-server-энкодеров**: prompt cache сервера (до 8 GiB RSS у causal
Qwen3) и буфер логитов (1–2 GiB закреплённой памяти на процесс) — оба не нужны для эмбеддингов и отключены
(`--cache-ram 0`, патч 0004), §2.1b.

## 0a. Итоговый отчёт RX580 (§62)

| Пункт §62 | Значение |
|---|---|
| Точная модель | AMD Radeon RX 580 Series (Ellesmere / Polaris 20, PCI 1002:67df rev e7), 36 CU, gfx803 (GCN4) |
| VRAM | **8 GB** — 8192 MiB (`mem_info_vram_total` = 8 589 934 592 B; ggml: «8192 MiB»; ROCm: «8192 MiB») |
| Драйвер | amdgpu (ядро Linux 6.12.107, Debian 13.6), DRM 3.61, KFD-узел есть; runtime PM: BACO через 5 с |
| Mesa / RADV / Vulkan | Mesa 25.0.7 (trixie), RADV POLARIS10, Vulkan API 1.4.305 (conformance 1.4.0.0); проверена и Mesa 26.2.3 (sid) |
| gfx | gfx803 (подтверждено `rocminfo`: ISA `amdgcn-amd-amdhsa--gfx803`) |
| Бэкенд, commit | llama.cpp / ggml Vulkan, `4da6337767f973e2b4d0797e5b323d77d8565e4a` (b11223); образ `vkm-rx580-llama:4da6337767f9-vk` |
| Патчи | 0001 конвертер (pplx/Giga bidirectional Qwen3, jina-colbert-v2, mmBERT-словарь); 0002 Qwen3 embeddings (без lm_head; без KV для non-causal); 0003 base64-передача токенных матриц; 0004 embeddings-контекст без буфера логитов; sha256 — `infra/core/rx580/patches/SHA256SUMS` |
| Квант | **Q8_0** — production (gate PASS на dev- и canary-пробах у всех выходов, кроме multi-vector BGE-M3); Q6_K — FAIL у pplx-embed и Giga; Q5_K_M — FAIL у 5 из 9 проверенных; F16 — эталонная точность (единственный вариант для multi-vector BGE-M3), но в 2–5 раз медленнее |
| Модели | 14 кандидатов работают: 9 dense (granite 97m/311m r2, jina-v5 nano/small, Qwen3-Emb-0.6B, pplx-embed, pplx-context, Giga-480M, mDenseOn), BGE-M3 (dense+sparse+multi-vector), 4 ColBERT (pplx-late, mLateOn, jina-colbert-v2 128/64); VRAM 100–1200 MiB на процесс, p50 запроса 6–32 мс (Q8_0) — [MODEL_MATRIX](AGENT_K_RX580_MODEL_MATRIX.md) §2 |
| Резидентны вместе | 7 пар dense + late — PASS (обе в VRAM, конкурентные запросы, 0 ошибок/OOM/reload). Кандидат granite-311m + mLateOn: 371 MiB; MODE C p50/p95 11,6/14,4 + 11,8/15,0 мс; MODE D 25,0/26,5 мс (87 q/s) + 25,2/26,8 мс (81 q/s) — [CONCURRENCY_RESULTS](AGENT_K_RX580_CONCURRENCY_RESULTS.md) |
| Parity | FINAL_CORPUS_ENCODER_APPROVED (Q8_0, dev 1918 + canary 1788 объектов): granite-97m/311m r2, jina-v5 nano/small, Qwen3-Emb, pplx-embed, Giga-480M, mDenseOn, BGE-M3 dense, pplx-late, mLateOn, jina-colbert-v2 128/64; NOT_APPROVED в Q8_0: BGE-M3 multi-vector (токенный p1 0,975/0,949) — в **F16 одобрены все три выхода BGE-M3** (p50 запроса 93 мс); pplx-context — PASS на обеих пробах, одобрен при правиле контекстов ≤ 2048 токенов — [PARITY_RESULTS](AGENT_K_RX580_PARITY_RESULTS.md) §3 |
| Память хоста | с `--cache-ram 0` и патчем 0004: процесс энкодера 0,3–0,4 GiB RSS и 32–190 MiB GTT (было 0,3–8,5 GiB RSS и 1–2 GiB GTT); сервис с парой granite-311m + mLateOn: VRAM 172 + 171 MiB, GTT 32 + 32 MiB |
| Runtime PM | keepalive 2 с: запрос после паузы p50 12 мс (без keepalive — 877 мс, GPU в BACO) — §2.1 |
| Сервис на CORE | `vkm-rx580-retrieval` на 127.0.0.1:8790 (временная пара granite-311m-r2 + mLateOn Q8_0, обе одобрены): live-тесты `gpu and services` — 2/2 PASS; `/embed/query` p50/p95 13,6/19,8 мс (dense), 13,7/16,5 мс (late), 16,2/16,6 мс (обе параллельно), 8 клиентов — 207 запросов/с при p95 53 мс |
| HIP / Mesa 26 | HIP (ROCm 6.4, gfx803) собирается и работает, но 134 провала test-backend-ops и в 1,9–2,8 раза медленнее — отклонён; Mesa 26.2 — те же числа, +5–13 % пропускной способности — кандидат на будущее (§2.2, §6–7) |

## 1. Железо и стек CORE (FACT, 28.09.2026)

| Параметр | Значение | Источник |
|---|---|---|
| GPU | AMD Radeon RX 580 (Ellesmere, PCI 1002:67df rev e7), 36 CU (4 SE × 9), gfx803 (GCN4 Polaris 10/20) | `lspci -nn`, dmesg amdgpu, `rocminfo` |
| VRAM | **8192 MiB** (`mem_info_vram_total` = 8589934592); видимая CPU часть (BAR) 256 MiB; GTT 16 GiB | sysfs, dmesg «8192M of VRAM memory ready» |
| Шина | PCIe 3.0 x16 (8 GT/s; в простое 2,5 GT/s) | sysfs `max_link_speed`, `current_link_speed` |
| Ядро / драйвер | Linux 6.12.107 (Debian 13.6), amdgpu 3.61.0, KFD-узел создан | `uname -r`, dmesg |
| Mesa / RADV | 25.0.7 (trixie); RADV POLARIS10, Vulkan API 1.4.305, conformance 1.4.0.0 | `vulkaninfo --summary` |
| VBIOS | ATOMBIOSBK-AMD VER015.050.002.001 | sysfs `vbios_version` |
| Частоты | sclk 300–1366 MHz (8 уровней), mclk 300/1000/2000 MHz | `pp_dpm_sclk`, `pp_dpm_mclk` |
| Runtime PM | `power/control = auto`, `autosuspend_delay_ms = 5000`, BACO; за 19 сут. GPU был активен ≈ 445 с | sysfs `power/*`, dmesg «Using BACO for runtime pm» |
| CPU / RAM | Ryzen 7 5800X (8C/16T, Zen 3, AVX2), 31 GiB; на хосте работают Neo4j и OpenSearch | `/proc/cpuinfo`, `free` |

## 2. Vulkan / RADV на Polaris: возможности (FACT, `vulkaninfo`)

| Свойство | Значение | Следствие для ggml |
|---|---|---|
| shaderFloat16 | **false** (у GFX8 нет packed fp16; RADV не экспонирует fp16-арифметику) | матмулы идут с fp32-аккумуляцией; F16-веса только как формат хранения |
| storageBuffer16BitAccess / 8BitAccess | true / true | чтение f16/int8 весов из буферов работает |
| shaderInt8, integer dot product | int8 = true; `VK_KHR_shader_integer_dot_product` есть, но без аппаратного dp4a | ggml: `int dot: 0` → квантованные матмулы через деквантизацию в fp32 |
| subgroup | 64 (wave64), subgroupSizeControl = true | ядра ggml используют wave64 |
| Shared memory (LDS) | 64 KiB на workgroup | тайлы mul_mm ggml помещаются |
| Cooperative matrix | нет | нет tensor-core-подобного пути (как и ожидалось для GCN4) |
| Кучи памяти | 7,75 GiB device-local + 256 MiB device-local host-visible + 15,6 GiB GTT | модель и буферы размещаются в VRAM (после первой отправки команд) |
| Очереди | 1 graphics + **4 compute** (+ sparse); в ядре 8 compute-колец | два процесса-модели получают разные аппаратные очереди → параллельное исполнение (§29, отчёт CONCURRENCY) |

ggml о устройстве: `AMD Radeon RX 580 Series (RADV POLARIS10) (radv) | uma: 0 | fp16: 0 | bf16: 0 | fp4: 0 | warp size: 64
| shared memory: 65536 | int dot: 0 | matrix cores: none`.

### 2.1 Размещение буферов и runtime PM

- Буферы, которые llama.cpp создаёт как device-local, после загрузки физически лежат в GTT и **мигрируют в VRAM при
  первой отправке команд** (amdgpu валидирует BO лениво). Поэтому VRAM измеряется только после прогрева
  (`/proc/<pid>/fdinfo`: `drm-memory-vram`), а сервис делает прогревочный запрос при старте.
- Через 5 с простоя GPU уходит в BACO; драйвер **вытесняет всю VRAM процессов в системную память**
  (`amd-evicted-vram` в fdinfo). Следующий запрос пробуждает GPU и возвращает буферы: **≈ 930–953 мс** против
  18–35 мс в тёплом состоянии (измерено 5 раз на Qwen3-Embedding и granite; `cold_after_idle` в результатах матрицы).
- Решение (CP-31): keepalive сервиса — каждые `VKM_RX580_KEEPALIVE_S` (по умолчанию 2 с) модель, не получавшая
  запросов в этом окне, получает крошечный запрос (1–2 токена); счётчики keepalive отдельно от трафика. Хостовая
  альтернатива (`power/control = on` для 0000:08:00.0 через udev-правило) — системная настройка; агент её не менял,
  координатор передал пользователю как опцию.
- **Измерено на сервисе** (образ `vkm-rx580-retrieval:0.1.0`, пара granite-311m-r2 + mLateOn Q8_0, запрос
  `/embed/query role=both` после пауз 7–9 с, 8 раз; `service_idle_probe.py`):

  | `VKM_RX580_KEEPALIVE_S` | Состояние GPU перед запросом | После паузы p50 / p95, мс | Тёплый запрос p50 / p95, мс |
  |---|---|---|---|
  | 2 (по умолчанию) | active (8 из 8) | 12,2 / 39,4 | 8,9 / 14,1 |
  | 0 (выключен) | suspended (8 из 8) | 877 / 893 | 8,9 / 14,5 |

  Keepalive стоит ≈ 1 запрос из 1–2 токенов на модель раз в 2 с простоя (51 за ≈ 100 с окна пробы); обе модели всё
  время резидентны. Цена — GPU не уходит в BACO (энергопотребление простоя выше); хостовая настройка дала бы тот же
  эффект без запросов.
- Чтение `gpu_busy_percent`/`pp_dpm_*` может само будить устройство; `/health` сервиса читает только счётчики менеджера
  памяти и `power/runtime_status` (режим `light`).

### 2.1a BAR 256 MiB и вторая резидентная модель

ggml-vulkan размещает буферы устройства «ReBAR-first»: сначала память `DEVICE_LOCAL | HOST_VISIBLE`, затем
`DEVICE_LOCAL`. У RX580 без Resizable BAR видимая CPU часть VRAM — 256 MiB (куча 2). Первая модель занимает её, а буферы
второго процесса amdgpu кладёт в GTT (системная память через PCIe): в сервисе с двумя моделями mLateOn Q8_0 имел
56 MiB VRAM и 153 MiB GTT (веса слоёв целиком в GTT, `amdgpu_gem_info`: BO 117 MB `GTT CPU_ACCESS_REQUIRED`).
`GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1` оставляет только `DEVICE_LOCAL` → обе модели целиком в VRAM (171,7 и
170,9 MiB). Переменная задана в образах, харнесе и сервисе; `/health` считает модель резидентной, только если её
буферы в VRAM (не вытеснены runtime PM и не ушли в GTT).

### 2.1b Память хоста процессов llama-server (RSS и GTT)

VRAM энкодеров мала (§0a, MODEL_MATRIX), но системная память процессов оказалась большой. Измерено по
`/proc/<pid>/status` (RssAnon/RssFile) и DRM fdinfo (`drm-memory-gtt`) в конце каждого прогона матрицы:

| Находка | Факт (CORE, b11223 + 0001–0003) | Причина (код на пине) | Решение |
|---|---|---|---|
| Prompt cache сервера | RSS `llama-server` у causal Qwen3 (Qwen3-Embedding, jina-v5-small) — **8,4–8,5 GiB**; у encoder-моделей (BERT/ModernBERT/EuroBERT, bidirectional pplx) — 0,25–0,6 GiB | `--cache-ram` по умолчанию 8192 MiB, `--cache-idle-slots` по умолчанию включён: при каждой новой задаче (любого типа, в том числе embedding) KV-состояние простаивающих слотов копируется из VRAM в RAM; у causal Qwen3-0.6B KV ≈ 112 KiB на токен, у моделей без KV-кеша сохранять нечего | `--cache-ram 0` в харнесе и сервисе (без пересборки); прогоны после 04:00 UTC 28.09 записаны с ним (`argv` в результате) |
| Буфер логитов | GTT процесса растёт с 70–280 MiB после прогрева до **1,0–2,1 GiB** после пакетов документов; прирост ≈ n_vocab × 2048 × 4 Б (1,0 GiB при словаре 128k … 2,0 GiB при 256–262k) — закреплённая (pinned) системная память | `llama_context::output_reserve` при `has_logits = true` резервирует n_vocab × n_outputs float и в embeddings-контексте; там все токены — выходы (n_outputs до n_batch = 2048); буфер создаётся в pinned host-памяти Vulkan и обнуляется | патч 0004: `has_logits = !cparams.embeddings` (прежнее поведение llama.cpp); извлечение логитов на пине уже защищено проверкой `logits.data != nullptr` |

Проверка 0004 — A/B двух бинарников (dev-сборка 0001–0003 против образа 0001–0004) на одном GGUF и одних token ids
(`bench ab`: 60 запросов + 512 документов, серверы по очереди, `--cache-ram 0` у обоих):

| Модель (Q8_0) | Выходы A и B | GTT процесса A → B, MiB | VRAM A / B, MiB | RSS A / B, MiB |
|---|---|---|---|---|
| granite-311m-r2 (ModernBERT, словарь 262k) | побитно равны (max abs diff 0; 60 + 512 векторов) | 823 → 34 | 184 / 182 | 405 / 405 |
| mLateOn (ModernBERT, токенные матрицы) | побитно равны | 819 → 34 | 186 / 184 | 354 / 356 |
| Qwen3-Embedding-0.6B (causal, `decode()`) | побитно равны | 710 → 183 | 1198 / 1197 | 296 / 297 |
| pplx-embed-0.6b (bidirectional, `encode()`) | побитно равны | 713 → 187 | 724 / 723 | 291 / 292 |

Буфер логитов растёт с самым большим пакетом (здесь ≤ 0,8 GiB; в матрице с пакетами длинных документов —
до 2,1 GiB); с 0004 его нет. Вместе с `--cache-ram 0` процесс энкодера занимает на хосте ≈ 0,3–0,4 GiB RSS и
35–190 MiB GTT вместо 0,3–8,5 GiB RSS и 1–2 GiB GTT.

### 2.2 Mesa 26.2 (Debian sid) против 25.0.7

Тот же исходник и GGUF, сборка в контейнере `vkm-rx580-research:sid` (Mesa 26.2.3), 4 модели Q8_0 (таблица §7):
parity совпадает до пятого знака (cos, top-10, Spearman те же, что у Mesa 25); пропускная способность документов
выше на 5–13 % (granite-311m 84,6 → 89,1 док/с, jina-v5-nano 87,7 → 93,7, mLateOn 77,3 → 85,8, Qwen3-Emb
14,9 → 16,8), p95 запроса ниже на 1–2 мс, p50 тот же (± 0,6 мс); VRAM +0–20 MiB. Оговорка: прогоны Mesa 26 шли с
`--cache-ram 0`, базовые Mesa 25 — без (для encoder-моделей это не важно; для Qwen3 часть выигрыша может быть отсюда).
Решение: production остаётся на Mesa 25.0.7 (тот же релиз Debian, что на хосте, образ по digest); Mesa 26 —
кандидат на обновление, когда появится в стабильной ветке Debian (или в закреплённом снимке), выигрыш ≈ 5–10 % не
оправдывает нестабильную базу.

## 3. llama.cpp / ggml Vulkan

- Пин: `4da6337767f973e2b4d0797e5b323d77d8565e4a` (тег b11223, 27.09.2026) — HEAD на момент начала работ и тот же
  коммит, что у реранкера m0 на EDGE (F): одна версия llama.cpp на платформе.
- Сборка: Docker (`infra/core/rx580/Dockerfile`: `toolchain` → `build` → `runtime`; база `debian:trixie-slim` по
  digest; glslc 2025.2, cmake 3.31.6, gcc 14.2); `GGML_VULKAN=ON`, статические библиотеки, AVX2/FMA/F16C для Zen 3.
  Обоснование Docker против нативной сборки: хост не меняется (glslc/libvulkan-dev не ставятся), Mesa берётся из того
  же релиза Debian, что и на хосте, либо любая другая для эксперимента (sid, §2.2), образ воспроизводим по digest и
  совпадает с правилом D9 «сервисы VKM на CORE работают в Docker». Доступ к GPU — только `/dev/dri` + группы render/video.
- `test-backend-ops -b Vulkan0` (режим test, все операторы): **18834/18834 PASS**, 4105 случаев «not supported»
  (OUT_PROD, CONV_2D, отдельные формы MUL_MAT для mxfp4/nvfp4/q1_0/iq2_xxs и т. п.) — ни один не встречается в графах
  кандидатов (`graph splits = 1`, всё на GPU). Время прогона 4 мин 53 с.

### 3.1 Поддержка архитектур кандидатов (на пине b11223)

| Модель (§6, §8) | HF-архитектура | llama.cpp arch | Поддержка на пине | Что сделано |
|---|---|---|---|---|
| Qwen3-Embedding-0.6B | Qwen3ForCausalLM | qwen3 | есть (causal, pooling last) | патч 0002 (без lm_head) |
| jina-embeddings-v5-text-small-retrieval | Qwen3Model | qwen3 | есть; официальные GGUF Jina (imatrix) | патч 0002 (без lm_head) |
| jina-embeddings-v5-text-nano-retrieval | EuroBertModel | eurobert | есть; официальные GGUF Jina | — |
| granite-embedding-311m/97m-multilingual-r2 | ModernBertModel (GeGLU / SwiGLU) | modern-bert | есть (sliding window 128, SiLU для 97m) | — |
| mDenseOn / mLateOn | ModernBertModel (mmBERT, Gemma-2 токенизатор) | modern-bert | граф есть; **конвертер падал** (неизвестный pre-tokenizer hash) | патч 0001: SPM-словарь из tokenizer.json |
| pplx-embed-v1 / context / late 0.6b | PPLXQwen3Model (bidirectional Qwen3) | qwen3 + `attention.causal=false` | **конвертер не знал архитектуру** | патч 0001 (регистрация) + 0002 (без KV-кеша для non-causal) |
| Giga-Embeddings-instruct-480M | Qwen3BidirectionalModel (словарь 128 256) | qwen3 + `attention.causal=false` | **конвертер не знал архитектуру** | патч 0001 (регистрация, запасной pre-tokenizer) + 0002 |
| BGE-M3 | XLMRobertaModel | bert | есть (dense CLS) | головы sparse/ColBERT — в шлюзе (numpy) |
| jina-colbert-v2 | HF_ColBERT (XLM-R + rotary) | jina-bert-v3 | граф есть; **конвертер не знал HF_ColBERT** | патч 0001: backbone → jina-bert-v3; голова 1024→128 — в шлюзе |

Головы ColBERT (pplx-late 1024→128, mLateOn — три линейных слоя без активаций и смещений, свёрнутые в одну матрицу
128×768, jina-colbert-v2 1024→128, BGE-M3 colbert/sparse) применяются к токенным выходам `--pooling none` в шлюзе
(numpy, ≈ 0,1–1 мс на запрос). Перенос головы в граф (`dense_2`) — возможная оптимизация объёма передачи для массового
кодирования документов, не нужна для корректности.

## 4. Патчи (sha256 в `infra/core/rx580/patches/SHA256SUMS`)

| Патч | Содержание | Проверка |
|---|---|---|
| `0001-convert-pplx-jina-colbert-mmbert.patch` | конвертер: `PPLXQwen3Model` → qwen3 с `causal_attention=False` (+ префикс `model.` у тензоров); `HF_ColBERT` → jina-bert-v3 backbone без головы и pooler; ModernBERT с Metaspace-BPE (Gemma-2) → SPM-словарь `_set_vocab_llama_hf` | parity RX580 против HF fp32 (матрица моделей) |
| `0002-qwen3-embeddings-no-kv-no-lm-head.patch` | Qwen3 в embeddings-контексте: (а) не строится `lm_head` (n_tokens × 151 936 логитов — ≈ 1,2 GiB compute-буфера при ubatch 2048); (б) для **bidirectional** Qwen3 (pplx) нет KV-кеша: последовательность кодируется целиком через `encode()` | Qwen3-Embedding Q8_0: память устройства 2344 → 699 MiB (ctx 4096, ubatch 2048); parity cos ≈ 0,9995 |
| `0003-server-embedding-base64.patch` | `llama-server`: `POST /embedding` с `"encoding_format": "base64"` отдаёт токенные матрицы (pooling none) как base64 float32 (`embedding_b64`, `n_rows`, `n_cols`) вместо десятичного JSON — ColBERT/BGE-M3 выдают 128–1024 чисел на токен | численно ничего не меняет (те же float32); клиент принимает оба формата |
| `0004-embeddings-context-no-logits-buffer.patch` | `llama_context::output_reserve`: в embeddings-контексте не резервируется буфер логитов (n_vocab × n_outputs float в pinned host-памяти, §2.1b) | A/B `bench ab` на 4 моделях (ModernBERT pooled и токенный, causal и bidirectional Qwen3): выходы побитно равны, GTT −0,5…−0,8 GiB на процесс (§2.1b) |

Урок первой версии 0002 (исправлено в тот же час): v1 отключала KV-кеш для **любого** Qwen3 в embeddings-режиме, но без
памяти `llama_context::decode()` вызывает `encode()`, а тот всегда ставит non-causal attention. Causal-модели
(Qwen3-Embedding, jina-v5-small) дали cos ≈ 0,2–0,3 против эталона; parity-харнес поймал это сразу (и на Vulkan, и на
CPU; непропатченный CPU-сервер давал 0,999). v2 ограничивает «без KV-кеша» non-causal моделями.

## 5. Собственные Vulkan-ядра

Не написаны: все операторы графов кандидатов поддержаны и проходят test-backend-ops; parity PASS. Производительность
ограничена аппаратно: у GCN4 нет packed fp16 и dp4a, поэтому ggml считает fp32 FMA после деквантизации. Q8_0 быстрее
F16 в 2–2,6 раза (меньше байт на вес при той же арифметике; F16-ветка без fp16-арифметики у RADV GFX8 медленная), см.
MODEL_MATRIX. Возможные направления, если понадобится: ядро int8×int8→int32 через 4×`v_mad_u32_u24` для Q8_0-матмулов
(эмуляция dot4), перенос ColBERT-головы в граф.

## 6. HIP / ROCm для gfx803

«Официально не поддерживается» — не ответ (§25); проверено фактически:

- Ядро: amdgpu KFD создаёт узел для 1002:67df (`/dev/kfd`, группа render) — пользовательскому ROCr-рантайму ядро не
  мешает.
- Debian trixie (хост): HIP 5.7.1 (`libamdhip64-5`), HSA runtime 6.1.2, **rocBLAS 5.5.1 содержит Tensile-ядра gfx803**
  (FACT: `dpkg -c librocblas0_5.5.1+dfsg-7`). Но llama.cpp b11223 требует HIP ≥ 6.1 (`ggml-hip/CMakeLists.txt`).
- Debian sid (контейнер `vkm-rx580-research:sid`, без изменений хоста): HIP 6.4.3, hipcc 7.0.2 (LLVM 21),
  **rocBLAS 6.4.4 с 68 файлами ядер gfx803** (FACT: `dpkg -c librocblas4_6.4.4-4`); `rocminfo` в контейнере с
  `/dev/kfd` видит агента `gfx803` «AMD Radeon RX 580 Series» (ISA `amdgcn-amd-amdhsa--gfx803`).
- llama.cpp b11223 (тот же пин и патчи) **собирается под HIP для gfx803** (`GGML_HIP=ON`, `GPU_TARGETS=gfx803`,
  hipcc/LLVM 21 из Debian sid) и видит устройство: `ROCm0: AMD Radeon RX 580 Series, gfx803, Wave Size 64,
  VRAM 8192 MiB` (KFD без xnack).
- **Корректность — нет:** `test-backend-ops -b ROCm0` — **16311/16445, 134 FAIL** (Vulkan на том же пине — 18834/18834).
  Падают базовые операторы, которые используют наши графы: `MUL_MAT` f32×f32 (ошибка 1,5–7 при допуске 5·10⁻⁴,
  в том числе формы 16×16×256 и 128×1×1057), `MUL_MAT` квантованных типов при n = 64 (q4_K, q5_K, q5_0/1, iq…:
  0,001–0,04), `MUL_MAT_ID`, `OUT_PROD`, `SOLVE_TRI`; в 7 случаях — «sentinel mismatch», то есть запись за пределы
  тензора. Вероятная причина — вне llama.cpp: gfx803 нет в списке GPU, поддерживаемых ROCm 6.x, ядра rocBLAS для
  gfx803 — сборка дистрибутива (Debian) без гарантий AMD (INTERPRETATION; отдельно не локализовано, ядро с ошибкой
  не искалось). Итог по моделям — §7.

## 7. Альтернативные бэкенды на тех же моделях: HIP (ROCm 6.4, gfx803) и Mesa 26.2

Тот же исходник (пин + патчи), те же GGUF, эталон и харнес; сборки — в контейнере `vkm-rx580-research:sid`
(`/dev/dri` + `/dev/kfd`), без изменений хоста. Варианты: `vk-mesa25` (production), `mesa26` (Vulkan, Mesa 26.2.3),
`hip` (ROCm 6.4.3, `GPU_TARGETS=gfx803`).

<!-- VARIANTS:BEGIN -->
| Model | Quant | Backend | Load s | VRAM MiB (proc) | Embed | cos mean | cos p1 | top-10 | top-50 | Spearman | Gate | q p50 ms | q p95 ms | docs/s | tok/s | busy % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| granite-311m-r2 | Q8_0 | hip | 1.21 | 490.7 | yes | 0.99996 | 0.99991 | 0.978 | 0.983 | 0.9962 | PASS | 12.3 | 14.5 | 30.8 | 3286.9 | 99.2 |
| granite-311m-r2 | Q8_0 | mesa26 | 1.00 | 192.1 | yes | 0.99998 | 0.99996 | 0.983 | 0.986 | 0.9982 | PASS | 11.5 | 12.2 | 89.1 | 9528.5 | 97.2 |
| granite-311m-r2 | Q8_0 | vk-mesa25 | 0.80 | 172.5 | yes | 0.99998 | 0.99996 | 0.983 | 0.986 | 0.9982 | PASS | 11.6 | 14.0 | 84.6 | 9043.6 | 96.1 |
| jina-v5-nano-retrieval | Q8_0 | hip | 1.20 | 514.6 | yes | 0.99976 | 0.99950 | 0.980 | 0.988 | 0.9973 | PASS | 12.1 | 14.7 | 34.8 | 4063.1 | 99.2 |
| jina-v5-nano-retrieval | Q8_0 | mesa26 | 0.60 | 226.1 | yes | 0.99989 | 0.99977 | 0.987 | 0.991 | 0.9986 | PASS | 9.8 | 10.3 | 93.7 | 10949.4 | 98.5 |
| jina-v5-nano-retrieval | Q8_0 | vk-mesa25 | 1.20 | 211.1 | yes | 0.99989 | 0.99977 | 0.987 | 0.991 | 0.9986 | PASS | 9.7 | 11.7 | 87.7 | 10256.5 | 94.9 |
| mlateon | Q8_0 | hip | 0.80 | 486.6 | yes | 0.99995 | 0.99960 | 0.972 | 0.965 | 0.9858 | PASS | 14.3 | 16.8 | 30.0 | 3299.6 | 98.1 |
| mlateon | Q8_0 | mesa26 | 0.60 | 191.8 | yes | 0.99998 | 0.99971 | 0.977 | 0.981 | 0.9951 | PASS | 12.3 | 13.3 | 85.8 | 9428.2 | 86.8 |
| mlateon | Q8_0 | vk-mesa25 | 0.60 | 192.0 | yes | 0.99998 | 0.99971 | 0.977 | 0.981 | 0.9951 | PASS | 11.9 | 15.3 | 77.3 | 8492.8 | 86.0 |
| qwen3-emb-0.6b | Q8_0 | hip | 0.80 | 1565.2 | yes | 0.99933 | 0.99889 | 0.963 | 0.978 | 0.9910 | PASS | 35.0 | 50.5 | 7.9 | 977.1 | 99.5 |
| qwen3-emb-0.6b | Q8_0 | mesa26 | 0.80 | 1199.7 | yes | 0.99968 | 0.99949 | 0.977 | 0.985 | 0.9956 | PASS | 28.6 | 29.5 | 16.8 | 2074.8 | 97.8 |
| qwen3-emb-0.6b | Q8_0 | vk-mesa25 | 0.60 | 1186.3 | yes | 0.99968 | 0.99949 | 0.977 | 0.985 | 0.9956 | PASS | 28.0 | 32.9 | 14.9 | 1849.0 | 93.4 |
<!-- VARIANTS:END -->

- **HIP отклонён.** Эмбеддинги на этих 4 моделях проходят gate, но с меньшим запасом (top-10 0,963–0,980 против
  0,977–0,987 у Vulkan), документы кодируются в 1,9–2,8 раза медленнее, VRAM в 1,3–2,8 раза больше (491–1565 против
  172–1200 MiB), RSS процесса 1,5–2,7 GiB (рантайм HIP + host-буферы), DRM-учёт времени движков отсутствует; главное —
  134 провала test-backend-ops в базовых операторах (§6), то есть корректность других форм графа не гарантирована.
- **Mesa 26.2** — те же числа parity, +5–13 % пропускной способности (§2.2); production — Mesa 25.0.7.

## 8. Риски

- Runtime PM: без keepalive (или хостовой настройки) первый запрос после ≥ 5 с простоя ≈ 1 с (§2.1).
- CPU хоста общий с OpenSearch/Neo4j: llama-server держит 2 потока на модель (`-t 2`), токенизация и головы — в сервисе;
  тяжёлые сборки и бенчмарки — с `nice`, пауза на время reconcile координатора.
- F16-веса на Polaris медленнее Q8_0 в 2–2,6 раза (у BGE-M3 латентность запроса ×3) — F16 только там, где Q8_0 не
  проходит gate (multi-vector BGE-M3).
- Квантование K-типов у моделей со скрытым размером, не кратным 256 (granite-97m: 384), падает на запасные типы:
  Q5_K_M этой модели не проходит parity-gate.
- Память хоста: без `--cache-ram 0` и патча 0004 процесс `llama-server` занимает до 8,5 GiB RSS и 1–2 GiB закреплённой
  памяти (§2.1b); оба исправления — в образе и сервисе, но любой запуск стокового `llama-server` на CORE их теряет.
- Патчи 0001–0004 — локальные к пину b11223; при обновлении llama.cpp их нужно переносить и заново проверять
  (parity + A/B), sha256 — в `SHA256SUMS`.
- Энергопотребление: keepalive держит GPU вне BACO круглосуточно (цена CP-31); хостовая альтернатива — решение
  пользователя.
