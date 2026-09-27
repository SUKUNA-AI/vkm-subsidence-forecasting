# AGENT F — text + visual reranker на EDGE (jina-reranker-v3.5 + jina-reranker-m0)

Phase 0 (discovery/design), read-only. Дата: 28.09.2026. Роль: F — RETRIEVAL / MODEL SERVICES ENGINEER. Редакция 2:
обновлено по данным инспекции EDGE.

**Статус разделов**

- **ПОДТВЕРЖДЕНО инспекцией EDGE** (сырьё агента B от 28.09.2026 00:45–01:06, прочитано локально, read-only):
  §1.4, §3.1, §4.1, §6.1, §8.1.
- **DRAFT** (дизайн до прототипа и измерений): §2, §3.2–3.4, §4.2–4.4, §5, §6.2–6.7, §7, §9.
- Собственная SSH-инспекция агента F не выполнялась: команду отклонила система разрешений сессии, обход не
  предпринимался. Использованы файлы агента B: `edge_01_system`, `edge_02_network`, `edge_03_docker`,
  `edge_03b_docker_detail`, `edge_04_systemd_cron_tools_du`, `edge_09_reranker`, `edge_12/13_careerops_*`,
  `edge_14_readiness`, `misc_15_final_checks`, `misc_16_pg_native_ufw`.
- Сырьё ресерча F лежит в git-ignored `<PUBLIC>/work/corpus_platform/phase0/agent_f/raw/research/`.

Метки:

- `[FACT]` — первоисточник или сырьё B;
- `[DERIV]` — арифметика из фактов;
- `[EST]` — оценка, подлежит измерению;
- `[MEM]` — по памяти кода/документации, в этой сессии не перепроверено.

## 0. Итог

1. **Text-reranker на EDGE — сценарий T3, резидентный, сохраняется без изменений.** `[FACT]`
   - Docker-контейнер `careerops-reranker` (Compose-проект `careerops-reranker`, образ
     `careerops-reranker:2.14-cu132-fp16`, `network_mode: host`).
   - Python `http.server`, transformers 4.57.3, PyTorch 2.14.0+cu132, backend `transformers-cuda`, **fp16 без
     квантизации**.
   - Модель: `jinaai/jina-reranker-v3.5` @ `e8a93f33f0b22108f8c2364f8484ce3422552fbc`; тот же revision у кода и
     токенизатора.
   - Работает с 10.09.2026, статус healthy; idle VRAM процесса **1212 MiB**.
   - API: `POST /v1/rerank`, `GET /readyz`, `GET /healthz`; порт 18082 на `0.0.0.0`.
   - Резервного пути «subprocess на каждый запрос» (T2) нет, поэтому **D-F2 больше не нужен**.
2. **Полезная ёмкость GPU — 3716 MiB, а не 4096.** 4096 total − 1216 used − 2500 free = **380 MiB зарезервировано**
   драйвером `[DERIV]`. Сейчас свободно 2500 MiB. dGPU дисплей не выводит (`Disp.A Off`, на GPU один процесс).
   Persistence mode On, compute mode Default. `[FACT]`
3. **Пик памяти v3.5 растёт с длиной запроса.** В `modeling.py` (@ `e8a93f33…`) стоит `output_hidden_states=True`: во
   время прохода держатся все 29 hidden states, ≈ 58 KiB/токен fp16. Код модели сам укладывает в один проход до ~125
   документов и десятки тысяч токенов `[FACT: код]`. Allocator PyTorch не отдаёт память обратно, пик становится новым
   «idle» до рестарта. Оценки `[EST]`:
   - ≈ 1.4 GiB при 2K токенов на запрос;
   - ≈ 1.6 GiB при 4K;
   - ≈ 2.0 GiB при 8K;
   - ≈ 2.9 GiB при 16K.
4. **Ограничивать v3.5 без изменения сервиса можно только на входе, через gateway.**
   - Токены считаются тем же токенизатором (`tokenizer.json`, sha256 `4e95945a…`, совпадает с кэшем сервиса).
   - Лимит запроса ≤ 4096 токенов суммарно: ≤ 24 кандидата × ≤ 160 токенов плюс запрос.
   - К text-бэкенду одновременно в работе не больше одного запроса. Это ограничение на один бэкенд (per-backend), а не
     глобальный lock: visual-путь независим.
   - Риск: порт 18082 открыт на `0.0.0.0`, ufw неактивен `[FACT]`, поэтому прямые LAN-клиенты могут обойти лимиты
     (D-F7).
5. **m0 целиком на GPU (≈ 2.3–2.9 GiB) рядом с v3.5 под нагрузкой не помещается** `[EST]`.
   - Рекомендация: **Q6_K сохраняется** (требование ~Q6 выполнено), меняется раскладка.
   - На GPU: vision encoder (mmproj Q8_0, самая тяжёлая по FLOP часть) и столько слоёв LLM, сколько позволяет бюджет;
     output-слой и остаток LLM — на CPU.
   - Формула бюджета: `B_m0 = 3716 − P_text − 128 MiB`.
   - При лимите 4K у v3.5 это ≈ 1950 MiB: ViT + ~24 из 28 слоёв; ≈ 5–7 с на изображение `[EST]`.
   - Отклонение «m0 резидентен в VRAM частично» документируется (D-F2′).
6. **Патч «skip lm_head» (D-F1) — теперь оптимизация, а не условие.** При `-ngl ≤ 28` output-слой и буфер логитов
   (≈ 594 MiB при `-ub 1024`) живут на CPU `[MEM]`. Патч только снимает ≈ 1–2 с CPU-времени на изображение.
   Стартуем на чистом upstream.
7. **Путь m0 + изображения в llama.cpp.** Mainline `llama-server` `/embedding` + multimodal + `--pooling last` +
   `embd_normalize: -1` `[FACT]`, плюс MLP-голова из `mlp_weights.npz` в gateway.
   - mmproj придётся конвертировать самим из чекпойнта m0: в официальном GGUF его нет.
   - Обязателен parity-тест против transformers (WORKSTATION).
8. **Развёртывание — Docker Compose**, по образцу существующего контейнера `careerops-reranker`: read_only,
   `cap_drop: ALL`, healthcheck, json-file 10m×3.
   - На EDGE уже есть Docker 26.1.5, Compose 2.26.1, NVIDIA Container Toolkit 1.20.0, драйвер 595.91.07 / CUDA 13.2,
     toolkit 13.2 в `/usr/local/cuda-13.2`. `[FACT]`
   - cmake/gcc/g++/make в выводе B не обнаружены, поэтому llama.cpp собирается внутри образа `nvidia/cuda:13.2-devel`
     с `-DCMAKE_CUDA_ARCHITECTURES=75`, а не на хосте.
   - Свободные порты: 18083 (m0, только loopback) и 18084 (gateway, LAN). `[FACT: B]`
9. **Конкурентность.** Два процесса и два CUDA context, драйвер делит GPU по времени, глобального lock нет. MPS не
   использовать: сбой одного клиента валит всех клиентов GPU `[FACT: NVIDIA]`. Лимит мощности ноутбучной GPU — 50 W;
   счётчик SW Power Capping ≈ 17 970 с `[FACT]`, то есть под нагрузкой будет троттлинг — пишем в receipt.
10. **Что координатору выполнить read-only** (в сырье B этого нет) — 4 команды из §8.2:
    - формат запроса и ответа `/v1/rerank`: в пробе B ответ `{'results': [{'index': 0}, {'index': 1}], 'usage':
      {'total_tokens': 185}}`, scores не видны — возможно, их отфильтровал вывод B;
    - однопоточный ли сервер;
    - есть ли в сервисе собственные лимиты;
    - используется ли `torch.compile`/Triton (в кэше есть каталоги Triton).
11. **Главные риски.**
    - VRAM: бюджет m0 зависит от реального пика v3.5, который меряем в Phase 4. **Каждая проба необратимо поднимает
      high-water mark сервиса до его рестарта.**
    - TU117 без tensor cores при cc 7.5.
    - Путь m0 + изображения официально не поддержан.
    - Порт 18082 открыт на LAN.
    - Лицензия NC.

## 1. jina-reranker-v3.5

### 1.1 Карточка модели

| Параметр | Значение | Статус |
|---|---|---|
| Репозиторий | `jinaai/jina-reranker-v3.5`, revision `e8a93f33f0b22108f8c2364f8484ce3422552fbc` (lastModified 2026-07-30), создан 2026-07-14 | FACT |
| Архитектура | `JinaForRanking(Qwen3ForCausalLM)`, custom code, база Qwen3-0.6B. 28 слоёв, hybrid «3L2G» attention: sliding-window слои чередуются с глобальными, окно 1024 | FACT |
| Параметры | 596 836 352 (BF16 safetensors, 1 193 708 152 B) | FACT (HF + кэш на EDGE) |
| Scoring | listwise «last but not late»: все документы и запрос в одном контексте. `lm_head = Identity`; скрытые состояния `<\|embed_token\|>` (151670) и `<\|rerank_token\|>` (151671) → projector 1024→512→512 (ReLU) → cosine. Между блоками — взвешенное слияние запроса | FACT (modeling.py, rerank.py) |
| Лимиты кода `rerank()` | запрос ≤ 1024 токенов, документ ≤ 8192. Блок = до 125 документов, пока `max_length − 2·len(query)` не опустится до 8192; `max_length = tokenizer.model_max_length`. Значит, один прямой проход может занимать десятки тысяч токенов | FACT (modeling.py) |
| Память прохода | `output_hidden_states=True`: 29 скрытых состояний × 1024 × 2 B ≈ 58 KiB/токен в fp16 держатся до конца прохода; логиты не считаются | FACT (код) + DERIV |
| Контекст | до 131 072 токенов | FACT |
| Языки | `multilingual`; русский явно не назван. Оценка на MIRACL (18 языков, включая русский) — косвенный признак | FACT + INFER |
| Лицензия | CC BY-NC 4.0 | FACT |
| Бенчмарки вендора, nDCG@10 (v3.5 / v3) | BEIR 63.20 / 62.10; MIRACL 74.11 / 72.20; RTEB 70.95 / 68.01; Struct-IR 48.3 / 38.7 | FACT (vendor) |
| Статья | arXiv 2607.18152 | FACT |

### 1.2 Официальные форматы и serving

| Формат | Serving | Резидентность |
|---|---|---|
| HF transformers (`jinaai/jina-reranker-v3.5`) | `AutoModel.from_pretrained(..., trust_remote_code=True)`; `model.rerank(query, documents, top_n=None, return_embeddings=False)` → `[{document, relevance_score, index, embedding?}]` | да, в долгоживущем процессе. **Именно так работает EDGE** |
| GGUF (`jinaai/jina-reranker-v3.5-GGUF` @ `884f7c67aa3ac24edb89064da8c7bfd03f4a90f5`) | только `llama-embedding` из форка `littlewine/llama.cpp` (upstream PR #26286 открыт, не влит) + `rerank.py`, который запускает процесс на каждый блок | нет (перечитывает веса на каждый вызов). На EDGE не используется |
| MLX | Apple Silicon | неприменимо |

Mainline llama.cpp с `--pooling rank`/`/v1/rerank` к v3.5 неприменим по двум причинам:

- v3.5 — listwise-модель с внешним projector и cosine, а не классификатор;
- mainline не поддерживает SWA-паттерн Qwen3. `[FACT + INFER]`

sha256 квантов GGUF (для справки; на EDGE не используются):

| Файл | sha256 |
|---|---|
| Q8_0 | `bedbedd688d18665448241f1aad78afb23a4476b89ae0867243e1c79aa4357b8` |
| Q6_K | `f8715093aacd087d13c81c3ab2c47109f60e3f5719d7f11c4e41bd99c6e93dda` |
| Q5_K_M | `d380967366a2a3fe3b93c173bda98558c5c0c8d8d07dfaf8999a32a314d3a096` |
| Q4_K_M | `40ec64a1b8c18a40a79bbd7b516115aec158791e56452e734c36c52a76c245a1` |
| BF16 | `d9b699dec7ae8e5ff058c3e9b767c5b7d467dc4dc38e4c6badfe061e82aabd60` |
| `projector.safetensors` | `b14c3d97315ca33490e630218c821640f183180fd971c5c3242f5b81aadcedf9` |

### 1.3 Совпадение с кэшем сервиса на EDGE

В кэше `careerops-reranker` лежат два блоба `[FACT: edge_09]`:

- `50c684b7…dfb6fe`, 1 193 708 152 B — это `model.safetensors`;
- `4e95945ab0cef486709f760b81efcc7a6e75747f9165d13ead29159737455803`, 11 423 225 B. Имя блоба в кэше HF совпадает с
  LFS sha256 `tokenizer.json` из GGUF-репозитория Jina `[FACT]`, значит это тот же файл. В Phase 4 подтвердить
  `sha256sum`.

Поэтому gateway может считать токены тем же токенизатором, что и сервис.

### 1.4 Фактическое развёртывание на EDGE — **ПОДТВЕРЖДЕНО**

| Параметр | Значение (сырьё B) |
|---|---|
| Сценарий | **T3**: transformers, резидентный процесс, не квантован (fp16) |
| Контейнер | `careerops-reranker`, Compose-проект `careerops-reranker`, compose-файл в deploy-каталоге CareerOps на EDGE (`<CAREEROPS_DEPLOY>/compose.yml`) |
| Образ | `careerops-reranker:2.14-cu132-fp16` (id `sha256:77f07d5a5230…`, 6.4 GB), база `pytorch/pytorch:2.14.0-cuda13.2-cudnn9-runtime`; `CMD python -m careerops_reranker`, `USER 10001:10001` |
| Runtime | `transformers-cuda`, `dtype_or_quantization = float16`, torch `2.14.0+cu132`, transformers `4.57.3` (из `/readyz`) |
| Модель | `jinaai/jina-reranker-v3.5`, `model_revision` = `model_code_revision` = `tokenizer_revision` = `e8a93f33…` (из `/readyz` и env) |
| Состояние | запущен 2026-09-10T10:58:55Z, `healthy`, `restart: unless-stopped`; host PID на момент инспекции 1430876, RSS ≈ 1.8 GB; `mem_limit` не задан |
| GPU | `deploy.resources.reservations.devices: nvidia, count 1, [gpu]`; VRAM процесса **1212 MiB** в idle, **1250 MiB** после одного пробного запроса B (2 документа, 185 токенов, 1.33 с) |
| Сеть | `network_mode: host`, `CAREEROPS_RERANKER_HOST=0.0.0.0`, `PORT=18082`; `ss`: LISTEN `0.0.0.0:18082`, backlog 5 (признак `http.server`); **ufw inactive** |
| API | `GET /healthz` → `{"status":"ok"}`; `GET /readyz` → `{runtime:{…}, status:"ready"}`; `POST /v1/rerank` — единственный POST-путь (`if self.path != "/v1/rerank"`). `/openapi.json`, `/health`, `/v1/models` → 404 |
| Hardening | `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, tmpfs `/tmp` 512 MB; healthcheck `/readyz` каждые 15 с; logging `json-file` 10m × 3 |
| Кэш | bind `/var/lib/careerops/reranker-cache → /cache` (HF, Triton: 2 каталога ядер) |
| Исходники | checkout репозитория CareerOps на EDGE (`<CAREEROPS_SRC>`, commit `41bc243`, ветка `ar/processing-v2`, чистый): `src/careerops_reranker/{server,runtime,config}.py` |
| Клиенты | на момент инспекции установленных соединений к 18082 нет |

Выводы:

- Сервис резидентен и сохраняется без изменений. Gateway использует его как text-бэкенд через `127.0.0.1:18082`:
  у обоих `network_mode: host`.
- Отчётные поля модели gateway берёт из `/readyz`. Их не хардкодим — так они всегда совпадают с реальностью.

## 2. jina-reranker-m0 — DRAFT

### 2.1 Карточка модели (без изменений против редакции 1)

| Параметр | Значение | Статус |
|---|---|---|
| Репозиторий | `jinaai/jina-reranker-m0` @ `94bfe0aeb2d4dd7978362699cddd5893d4e0adc8` (lastModified 2026-04-09) | FACT |
| Архитектура | `JinaVLForRanking(Qwen2VLForConditionalGeneration)`. LLM: 1536 hidden, 28 слоёв, 12 голов, 2 KV-головы, vocab 151 936, M-RoPE `[16,24,24]`. ViT Qwen2-VL: patch 14, merge 2×2 | FACT |
| Параметры | 2 444 721 665 (BF16) | FACT |
| Обучение | LoRA на LLM, vision encoder и projector взяты из pretrained Qwen2-VL (блог). База: в карточке — `-Instruct`, в README GGUF — `Qwen/Qwen2-VL-2B`. Расхождение записано | FACT |
| Score | `sigmoid(MLP(h_last) − 2.65)`, MLP = `Linear(1536,1536) → ReLU → Linear(1536,1)`; последний токен — score-токен id 100 | FACT (modeling.py) |
| Промпт (изображение) | `**Document**:\n<\|vision_start\|><\|image_pad\|><\|vision_end\|>\n**Query**:\n{query}` + id 100 | FACT |
| Предобработка | `min_pixels = 3136`, `max_pixels = 602112` → ≤ 768 visual tokens на изображение | FACT |
| Контекст | карточка: 10 240; блог: «up to 32K»; рабочий лимит — 10 240 (дефолт кода) | FACT, конфликт записан |
| Лицензия | CC BY-NC 4.0 | FACT |

Visual tokens для A4:

- длинная сторона 768 px → 756×532 → 513 токенов;
- длинная сторона 1024 px → 896×644 → 736 токенов;
- всё больше упирается в потолок ≈ 768 токенов `[DERIV]`.

Эффективное разрешение страницы — ≈ 65–80 dpi: макет, рисунки и таблицы различимы, мелкий текст — нет. Crop рисунка
даёт более высокое разрешение.

### 2.2 Официальный GGUF m0

Репозиторий `jinaai/jina-reranker-m0-GGUF` @ `61490ce6a4799192781ab7beab4ee4661675488c`: arch `qwen2vl`, только LLM
(1 543 714 304 параметров), **без mmproj**. `[FACT]`

| Файл | Байт | sha256 |
|---|---:|---|
| **`jina-reranker-m0-Q6_K.gguf`** | 1 272 737 984 | `115f0be6a38939fc7a960d088588c2d28ad1db26da1a89a0e465c932d8ace6d5` |
| `jina-reranker-m0-Q8_0.gguf` | 1 646 571 200 | `8a785993eb0030c4d6e537013e9b41846abb6df90f7c747fbf5bc8e729fe7a03` |
| `jina-reranker-m0-Q5_K_M.gguf` | 1 125 048 512 | `7a2091585b6722374e6482377a225a83dbfecfb50d4aa2e812ef9f7820f3d770` |
| `jina-reranker-m0-Q4_K_M.gguf` (дефолт `-hf`; не использовать) | 986 046 656 | `30c680bcc5ff0b89d0b3470a1fa44cd510b99f972a9e1bda61075277ff50e3c0` |
| `mlp_weights.npz` | 3 613 968 | `b7bea0fbe19940fbd1a98ffb3287bc74aec064646b4f602d287605ead9e36448` |

Особенности, заявленные Jina `[FACT]`:

- в токенизаторе GGUF id 100 поменян местами с `<|box_end|>` (151649), поэтому промпт заканчивается строкой
  `<|box_end|>`;
- эмбеддинг последнего токена берётся через `--pooling last --embd-normalize -1`;
- score = `sigmoid(relu(x·W1 + b1)·W2 + b2 − logit_bias)`.

### 2.3 llama.cpp и изображения

- Mainline `llama-server`: «`/embedding` … also supports multimodal embeddings»; формат
  `{"prompt_string": "…<__media__>…", "multimodal_data": ["<base64>"]}`; `embd_normalize: -1`. `[FACT: README master]`
- mtmd поддерживает Qwen2-VL; `--image-min-tokens`/`--image-max-tokens` переопределяют лимиты по умолчанию 8/4096;
  warmup идёт на максимальном размере. `[FACT: clip.cpp]`
- В режиме `--embeddings` lm_head считается на всех токенах `[FACT: PR #28949, черновик только для qwen35]`. Логиты
  1024 × 151 936 × 4 B = 593.5 MiB `[DERIV]`. Если output-слой на CPU (`-ngl ≤ n_layer`), этот буфер тоже на CPU
  `[MEM]`.
- Для embedding-задач весь промпт должен помещаться в `n_ubatch` `[MEM]`, отсюда `-ub 1024`: 768 visual + ≈ 13
  служебных + запрос ≤ 224 + 1 ≈ 1006 токенов.
- Нужно сделать:
  1. mmproj из чекпойнта m0 (`convert_hf_to_gguf.py --mmproj`, f16 → q8_0);
  2. промпт `**Document**:\n<__media__>\n**Query**:\n{q}<|box_end|>`;
  3. проверить, что mtmd оборачивает картинку в `<|vision_start|>…<|vision_end|>` `[MEM]`;
  4. parity-тест против transformers.

### 2.4 Варианты serving m0 (пересмотрены по фактам EDGE)

| Вариант | Точность | VRAM m0 `[EST]` | Влезает рядом с v3.5 (`B_m0` ≈ 1.5–2.1 GiB) | Латентность на изображение `[EST]` | Вердикт |
|---|---|---:|---|---|---|
| A-full: всё на GPU, `-ngl 99`, без патча | Q6_K + mmproj Q8_0 | ≈ 2.84 GiB | нет | 3.5–5 с | отклонено |
| A-full-patch: всё на GPU + патч | то же | ≈ 2.26 GiB | нет (только если v3.5 почти не нагружен) | 3–5 с | отклонено |
| A-ngl28: output-слой на CPU | то же | ≈ 2.08 GiB | на грани (только при лимите текста ≈ 2K) | 3.7–5.3 с | условно |
| **A-split: ViT на GPU + `-ngl k` (k ≈ 13–24), output на CPU** | **то же (Q6_K сохранён)** | **= `B_m0` по построению** | **да** | **≈ 5–7 с при k ≈ 24; ≈ 7–10 с при k ≈ 13** | **рекомендуется** |
| A-vitCPU: ViT на CPU, LLM на GPU | то же | ≈ 1.27 GiB | да | 20–40 с | запасной (batch) |
| A″-Q5_K_M на GPU | Q5_K_M (отклонение) | ≈ 1.95–2.7 GiB | как A-ngl28 или хуже | — | не даёт выигрыша против A-split |
| B′: transformers + bnb NF4 | 4 bit (ниже цели) | 2.5–3.0 GiB | нет | — | отклонено |
| B: bnb LLM.int8 | 8 bit | 3.3–3.8 GiB | нет | — | отклонено |
| vLLM / ONNX / HQQ / torchao | — | ≥ fp16-вес или не проверено на sm_75 | нет | — | отклонено (аргументы — редакция 1) |

Как получены оценки латентности `[EST]` на GTX 1650 Mobile (≈ 3 TFLOPS FP32 паспортно, лимит 50 W, троттлинг):

- ViT ≈ 5.7 TFLOP на изображение (768 токенов) → на GPU ≈ 2–3 с;
- LLM ≈ 2.1 TFLOP: на GPU ≈ 0.5 с на все слои, на CPU (Ryzen 5 4600H, ≈ 0.2–0.3 TFLOPS эфф.) ≈ 7–10 с на все слои;
- lm_head на CPU ≈ 0.37 TFLOP → 1.2–1.8 с; патч D-F1 это убирает.

Вывод: ViT, самая тяжёлая часть, всегда на GPU; остаток бюджета отдаётся слоям LLM.

## 3. Бюджет VRAM

### 3.1 Факты EDGE — **ПОДТВЕРЖДЕНО**

| Величина | Значение | Источник |
|---|---:|---|
| `memory.total` | 4096 MiB | nvidia-smi |
| `memory.used` (idle, один процесс) | 1216 MiB | nvidia-smi |
| `memory.free` | 2500 MiB | nvidia-smi |
| Зарезервировано драйвером | **380 MiB** | DERIV: 4096 − 1216 − 2500 |
| **Полезная ёмкость для процессов** | **3716 MiB** | DERIV |
| v3.5, idle | 1212 MiB | `--query-compute-apps` |
| v3.5 после запроса на 185 токенов | 1250 MiB (+38) | проба B |
| Дисплей на dGPU | нет (`Disp.A Off`, других процессов нет) | nvidia-smi |
| Persistence / compute mode | On / Default | nvidia-smi |
| Драйвер / CUDA | 595.91.07 / 13.2; toolkit 13.2.2 (nvcc 13.2.86) | nvidia-smi, dpkg |
| Лимит мощности | 50 W; SW Power Capping накоплено ≈ 17 970 с | nvidia-smi -q |

Наблюдение: при весах fp16 ≈ 1138 MiB `[DERIV]` накладные расходы процесса PyTorch в idle всего ≈ 74 MiB. На CUDA
13.2 с lazy loading CUDA context дешевле, чем я закладывал в редакции 1 (150–300 MiB). Для процесса llama.cpp теперь
закладываю ≈ 100 MiB `[EST]`.

### 3.2 Пик v3.5 под нагрузкой (без изменения сервиса)

Прирост на токен за один прямой проход, fp16 `[DERIV/EST]`:

- 29 скрытых состояний ≈ 58 KiB — держатся до конца прохода;
- временные буферы MLP ≈ 18 KiB;
- q/k/v/o ≈ 10 KiB;
- norm/residual ≈ 5 KiB;
- **итого ≈ 90 KiB/токен**;
- плюс маска sliding-window слоёв: до N² байт (bool) — 16 MiB при 4K, 64 MiB при 8K;
- плюс фрагментация allocator 10–30%.

**Риск:** если SDPA на sm_75 с маской уйдёт в math-ветку, attention займёт 16 × N² × 2 B — 2 GiB уже при 8K. Это
только измерять.

| Токенов на запрос (всего) | Рост `[EST]` | Пик процесса v3.5, `P_text` `[EST]` |
|---:|---:|---:|
| 185 (проба B) | +38 MiB | 1250 MiB (**факт**) |
| 2 048 | ≈ +0.2 GiB | ≈ 1.4 GiB |
| 4 096 | ≈ +0.4 GiB | ≈ 1.6 GiB |
| 8 192 | ≈ +0.8 GiB | ≈ 2.0 GiB |
| 16 384 | ≈ +1.7 GiB | ≈ 2.9 GiB |

- Allocator PyTorch кэширует, а не освобождает: после самого большого запроса `P_text` остаётся занятой до рестарта
  контейнера `[MEM]`. Для бюджета это удобно, потому что пик не «гуляет». Но **проба большого размера необратимо**
  (до рестарта) съедает бюджет m0.
- Как ограничить пик без изменения сервиса:
  1. лимит токенов на запрос в gateway: суммарно ≤ 4096, считается тем же `tokenizer.json`;
  2. не больше одного запроса одновременно в работе к text-бэкенду: если сервер многопоточный, два параллельных прохода
     удвоили бы пик;
  3. отказаться от «разогревающих» проб больше лимита.
- Прямые клиенты на `0.0.0.0:18082` эти лимиты обходят (D-F7).
- Менять `PYTORCH_CUDA_ALLOC_CONF` (`expandable_segments`) или вызывать `empty_cache` — это изменение сервиса. Не
  сейчас; только по отдельному решению.

### 3.3 Компоненты m0 (Q6_K + mmproj Q8_0) `[EST]`

| Компонент | MiB | Основание |
|---|---:|---|
| CUDA context llama.cpp | ≈ 100 | EST по факту PyTorch-процесса |
| mmproj Q8_0 (ViT) | ≈ 677 | размер mmproj Qwen2-VL-2B, форма та же |
| Compute ViT, 3072 патча, FA on / off | ≈ 150 / ≈ 680 | EST / DERIV: KQ 16 × 3072² × 4 B = 576 MiB |
| Слой LLM Q6_K | ≈ 36.5 на слой (28 слоёв ≈ 1023) | DERIV: (1214 − 191 на tied output) / 28 |
| KV, `-c 2048` | ≈ 2 на слой на GPU | DERIV: 1 KiB/токен/слой |
| Compute LLM без логитов (output на CPU) | ≈ 80–120 | EST |
| Output-слой + логиты | 0 на GPU при `-ngl ≤ 28` | MEM |

`VRAM_m0(k) ≈ 100 + 677 + 150 + 100 + 38.5·k` MiB, где k — число слоёв LLM на GPU; при k = 28 ≈ 2.1 GiB.

### 3.4 Рабочие точки (все цифры `[EST]`, кроме 3716)

| Лимит текста на запрос | `P_text` | `B_m0 = 3716 − P_text − 128` | k (слоёв LLM на GPU) | Visual, с/изобр. |
|---|---:|---:|---:|---|
| 2K (≈ 12 × 160) | ≈ 1430 MiB | ≈ 2150 MiB | 28 (упор в максимум) | ≈ 3.7–5.3 |
| **4K (≈ 24 × 160) — рекомендуемая** | **≈ 1640 MiB** | **≈ 1950 MiB** | **≈ 24** | **≈ 5–7** |
| 8K (≈ 48 × 160) | ≈ 2050 MiB | ≈ 1540 MiB | ≈ 13 | ≈ 7–10 |

k считается как `⌊(B_m0 − 1027) / 38.5⌋` по формуле §3.3.

k фиксируется после измерения реального `P_text` на выбранном лимите (§7.6). Проверяется по стартовым строкам
llama-server: `offloaded k/29 layers`, `CUDA0 model buffer size`, `CUDA0 compute buffer size`,
`CUDA_Host compute buffer size`.

Если ViT-compute без FA окажется ≈ 680 MiB (FA не работает на TU117), то `--image-max-tokens` понижается до ≈ 384
(KQ ≈ 144 MiB) либо уменьшается k.

## 4. Конкурентность

### 4.1 Факты — **ПОДТВЕРЖДЕНО**

- Compute mode Default: несколько процессов на GPU разрешены.
- Persistence On.
- Контейнер получает GPU через device request (`count: 1`), а не монопольно, поэтому второй контейнер получит ту же
  GPU.
- Сервер v3.5 — `http.server` (backlog 5). Многопоточный он или нет — неизвестно (§8.2).

### 4.2 Модель исполнения — DRAFT

- Два процесса (контейнер v3.5 и контейнер m0), у каждого свой CUDA context. Драйвер делит GPU по времени `[MEM]`.
- Запросы к обоим выполняются с перекрытием во времени; параллельности на уровне SM нет, прикладной сериализации нет.
- CUDA MPS не использовать `[FACT: NVIDIA docs]`:
  - только Linux;
  - рекомендован `EXCLUSIVE_PROCESS`;
  - фатальная ошибка клиента затрагивает всех клиентов той же GPU;
  - это системная настройка.
- Ограничения в gateway действуют **на каждый бэкенд отдельно**:
  - text: не больше одного запроса одновременно в работе + очередь ≤ 8, иначе 503 `RERANK_BACKEND_BUSY`. Нужно для
    пика VRAM.
  - visual: ≤ 2 (по числу слотов `-np 2`) + очередь ≤ 4.
  - Общих lock/semaphore между путями нет; тест это проверяет (§7).

### 4.3 Параметры m0 llama-server (эскиз)

```text
llama-server -m /models/jina-reranker-m0-Q6_K.gguf --mmproj /models/mmproj-jina-reranker-m0-Q8_0.gguf \
  --embeddings --pooling last -ngl <k> --fit off -fa on -t 6 \
  -c 2048 -np 2 -b 1024 -ub 1024 --image-min-tokens 4 --image-max-tokens 768 \
  --cache-ram 0 --metrics --host 127.0.0.1 --port 18083
```

- `-ngl <k>` ≤ 28: output-слой и логиты остаются на CPU `[MEM]`. k берётся из §3.4.
- `--fit off`: по умолчанию `--fit on` подстраивает незаданные аргументы под свободную VRAM с запасом 1024 MiB
  `[FACT]`. Нам нужны явные размеры и явный отказ при старте.
- `-t 6`: шесть физических ядер Ryzen 5 4600H; часть слоёв считается на CPU.
- `--sleep-idle-seconds` не задавать: по умолчанию выключено, иначе модель выгружается `[FACT]`.
- Warmup оставить включённым: резервирование при старте.

### 4.4 Доказательство «no reload»

| Бэкенд | Что сравниваем до и после окна теста |
|---|---|
| v3.5 | `docker inspect -f '{{.State.StartedAt}} {{.RestartCount}} {{.State.Pid}}' careerops-reranker` (базовая линия: `StartedAt` 2026-09-10T10:58:55Z); непрерывность VRAM host-PID; отсутствие стартовых строк загрузки в `docker logs --since <t0>` |
| m0 | то же для контейнера m0; ноль строк `llama_model_load` / `load_tensors` / `clip_model_loader` за окно; `/props` без признака sleeping; `/metrics` растут монотонно |

## 5. API-обёртка и контракт — DRAFT

### 5.1 Размещение

`vkm-rerank-gateway` на EDGE — отдельный Compose-сервис, `network_mode: host`, порт 18084 в LAN.

- Бэкенды: v3.5 `127.0.0.1:18082` (существующий, без изменений), m0 `127.0.0.1:18083`.
- VKM API и MCP на CORE видят только gateway.
- Промпты m0, MLP-голова, счёт токенов v3.5 и пины живут рядом с моделями.
- Поток по §32: OpenSearch → стабильные ID → VKM API берёт canonical text или байты артефакта → gateway →
  ранжированные ID.

### 5.2 Эндпойнты

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/v1/rerank/text` | v3.5 через `POST 127.0.0.1:18082/v1/rerank` (схема уточняется по §8.2) |
| POST | `/v1/rerank/visual` | m0: `/embedding` multimodal + MLP; в кандидатах только изображения |
| GET | `/health` | liveness gateway + `/healthz` v3.5 + `/health` m0 |
| GET | `/status` | ModelInfo обоих бэкендов (для v3.5 — из `/readyz`), `StartedAt`/`RestartCount` контейнеров, VRAM (NVML, read-only), активные лимиты, `config_hash` |

### 5.3 Контракт (pydantic v2, эскиз; утверждается через decision note — D-F4)

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

class TextCandidate(_Strict):
    id: str = Field(min_length=1, max_length=256)       # стабильный ID объекта
    text: str = Field(min_length=1, max_length=4_000)   # canonical text; обрезка до 160 токенов → truncated=True

class TextRerankRequest(_Strict):
    query: str = Field(min_length=1, max_length=1_000)  # ≤ 128 токенов
    candidates: list[TextCandidate] = Field(min_length=1, max_length=24)
    top_n: int | None = Field(default=None, ge=1, le=24)
    request_id: str | None = None

class ImagePayload(_Strict):
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    b64: str                                            # ≤ 10 MiB после декодирования
    artifact_id: str | None = None                      # ID рендера/crop на CORE (провенанс)
    sha256: str | None = None                           # если задан — сверяется

class VisualCandidate(_Strict):
    id: str = Field(min_length=1, max_length=256)
    image: ImagePayload                                 # поля text нет: extra="forbid" → 422

class VisualRerankRequest(_Strict):
    query: str = Field(min_length=1, max_length=1_000)  # ≤ 224 токенов
    candidates: list[VisualCandidate] = Field(min_length=1, max_length=16)
    top_n: int | None = Field(default=None, ge=1, le=16)
    request_id: str | None = None

class ModelInfo(_Strict):
    model_id: str                 # из /readyz (text) или манифеста (visual)
    model_revision: str           # HF commit весов
    code_revision: str | None     # text: model_code_revision из /readyz
    tokenizer_revision: str | None
    artifact_file: str | None     # visual: "jina-reranker-m0-Q6_K.gguf"
    artifact_sha256: str | None
    quant: str                    # text: "fp16 (no quantization)"; visual: "Q6_K"
    aux_artifacts: list[dict]     # visual: mmproj (sha256, quant, derived_from), mlp_weights.npz
    placement: str | None         # visual: "vit=GPU; llm_layers_gpu=k/28; output=CPU"
    backend: str                  # "transformers-cuda (careerops-reranker)" | "llama.cpp llama-server"
    backend_version: str          # torch/transformers из /readyz | llama.cpp commit (+ patch sha256)
    config_hash: str
    score_semantics: str          # "cosine(projector(h_doc), projector(h_query))" | "sigmoid(mlp(h_last)-2.65)"
    license: str                  # "CC-BY-NC-4.0"
    deviation_note: str | None

class RankedCandidate(_Strict):
    id: str
    rank: int
    score: float                  # сравнимо только внутри одного model_id+revision+quant
    input_index: int
    n_tokens: int | None = None
    truncated: bool = False
    image_sha256: str | None = None
    image_size_px: tuple[int, int] | None = None

class RerankResponse(_Strict):
    kind: Literal["text", "visual"]
    request_id: str
    model: ModelInfo
    results: list[RankedCandidate]
    n_candidates: int
    latency_ms: dict[str, float]  # total, queue, preprocess, backend
    gateway_version: str
    created_at: str
    warnings: list[str] = []
```

Scores — сигнал для retrieval, а не evidence. В canonical Parquet они не пишутся. Если VKM API/MCP их отдаёт, то с
провенансом (`model_id`, `revision`, `quant`, `placement`, `config_hash`) и `AUTO_EXTRACTED_UNREVIEWED`.

### 5.4 Лимиты

| Лимит | Text | Visual |
|---|---|---|
| кандидатов | ≤ 24 | ≤ 16 |
| запрос | ≤ 128 токенов | ≤ 224 токенов |
| документ | ≤ 160 токенов (обрезка с флагом `truncated`) | — |
| **всего токенов в проход** | **≤ 4096 (иначе 413)**; параметр `TEXT_MAX_PROMPT_TOKENS` меняется только вместе с пересчётом `B_m0` | ≤ 1006 на кандидата |
| изображение | — | PNG/JPEG/WebP, ≤ 10 MiB, ≤ 50 MP до resize; тело ≤ 64 MiB |
| одновременно в работе | 1 (+ очередь 8) | 2 (+ очередь 4) |
| таймаут бэкенда | 60 с | 300 с |

Токенизатор для лимитов — `tokenizer.json` v3.5 (sha256 `4e95945a…`) в gateway; тот же, что у сервиса (§1.3).
Изображения нормализуются в gateway: EXIF → RGB → `smart_resize` в 3136…602112 px (кратно 28) → PNG → sha256.

### 5.5 Источник изображений

Inline base64 от VKM API (CORE): EDGE не видит `$VKM_DATA_ROOT`. Режим URL в v0 не вводится (связанность с CORE,
SSRF-поверхность).

### 5.6 Ошибки, логи, безопасность

- **Ошибки.** Коды без изменений против редакции 1: `RERANK_INPUT_INVALID` 422, `RERANK_IMAGE_DECODE_FAILED` 422,
  `RERANK_PAYLOAD_TOO_LARGE` 413, `RERANK_BACKEND_BUSY` 503, `RERANK_BACKEND_UNAVAILABLE` 503,
  `RERANK_BACKEND_TIMEOUT` 504, `RERANK_BACKEND_ERROR` 502. Поля по §44: code, stage, tool, message, retryable,
  source, page, log_reference = `request_id`. OOM бэкенда: 502 и отдельная строка лога с `error_code=BACKEND_OOM`.
- **Логи.** JSON-строка на запрос: `ts`, `service`, `request_id`, `run_id`/`job_id`, `kind`, `backend`,
  `n_candidates`, `candidate_ids` (первые 32 + sha256 списка), `n_tokens_total`, `duration_ms`, `backend_ms`,
  `status`, `error_code`, `model_revision`, `quant`. Тексты и байты изображений не логируются. Docker json-file
  10m × 3, как у существующего контейнера.
- **Безопасность.**
  - m0 слушает только `127.0.0.1`.
  - Gateway — LAN + заголовок `X-VKM-Rerank-Token` из host-local env-файла (`0600`); в репозитории только
    `.env.example`.
  - Существующий `0.0.0.0:18082` при неактивном ufw — D-F7.

### 5.7 Код в PUBLIC (зона F)

- `src/vkm_corpus/retrieval/`:
  - `contracts.py`, `gateway.py`, `images.py`, `tokens.py` (счёт токенов v3.5), `m0_head.py`;
  - `backends/{careerops_v35.py, llama_mm_embedding.py}`.
- `infra/edge/`:
  - `compose/{vkm-rerank-visual,vkm-rerank-gateway}.compose.yml`;
  - `docker/llama-server-cuda132-sm75.Dockerfile`;
  - `env/*.env.example`, `models/MODELS_MANIFEST.example.json`;
  - `acceptance/rerank_acceptance.py`.
- Тесты `tests/corpus/retrieval/`: контракты, `smart_resize`, счёт токенов, fake-бэкенды без GPU (в том числе
  отсутствие общего lock); live — `@pytest.mark.edge_live`, без окружения `NOT_RUN`.

## 6. Развёртывание и пиннинг

### 6.1 Факты окружения — **ПОДТВЕРЖДЕНО**

- ОС: Debian 13.6 (trixie), ядро 6.12.107; ноутбук HP Pavilion Gaming 15-ec1xxx, Ryzen 5 4600H (6C/12T), 30 GiB RAM.
- GPU: TU117M (GTX 1650 Mobile / Max-Q) + iGPU AMD Renoir.
- Docker 26.1.5, Compose 2.26.1, NVIDIA Container Toolkit 1.20.0 (`nvidia-ctk`). В Docker зарегистрирован runtime
  `nvidia`; по умолчанию используется `runc` + device requests.
- Драйвер 595.91.07; `cuda-toolkit-13-2` установлен (`/usr/local/cuda-13.2`, nvcc 13.2.86). Есть и пакеты Debian
  `libcublas12`/`libcudart12` 12.4.
- Хостовые инструменты по выводу B: python3 3.13.5, git 2.47.3, curl, jq. **cmake, gcc/g++, make в выводе B не
  обнаружены**; подтвердить командой из §8.2.
- Диск: `/` 443 GB, свободно ≈ 366 GB. Журналы journald — 293.7 MB.
- Свободные порты: 18083, 18084, 8000, 8765, 7474, 9200.

### 6.2 Способ — Docker Compose (изменено против редакции 1)

Причины:

- существующий text-reranker уже в Compose с NVIDIA CT;
- на хосте нет сборочного toolchain, а ставить системные пакеты — значит менять систему;
- так соблюдается единообразие на хосте.

Два новых Compose-проекта (или один — решает координатор): `vkm-rerank-visual` (m0 llama-server) и
`vkm-rerank-gateway`. Hardening — как у `careerops-reranker`: `read_only`, `cap_drop: [ALL]`, `no-new-privileges`,
tmpfs `/tmp`, healthcheck, `restart: unless-stopped`, json-file 10m × 3. Веса монтируются read-only из host-каталога
`$VKM_MODELS_DIR`; путь выбирается с агентом B, не в git.

### 6.3 Сборка образа m0

Многостадийный Dockerfile:

- `FROM nvidia/cuda:13.2.<patch>-devel-<os>@sha256:<digest> AS build`;
- `git checkout <PINNED_COMMIT>`;
- `cmake -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=75 -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON
  -DLLAMA_BUILD_TESTS=ON`;
- цели: `llama-server`, `test-backend-ops`, `llama-bench`;
- `FROM nvidia/cuda:13.2.<patch>-runtime-<os>@sha256:<digest>`.

Замечания:

- CPU-варианты ggml нужны потому, что часть слоёв считается на CPU. Образ, собранный на WORKSTATION (другой CPU),
  должен выбрать правильный вариант на Zen2 `[MEM]`. Либо сборка прямо на EDGE.
- Что CUDA 13.2 работает с sm_75 на этом драйвере, доказывает существующий PyTorch cu132 на этой же GPU `[FACT]`.
- Скачивание базовых образов (несколько GB) — Phase 4, по одобрению (D-F8).
- **Проверка на TU117 до включения:**
  - `test-backend-ops -b CUDA0 -o MUL_MAT`, `-o FLASH_ATTN_EXT`, `-o SOFT_MAX`;
  - `llama-bench`;
  - при ошибках сравнить `-DGGML_CUDA_FORCE_CUBLAS=ON` / `-DGGML_CUDA_FORCE_MMQ=ON` и `-fa off`.
- Receipt сборки: commit, digest базовых образов, флаги, sha256 бинарников, код выхода.

### 6.4 Пины моделей

| Роль | Репозиторий @ revision | Файл | sha256 |
|---|---|---|---|
| text (**существующий**, не меняется) | `jinaai/jina-reranker-v3.5` @ `e8a93f33f0b22108f8c2364f8484ce3422552fbc` | `model.safetensors` (1 193 708 152 B), `tokenizer.json` | blob `50c684b7…dfb6fe` (HF xet/LFS id; сверить `sha256sum` при Phase 4); `4e95945a…455803` |
| visual, LLM | `jinaai/jina-reranker-m0-GGUF` @ `61490ce6a4799192781ab7beab4ee4661675488c` | `jina-reranker-m0-Q6_K.gguf` | `115f0be6a38939fc7a960d088588c2d28ad1db26da1a89a0e465c932d8ace6d5` |
| visual, голова | тот же | `mlp_weights.npz` | `b7bea0fbe19940fbd1a98ffb3287bc74aec064646b4f602d287605ead9e36448` |
| visual, источник mmproj и эталона | `jinaai/jina-reranker-m0` @ `94bfe0aeb2d4dd7978362699cddd5893d4e0adc8` | `model.safetensors` (≈ 4.9 GB) | при скачивании |
| visual, mmproj (производный) | своя конвертация, pinned llama.cpp | `mmproj-jina-reranker-m0-{f16,Q8_0}.gguf` | в receipt |
| кандидат на переиспользование (только при равенстве `visual.*`) | `ggml-org/Qwen2-VL-2B-Instruct-GGUF` @ `bb307c036e8a1ed7b663bbd0c35b41c4c9294cfd` | `mmproj-Qwen2-VL-2B-Instruct-Q8_0.gguf` | `a0ad91f00a7a80dcf84d719a61b00ee2e07b71794f4ee2dfa81a254621a8c418` |

### 6.5 Производство mmproj (Phase 4, D-F3)

1. Скачать чекпойнт m0 на WORKSTATION.
2. Сделать локальную копию `config.json` с `Qwen2VLForConditionalGeneration`.
3. `convert_hf_to_gguf.py --mmproj` → f16, затем q8_0, на том же commit, что образ.
4. Посчитать sha256 и перенести на EDGE.
5. Записать receipt.

### 6.6 Healthchecks и логи

- **Healthchecks:**
  - m0 — `/health` (llama-server);
  - gateway — `/health`;
  - v3.5 — существующий `/readyz`, не трогаем.

  Автоматического перезапуска по внешнему таймеру нет: перезапуск — это reload, он должен быть объясним.
- **Логи:** Docker json-file 10m × 3. journald на хосте занимает 293.7 MB, его настройки не меняем.

### 6.7 Warmup

После старта m0 отправляется один запрос с двумя изображениями по 768 токенов, чтобы дорастить пулы ggml. Для v3.5
warmup не нужен: он уже работает. Его `P_text` — это high-water mark после самого большого разрешённого запроса (§7.6).

## 7. Acceptance-тест — DRAFT

### 7.1 Проверки (§31, §56)

| # | Проверка | PASS, если |
|---|---|---|
| 1 | оба загружены | v3.5 `/readyz` = ready, m0 `/health` = 200, gateway `/health` = 200; оба host-PID есть в `--query-compute-apps` |
| 2 | idle VRAM | 10 с выборки: `memory.used`, VRAM по PID; `free ≥ 128 MiB` |
| 3 | text | контракт; детерминизм (`\|Δ\| ≤ 1e-4`); sanity top-3 ≥ 4/5 русских синтетических запросов; лимит 4096 соблюдается (запрос больше → 413) |
| 4 | visual | контракт; sanity top-1 ≥ 4/5; «visual значит visual» (§7.3); в `/status` placement = `vit=GPU, k/28` |
| 5 | concurrent | text и visual перекрываются во времени, text завершается раньше конца visual; `pmon` показывает интервалы, где активны оба PID |
| 6 | burst | 12 смешанных (8 text + 4 visual) + 60 с нагрузки: все 200 или ожидаемые 503 BUSY при переполнении очереди; ни одного 5xx кроме BUSY |
| 7 | no OOM | в логах обоих контейнеров за окно нет `out of memory`/`CUDA error`; во всех выборках `memory.free ≥ 128` MiB, то есть `memory.used ≤ 3716 − 128 = 3588` MiB |
| 8 | no reload | §4.4: `StartedAt`/`RestartCount`/PID совпадают; ноль стартовых строк загрузки |
| 9 | latency | p50/p95/max по фазам + `backend_ms`; clocks, throttle reasons, power, temperature |

### 7.2 Предрегистрация

Пороги фиксируются коммитом до первого запуска. Латентность наблюдается, пороги по ней координатор задаёт после
первого измерения. Предварительные значения `[EST]`:

- text, 24 кандидата, idle: p95 ≤ 5 с;
- visual, 8 изображений при k ≈ 24: p95 ≤ 90 с.

### 7.3 «Visual значит visual»

Текст промпта у всех кандидатов одинаков (`**Document**:\n<image>\n**Query**:\n{q}`), поэтому различия scores могут
прийти только из пикселей.

- **A. Разброс.** Разрез против диаграммы на запрос о разрезе: разрез на первом месте, `max − min ≥ 0.05`.
- **B. Перестановка.** Те же изображения под переставленными ID: ранг следует за изображением.
- **C. Дубликат.** Одно изображение под двумя ID: `|Δ| ≤ 1e-3`.
- **D. Одинаковая подпись.** На обоих изображениях «Рис. 1», содержание разное.
- **E. Пустой лист** ниже соответствующего изображения.
- **F. Негативный контракт:** без `image` → 422; с полем `text` → 422; битый base64 → 422; больше 10 MiB → 413.
- **G. Информационно, без порога:** кириллица на изображении.

### 7.4 Синтетические фикстуры

Генерируются детерминированно (seed): карта-схема, таблица, график оседания, геологический разрез, текстовая страница,
пустой лист, плюс 24 синтетических русских документа. Реальных текстов источников нет.

### 7.5 Структура скрипта

`infra/edge/acceptance/rerank_acceptance.py` запускается на EDGE. Логика — как в редакции 1: `Receipt`, `GpuSampler`
раз в 200 мс, 9 проверок, receipt JSON в `$VKM_DATA_ROOT/logs/acceptance/rerank/<ts>/`, exit 0 только при PASS.

Отличие: `snapshot_units()` теперь читает `docker inspect` (`StartedAt`, `RestartCount`, `State.Pid`) для
`careerops-reranker` и контейнера m0, а не `systemctl show`.

### 7.6 Измерение `P_text` и parity (Phase 4, до acceptance)

- **`P_text`.** Серия запросов к существующему `/v1/rerank` через gateway с ростом размера: 1K → 2K → 3K → 4K токенов.
  **Выше лимита gateway не ходить: high-water mark не вернётся до рестарта сервиса.** После каждого шага снимается VRAM
  PID. Итоговое значение фиксирует k для m0 (§3.4). Receipt: размеры, VRAM, латентность.
- **Parity m0.** Эталон — transformers на WORKSTATION (bf16, `94bfe0ae…`), 6 изображений × 5 запросов. PASS:
  Spearman ρ ≥ 0.9 по каждому запросу и `max|Δ| ≤ 0.05` `[EST]`. При FAIL путь блокируется.
- **Parity раскладки.** Scores m0 при k = 28 и при выбранном k должны совпасть до `|Δ| ≤ 1e-3`: раскладка не должна
  менять результат.

## 8. Инспекция EDGE

### 8.1 Что закрыто сырьём агента B — **ПОДТВЕРЖДЕНО**

- Как запущен v3.5 и с какими параметрами.
- Revision и формат весов.
- Порт и health-пути.
- VRAM idle и после пробы.
- Версии драйвера и CUDA; persistence; дисплей.
- Docker, Compose, NVIDIA CT.
- Свободные порты и диск; ufw.
- Каталоги кэша.
- Commit исходников сервиса.

Не закрыто:

- схема запроса и ответа `/v1/rerank`;
- модель многопоточности сервера;
- собственные лимиты сервиса;
- `torch.compile`/Triton;
- наличие cmake/gcc;
- стартовые строки логов контейнера (маркер загрузки для проверки «no reload»).

### 8.2 Read-only команды для координатора (агент F их не выполняет)

`<CAREEROPS_SRC>` — checkout CareerOps на EDGE: путь есть в `edge_09_reranker.txt`, в разделе «source route
decorators». Выполнять на EDGE, например через `ssh edge '…'`.

```bash
# 1) Схема /v1/rerank, модель потоков, лимиты и ответ (что именно принимает и возвращает сервер)
grep -n -E "ThreadingHTTPServer|HTTPServer|ThreadingMixIn|serve_forever|Lock\(|RLock|Semaphore|threading|json\.loads|json\.dumps|\"(query|documents|top_n|return_documents|model|results|index|relevance_score|score|usage|total_tokens|document)\"|max_|limit|send_response|Content-Length" <CAREEROPS_SRC>/src/careerops_reranker/server.py

# 2) Как вызывается модель и что влияет на память/латентность
grep -n -E "def |rerank\(|compute_score|no_grad|inference_mode|empty_cache|torch\.compile|compile\(|attn_implementation|sdpa|flash|eager|dtype|float16|max_|truncat|block|batch|PYTORCH_CUDA_ALLOC_CONF|expandable|set_per_process_memory_fraction|Triton|triton" <CAREEROPS_SRC>/src/careerops_reranker/runtime.py <CAREEROPS_SRC>/src/careerops_reranker/config.py

# 3) Контракт запроса/ответа на стороне клиента CareerOps (имена полей, лимиты token_budget/top_k)
sed -n '1,200p' <CAREEROPS_SRC>/src/careerops_processing/contracts/reranking.py

# 4) Маркеры старта (для no-reload), рестарты, toolchain хоста
sudo -n docker inspect -f '{{.State.StartedAt}} {{.RestartCount}} {{.State.Pid}}' careerops-reranker; sudo -n docker logs careerops-reranker 2>&1 | grep -v -E '"GET /(readyz|healthz)' | head -40; for t in cmake gcc g++ make ninja; do printf '%s: ' "$t"; command -v "$t" || echo MISSING; done
```

Ожидаемый результат:

- точные имена полей запроса и ответа, включая `relevance_score`;
- однопоточный сервер или `ThreadingHTTPServer`: определяет, нужен ли лимит «1 in-flight»;
- есть ли в сервисе свои лимиты;
- используется ли `torch.compile` (всплески латентности на новых формах);
- стартовая строка загрузки для проверки «no reload»;
- toolchain хоста.

## 9. Риски и решения

### 9.1 Риски

| # | Риск | Митигация |
|---|---|---|
| R1 | Бюджет m0 зависит от `P_text`, а пробы необратимо поднимают high-water mark v3.5 до рестарта | лимит 4096 в gateway; измерения только до лимита; k выбирается после измерения |
| R2 | `0.0.0.0:18082` при неактивном ufw: прямые LAN-клиенты обходят лимиты gateway и могут раздуть VRAM v3.5 → OOM у m0 | D-F7 (после cleanup-plan): bind 127.0.0.1 или правило firewall |
| R3 | SDPA на sm_75 с маской SWA может уйти в math-ветку (O(N²) памяти) | мерить на 1K…4K; при скачке уменьшить лимит |
| R4 | TU117 без tensor cores при cc 7.5 (MMQ/FA ggml) | `test-backend-ops`, варианты сборки, `-fa off` + меньше visual tokens |
| R5 | Путь m0 + изображения в llama.cpp официально не поддержан (наш mmproj, обёртка токенов mtmd `[MEM]`) | parity-тест, блокирующий |
| R6 | Раскладка GPU/CPU: m0 не целиком в VRAM (отклонение от «resident on GPU») | D-F2′; Q6_K сохранён; раскладка отражается в `/status` и receipt |
| R7 | Латентность visual 5–10 с на изображение, троттлинг ноутбука по мощности (50 W) | ≤ 16 кандидатов; предотбор текстом; clocks/throttle в receipt |
| R8 | Если в `careerops-reranker` используется `torch.compile`, новые формы вызывают перекомпиляцию | выяснить (§8.2), при необходимости бакетировать длины в gateway |
| R9 | CC BY-NC 4.0 у обеих моделей | диплом некоммерческий (не юридическая консультация); лицензия в `/status` и манифесте |
| R10 | Русский официально не подтверждён ни для одной модели | smoke-тесты на русском; без заявлений о качестве |
| R11 | Существующий сервис — часть CareerOps, которую выводят из эксплуатации (§8 постановки) | зафиксировать в cleanup-plan агента B исключение: `careerops-reranker`, его кэш, compose и env **сохраняются** |

### 9.2 Решения для координатора (пересмотрены)

- **D-F1 (было: обязательный патч) → необязательно.** Раскладка `-ngl ≤ 28` убирает логиты с GPU. Патч — позже, как
  оптимизация на ≈ 1–2 с/изображение. Старт на чистом upstream.
- **D-F2 (резидентный v3.5-host) → снято:** на EDGE T3, сервис резидентен.
- **D-F2′ (новое).** Принять рабочую точку «текст ≤ 4096 токенов на запрос, 1 запрос одновременно в работе» и
  раскладку m0 «ViT на GPU + k слоёв LLM на GPU, остальное на CPU» как документированное отклонение от «m0 целиком в
  VRAM». Квант Q6_K сохраняется.
- **D-F3 (остаётся).** Скачать чекпойнт m0 (≈ 4.9 GB) на WORKSTATION для mmproj и эталона; на EDGE — Q6_K GGUF
  (1.27 GB) и `mlp_weights.npz`.
- **D-F4 (остаётся).** Контракт §5.3 — общий с агентом G, через decision note.
- **D-F5 (уточнено).** Пороги acceptance до первого запуска. Отдельно разрешить протокол измерения `P_text` (§7.6) с
  верхней границей = лимит gateway.
- **D-F6 (закрыто для Phase 0)** данными агента B. Для Phase 4 агенту F нужен разрешённый `ssh edge`, либо команды
  выполняет координатор.
- **D-F7 (новое).** Защитить `careerops-reranker:18082`: после cleanup-plan — `CAREEROPS_RERANKER_HOST=127.0.0.1`
  (пересоздание контейнера = один **плановый** reload до базовой линии acceptance) или правило firewall. До этого
  риск R2 принимается явно.
- **D-F8 (новое).** Docker Compose для m0 и gateway; сборка llama.cpp в образе CUDA 13.2 devel. Это скачивание
  базовых образов (несколько GB) вместо установки cmake/gcc на хост.

## Источники (проверено 28.09.2026)

- Сырьё агента B (локально, git-ignored): `<PUBLIC>/work/corpus_platform/phase0/agent_b/raw/edge_*.txt`,
  `misc_15_final_checks.txt`, `misc_16_pg_native_ufw.txt`.
- `jinaai/jina-reranker-v3.5` — карточка, `modeling.py` @ `e8a93f33…`: `https://huggingface.co/jinaai/jina-reranker-v3.5`.
- `jinaai/jina-reranker-v3.5-GGUF` — README, `rerank.py`, LFS sha256: `https://huggingface.co/jinaai/jina-reranker-v3.5-GGUF`.
- arXiv 2607.18152; llama.cpp PR #26286: `https://github.com/ggml-org/llama.cpp/pull/26286`.
- `jinaai/jina-reranker-m0` — карточка, `config.json`, `preprocessor_config.json`, `modeling.py`:
  `https://huggingface.co/jinaai/jina-reranker-m0`.
- `jinaai/jina-reranker-m0-GGUF` — README, файлы, GGUF-метаданные: `https://huggingface.co/jinaai/jina-reranker-m0-GGUF`.
- `https://github.com/jina-ai/jina-reranker-m0-gguf`.
- Блог Jina о m0: `https://jina.ai/news/jina-reranker-m0-multilingual-multimodal-document-reranker/`.
- README llama-server (master): `https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md`.
- llama.cpp PR #18665 (закрыт) и PR #28949 (черновик): `https://github.com/ggml-org/llama.cpp/pull/18665`,
  `https://github.com/ggml-org/llama.cpp/pull/28949`.
- llama.cpp через Context7 (`/ggml-org/llama.cpp`): `ggml-cuda/CMakeLists.txt`, `tools/mtmd/clip.cpp`,
  `clip-model.h`, `common/arg.cpp`.
- `ggml-org/Qwen2-VL-2B-Instruct-GGUF`: `https://huggingface.co/ggml-org/Qwen2-VL-2B-Instruct-GGUF`.
- bitsandbytes (Context7) — требования к CC.
- NVIDIA MPS: `https://docs.nvidia.com/deploy/mps/when-to-use-mps.html`.
