# RETRIEVAL_BENCHMARK_V0 — результаты на canary-снимке

Агент J, 2026-09-28. Снимок `snap-20260928T073057Z-a2696925` (canary). Статус: **V0 выполнен на canary** для 133 из
189 запросов (107 текстовых + 26 визуальных); остальные 56 — NOT_RUN (их релевантные источники ещё не в снимке).
Сырые результаты — [`results_v0.json`](results_v0.json) (только ID запросов и числа); выбор пары для сервиса RX580 —
[`MODEL_SELECTION_V0.md`](MODEL_SELECTION_V0.md); дизайн и протокол —
[`AGENT_J_RETRIEVAL_BENCHMARK_DESIGN.md`](../../docs/implementation_work/AGENT_J_RETRIEVAL_BENCHMARK_DESIGN.md).

## 0. Главное

1. **Late interaction — главный источник качества.** mLateOn (полный MaxSim): nDCG@10 0,510 против 0,419 у BM25
   (+0,091 [+0,034; +0,151], p = 0,002; после поправки Холма по семейству из 35 систем — 0,050, у схемы E —
   0,027–0,039), R@50 0,809 против 0,681; на test-сплите +0,122 (p = 0,001). Риск «mLateOn обучен без русского» на V0 не подтвердился: срез `russian` (93 запроса) — 0,499 против
   0,471 у BM25 и 0,421–0,424 у лучших dense-моделей.
2. **Схема сервиса RX580 (RRF(BM25, dense) top-100 → mLateOn) качества не теряет:** 0,507 (granite) / 0,514 (Qwen3) /
   0,517 (jina-nano) — неотличимо от полного MaxSim (−0,003, p = 0,75). Без dense-канала (BM25 top-100 → mLateOn) —
   0,450: dense нужен как источник кандидатов (+0,057, p = 0,002; R@50 +0,091).
3. **Dense-модели сами по себе BM25 значимо не превосходят** (jina-v5-nano 0,439, Qwen3-0.6B 0,431), а
   **granite-311m-r2 ниже BM25** (0,359, p = 0,076) и значимо хуже Qwen3 и jina-nano (−0,072 и −0,080, p ≤ 0,001).
   Межъязыковые запросы (13): BM25 = 0; mLateOn 0,45–0,67; granite на ru→en — 0,16.
4. **Текстовый реранк jina-reranker-v3.5 (top-24) значимого выигрыша не дал:** поверх BM25 +0,017 (p = 0,32), поверх
   сильной первой стадии −0,004 (p = 0,80; E с granite) и −0,005 (p = 0,77; E с Qwen3). Кандидаты урезаются API до ≈ 160 токенов (59% кандидатов
   усечены), 7–28% вызовов с 24 кандидатами получают 413 (находка 8). Вызов — 26–30 с при параллельном визуальном реранке, ≈ 15 с без него.
5. **Визуальный трек (26 запросов):** лучшая первая стадия — RRF(BM25, Qwen3, late): 0,64 против 0,55 у BM25
   (p = 0,02–0,04 без поправки). Реранк **jina-reranker-m0** по 8 изображениям снизил nDCG@10 на 0,064 (p = 0,17, n.s.)
   при 88–142 с на вызов — в текущей постановке не оправдан.
6. **BGE-M3 fp32 CPU (FlagEmbedding)** пару не заменяет: dense — 0,424 (≈ BM25; наравне с Qwen3 и jina-nano,
   −0,007 / −0,015, n.s.), sparse — 0,355 (хуже BM25: −0,064, p = 0,025), multi-vector (полный MaxSim) — 0,489 (+0,070
   к BM25, p = 0,020; к mLateOn −0,022, n.s.), unified 0,4 / 0,2 / 0,4 — 0,472 (хуже mLateOn: −0,039, p = 0,032). Как
   dense-канал схемы сервиса — 0,515 (= Qwen3), в RRF(BM25, BGE-M3, mLateOn) — 0,519, лучшая точка V0 (= тот же RRF с
   Qwen3, +0,003). Multi-vector BGE-M3 в 8 раз тяжелее mLateOn (1024 против 128 измерений на токен: 4,0 против 0,5 ГБ
   fp16 на canary) и не лучше его.
7. **Пара для RX580:** late — **оставить mLateOn**; dense — **заменить granite-311m-r2 на Qwen3-Embedding-0.6B** (обе
   Apache-2.0), уверенность средняя: внутри схемы сервиса dense-модели на V0 неразличимы, выигрыш Qwen3 — в
   самостоятельном dense-поиске, трёхканальном RRF и межъязыковых запросах; BGE-M3 dense — равноценная альтернатива
   (MIT). Подробно — [MODEL_SELECTION_V0.md](MODEL_SELECTION_V0.md).
8. **Находка для F/G (контракт реранка):** API резервирует 256 токенов под обвязку listwise-промпта v3.5, а обвязка 24
   пассажей + запрос (он входит дважды) занимает 497–625 токенов → при 24 «длинных» кандидатах промпт 4 337–4 465 >
   4 096 → HTTP 413 (`RERANK_PAYLOAD_TOO_LARGE`). Гарантированно помещается n ≤ 7. Исправление: считать `per_candidate`
   от фактической обвязки (токенизатор v3.5 есть в шлюзе) или резервировать ≈ 256 + 11·n + 2·|query| токенов.

Ключевые системы, текстовый трек (107 запросов, уровень страниц, VERIFIED qrels):

| # | Система | nDCG@10 | R@10 | R@50 | MRR@10 | judged@10 | ΔnDCG@10 vs BM25 [95% CI], p | ΔR@50 vs BM25, p |
|---|---|---|---|---|---|---|---|---|
| 1 | F3 · RRF(BM25, BGE-M3 fp32, mLateOn) | 0.519 | 0.607 | 0.795 | 0.608 | 0.26 | +0.100 [+0.055; +0.147] p<0.001 ✓ | +0.114, p<0.001 |
| 2 | F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | 0.515 | 0.597 | 0.810 | 0.619 | 0.25 | +0.096 [+0.048; +0.146] p<0.001 ✓ | +0.129, p<0.001 |
| 3 | E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100 | 0.515 | 0.578 | 0.781 | 0.632 | 0.26 | +0.096 [+0.038; +0.155] p=0.001 ✓ | +0.100, p<0.001 |
| 4 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | 0.514 | 0.578 | 0.779 | 0.630 | 0.25 | +0.095 [+0.037; +0.154] p<0.001 ✓ | +0.099, p<0.001 |
| 5 | L · late mLateOn (full MaxSim) | 0.510 | 0.573 | 0.809 | 0.626 | 0.25 | +0.091 [+0.034; +0.151] p=0.002 ✓ | +0.129, p<0.001 |
| 6 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 0.509 | 0.582 | 0.779 | 0.627 | 0.25 | +0.090 [+0.035; +0.147] p=0.001 ✓ | +0.099, p<0.001 |
| 7 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | 0.507 | 0.571 | 0.774 | 0.627 | 0.25 | +0.088 [+0.033; +0.145] p=0.001 ✓ | +0.093, p<0.001 |
| 8 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 0.502 | 0.574 | 0.774 | 0.616 | 0.25 | +0.083 [+0.030; +0.139] p=0.003 ✓ | +0.093, p<0.001 |
| 9 | L · late BGE-M3 multi-vector fp32 (full MaxSim) | 0.489 | 0.565 | 0.784 | 0.588 | 0.24 | +0.070 [+0.013; +0.128] p=0.020 ✓ | +0.104, p=0.004 |
| 10 | C · RRF(BM25, mLateOn) | 0.487 | 0.569 | 0.814 | 0.579 | 0.24 | +0.068 [+0.035; +0.101] p<0.001 ✓ | +0.133, p<0.001 |
| 11 | E · RRF(BM25, BGE-M3 fp32) → BGE-M3 multi-vector fp32 re-score top-100 | 0.483 | 0.553 | 0.779 | 0.587 | 0.23 | +0.064 [+0.009; +0.122] p=0.029 ✓ | +0.098, p=0.002 |
| 12 | L · late jina-colbert-v2 (full MaxSim) | 0.483 | 0.533 | 0.763 | 0.615 | 0.23 | +0.064 [+0.006; +0.123] p=0.030 ✓ | +0.083, p=0.008 |
| 13 | U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | 0.472 | 0.531 | 0.796 | 0.600 | 0.23 | +0.053 [-0.004; +0.112] p=0.073 | +0.115, p<0.001 |
| 14 | C · RRF(BM25, BGE-M3 fp32) | 0.464 | 0.552 | 0.775 | 0.548 | 0.23 | +0.045 [+0.008; +0.082] p=0.017 ✓ | +0.095, p<0.001 |
| 15 | C · RRF(BM25, Qwen3-Emb-0.6B) | 0.455 | 0.554 | 0.787 | 0.544 | 0.23 | +0.036 [-0.003; +0.075] p=0.069 | +0.106, p<0.001 |
| 16 | B · dense jina-v5-nano | 0.439 | 0.489 | 0.755 | 0.554 | 0.22 | +0.020 [-0.048; +0.089] p=0.571 | +0.074, p=0.067 |
| 17 | A · BM25 → rerank v3.5 (top-24) | 0.436 | 0.489 | 0.681 | 0.535 | 0.22 | +0.017 [-0.017; +0.049] p=0.317 | +0.000, p=1.000 |
| 18 | B · dense Qwen3-Emb-0.6B | 0.431 | 0.493 | 0.741 | 0.551 | 0.21 | +0.012 [-0.055; +0.078] p=0.734 | +0.060, p=0.106 |
| 19 | B · dense BGE-M3 fp32 | 0.424 | 0.498 | 0.762 | 0.527 | 0.22 | +0.005 [-0.060; +0.070] p=0.872 | +0.081, p=0.020 |
| 20 | A · BM25 | 0.419 | 0.482 | 0.681 | 0.509 | 0.21 | — | — |
| 21 | B · dense granite-311m-r2 | 0.359 | 0.418 | 0.632 | 0.459 | 0.18 | -0.060 [-0.126; +0.006] p=0.076 | -0.048, p=0.254 |
| 22 | S · sparse BGE-M3 fp32 (lexical weights) | 0.355 | 0.388 | 0.625 | 0.474 | 0.17 | -0.064 [-0.119; -0.011] p=0.025 | -0.056, p=0.066 |

## 1. Что измерено и на чём

- Снимок: canary `snap-20260928T073057Z-a2696925` (manifest `699721fc…`, DuckDB v1.5.5, pipeline 0.1.0) —
  15 источников, 2 251 страница; объекты: 39 334 блока, 2 173 рисунка, 612 таблиц, 6 526 формул, 0 библиографических
  записей.
- Единицы поиска — правило `vkm-units-v1`: 13 172 единицы (BLOCK_GROUP 4 783, FIGURE 1 304, FORMULA 6 474, TABLE 611;
  средняя длина 852 / 219 / 204 / 789 символов) на 2 231 странице; флаги: CONTEXT_FALLBACK 394, NO_CAPTION 138,
  SPLIT_PART 131, TABLE_TRUNCATED 154. PAGE-единицы (2 115) в V0 кандидатами первой стадии не служат.
- Оценка — на уровне страниц: ранжированные единицы → страницы (первое вхождение). Qrels — только VERIFIED и только
  страницы canary-канона; релевантно = grade ≥ 2; gain nDCG = grade (линейный).
- Покрытие: запрос входит в V0, если у него есть страница с grade ≥ 2 в canary-каноне: **107 текстовых + 26 визуальных
  = 133**. Остальные 56 (38 текстовых, 18 визуальных) — NOT_RUN: их релевантные источники ещё не в опубликованном снимке.
  Сплиты (стратифицированные, из бенчмарка): среди покрытых текстовых — 66 test; выбор систем по test не делался
  (параметры не настраивались, RRF k = 60 по умолчанию).
- Значимость: парный sign-flip randomization test (10 000 перестановок, seed 20260928) и 95% bootstrap-CI разности;
  «✓» — правило `worth_it` (Δ ≥ 0.02 и нижняя граница CI > 0). p в таблицах не скорректированы; поправка Холма по
  семейству «первая стадия vs BM25» — в § 3.
- judged@10 ≈ 0.2–0.25: большая часть верхних страниц не размечена (qrels собраны из evidence, объектного индекса и
  осмотра страниц, а не из пулов систем). Неразмеченное считается нерелевантным → абсолютные значения занижены у всех
  систем; сравнения парные. Пул-разметка top-10 — NOT_RUN (не помещается в окно V0).

## 2. Системы

| Код | Этап | Реализация в V0 |
|---|---|---|
| A | BM25 | локальный `LocalBM25` (анализатор = `vkm_text`: ё→е, стоп-слова ru/en, защищённые формы минералов, Snowball-ru + Porter), k1 = 1.2, b = 0.75, глубина 100 |
| B | dense | точный косинус по всем 13 172 единицам; векторы документов — RX580 (llama.cpp Vulkan, Q8_0) через harness агента K (`vkm_corpus.embeddings.cli encode`, derived Parquet), векторы запросов — `/embed/query` того же сервиса |
| L | late | полный MaxSim по всем токенам всех единиц (CPU, 8 потоков, fp32) — потолок качества late без PLAID |
| S | sparse | BGE-M3 lexical weights: скалярное произведение весов запроса и единицы по всем 13 172 единицам |
| U | M3-unified | 0,4·dense + 0,2·sparse + 0,4·ColBERT (BGE-M3, веса карточки модели) по всем единицам |
| C | RRF(BM25, X) | k = 60, по top-100 каждого списка |
| E | … → late re-score | top-100 первой стадии переупорядочены MaxSim late-модели; «E · RRF(BM25, dense) → late» — схема сервиса RX580 (гибрид → late) |
| F3 | RRF(BM25, dense, late) | k = 60, top-100 (late — полный MaxSim) |
| R | … → текстовый реранк | top-24 единиц (дедуп по канон. ID) → VKM API `POST /v1/rerank/text` → EDGE `jina-reranker-v3.5` (fp16, listwise); BLOCK_GROUP уходит passage-ом из своих блоков; бюджет ≈ 160 токенов на кандидата; 413 → повтор с top-12, затем top-7; отклонённые кандидаты — после переранжированных в исходном порядке; ≤ 2 запросов одновременно |
| V | … → визуальный реранк | top-8 изображений (FIGURE → кроп рисунка, иначе → изображение страницы) → `POST /v1/rerank/visual` → EDGE `jina-reranker-m0` (GGUF Q6_K + mmproj Q8_0); остальные страницы — в порядке первой стадии |

Модели эмбеддингов (RX580 — Q8_0 с вердиктом `FINAL_CORPUS_ENCODER_APPROVED` гейта паритета агента K; BGE-M3 — эталонная точность fp32 на CPU):

| Модель | Роль | Где считалась | Лицензия |
|---|---|---|---|
| `ibm-granite/granite-embedding-311m-multilingual-r2` | dense, 768 | прод-сервис RX580 (пара по умолчанию) | Apache-2.0 |
| `lightonai/mLateOn` | late, 128 | прод-сервис RX580 (пара по умолчанию) | Apache-2.0 |
| `jinaai/jina-embeddings-v5-text-nano-retrieval` | dense, 768 | вспомогательный экземпляр образа K (nice 10, удалён) | CC BY-NC 4.0 |
| `jinaai/jina-colbert-v2` | late, 128 | вспомогательный экземпляр образа K (nice 10, удалён) | CC BY-NC 4.0 |
| `Qwen/Qwen3-Embedding-0.6B` | dense, 1024 | вспомогательный экземпляр образа K (nice 10, удалён) | Apache-2.0 |
| `BAAI/bge-m3` @ `5617a9f6` | dense 1024 + sparse + multi-vector 1024 | «BGE-M3 fp32 CPU (FlagEmbedding)»: документы — агент K на CPU CORE (`BGEM3FlagModel`), запросы — энкодер лаборатории с той же семантикой (fp32 CPU) | MIT |
| `jinaai/jina-reranker-v3.5` | text rerank | EDGE, через VKM API | CC BY-NC 4.0 |
| `jinaai/jina-reranker-m0` | visual rerank | EDGE, через VKM API | CC BY-NC 4.0 |

## 3. Текстовый трек: все системы (107 запросов)

| # | Система | nDCG@10 | R@10 | R@50 | MRR@10 | judged@10 | ΔnDCG@10 vs BM25 [95% CI], p | ΔR@50 vs BM25, p |
|---|---|---|---|---|---|---|---|---|
| 1 | F3 · RRF(BM25, BGE-M3 fp32, mLateOn) | 0.519 | 0.607 | 0.795 | 0.608 | 0.26 | +0.100 [+0.055; +0.147] p<0.001 ✓ | +0.114, p<0.001 |
| 2 | E · RRF(BM25, jina-v5-nano) → mLateOn re-score top-100 | 0.517 | 0.583 | 0.796 | 0.630 | 0.25 | +0.098 [+0.040; +0.157] p<0.001 ✓ | +0.115, p<0.001 |
| 3 | F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | 0.515 | 0.597 | 0.810 | 0.619 | 0.25 | +0.096 [+0.048; +0.146] p<0.001 ✓ | +0.129, p<0.001 |
| 4 | E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100 | 0.515 | 0.578 | 0.781 | 0.632 | 0.26 | +0.096 [+0.038; +0.155] p=0.001 ✓ | +0.100, p<0.001 |
| 5 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | 0.514 | 0.578 | 0.779 | 0.630 | 0.25 | +0.095 [+0.037; +0.154] p<0.001 ✓ | +0.099, p<0.001 |
| 6 | L · late mLateOn (full MaxSim) | 0.510 | 0.573 | 0.809 | 0.626 | 0.25 | +0.091 [+0.034; +0.151] p=0.002 ✓ | +0.129, p<0.001 |
| 7 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 0.509 | 0.582 | 0.779 | 0.627 | 0.25 | +0.090 [+0.035; +0.147] p=0.001 ✓ | +0.099, p<0.001 |
| 8 | F3 · RRF(BM25, jina-v5-nano, mLateOn) | 0.507 | 0.596 | 0.803 | 0.586 | 0.25 | +0.088 [+0.041; +0.136] p<0.001 ✓ | +0.122, p<0.001 |
| 9 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | 0.507 | 0.571 | 0.774 | 0.627 | 0.25 | +0.088 [+0.033; +0.145] p=0.001 ✓ | +0.093, p<0.001 |
| 10 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 0.502 | 0.574 | 0.774 | 0.616 | 0.25 | +0.083 [+0.030; +0.139] p=0.003 ✓ | +0.093, p<0.001 |
| 11 | E · RRF(BM25, Qwen3-Emb-0.6B) → jina-colbert-v2 re-score top-100 | 0.501 | 0.568 | 0.789 | 0.631 | 0.24 | +0.082 [+0.026; +0.139] p=0.004 ✓ | +0.108, p<0.001 |
| 12 | F3 · RRF(BM25, jina-v5-nano, jina-colbert-v2) | 0.497 | 0.587 | 0.789 | 0.578 | 0.25 | +0.078 [+0.032; +0.125] p=0.001 ✓ | +0.108, p<0.001 |
| 13 | E · RRF(BM25, Qwen3-Emb-0.6B) → BGE-M3 multi-vector fp32 re-score top-100 | 0.497 | 0.569 | 0.776 | 0.596 | 0.24 | +0.078 [+0.021; +0.137] p=0.010 ✓ | +0.095, p<0.001 |
| 14 | F3 · RRF(BM25, Qwen3-Emb-0.6B, jina-colbert-v2) | 0.497 | 0.582 | 0.797 | 0.587 | 0.24 | +0.078 [+0.034; +0.122] p<0.001 ✓ | +0.116, p<0.001 |
| 15 | E · RRF(BM25, jina-v5-nano) → BGE-M3 multi-vector fp32 re-score top-100 | 0.492 | 0.563 | 0.789 | 0.593 | 0.24 | +0.073 [+0.018; +0.132] p=0.014 ✓ | +0.109, p<0.001 |
| 16 | E · RRF(BM25, jina-v5-nano) → jina-colbert-v2 re-score top-100 | 0.491 | 0.548 | 0.801 | 0.629 | 0.24 | +0.072 [+0.014; +0.130] p=0.013 ✓ | +0.121, p<0.001 |
| 17 | E · RRF(BM25, granite-311m-r2) → jina-colbert-v2 re-score top-100 | 0.491 | 0.564 | 0.776 | 0.616 | 0.24 | +0.072 [+0.019; +0.125] p=0.006 ✓ | +0.096, p<0.001 |
| 18 | L · late BGE-M3 multi-vector fp32 (full MaxSim) | 0.489 | 0.565 | 0.784 | 0.588 | 0.24 | +0.070 [+0.013; +0.128] p=0.020 ✓ | +0.104, p=0.004 |
| 19 | C · RRF(BM25, mLateOn) | 0.487 | 0.569 | 0.814 | 0.579 | 0.24 | +0.068 [+0.035; +0.101] p<0.001 ✓ | +0.133, p<0.001 |
| 20 | E · RRF(BM25, granite-311m-r2) → BGE-M3 multi-vector fp32 re-score top-100 | 0.484 | 0.555 | 0.761 | 0.587 | 0.24 | +0.065 [+0.010; +0.121] p=0.021 ✓ | +0.081, p=0.003 |
| 21 | E · RRF(BM25, BGE-M3 fp32) → BGE-M3 multi-vector fp32 re-score top-100 | 0.483 | 0.553 | 0.779 | 0.587 | 0.23 | +0.064 [+0.009; +0.122] p=0.029 ✓ | +0.098, p=0.002 |
| 22 | L · late jina-colbert-v2 (full MaxSim) | 0.483 | 0.533 | 0.763 | 0.615 | 0.23 | +0.064 [+0.006; +0.123] p=0.030 ✓ | +0.083, p=0.008 |
| 23 | F3 · RRF(BM25, granite-311m-r2, mLateOn) | 0.477 | 0.555 | 0.801 | 0.579 | 0.24 | +0.058 [+0.006; +0.108] p=0.031 ✓ | +0.120, p<0.001 |
| 24 | U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | 0.472 | 0.531 | 0.796 | 0.600 | 0.23 | +0.053 [-0.004; +0.112] p=0.073 | +0.115, p<0.001 |
| 25 | C · RRF(BM25, jina-colbert-v2) | 0.467 | 0.553 | 0.784 | 0.554 | 0.24 | +0.048 [+0.014; +0.081] p=0.004 ✓ | +0.103, p<0.001 |
| 26 | C · RRF(BM25, BGE-M3 fp32) | 0.464 | 0.552 | 0.775 | 0.548 | 0.23 | +0.045 [+0.008; +0.082] p=0.017 ✓ | +0.095, p<0.001 |
| 27 | F3 · RRF(BM25, granite-311m-r2, jina-colbert-v2) | 0.462 | 0.561 | 0.800 | 0.548 | 0.24 | +0.043 [-0.003; +0.088] p=0.069 | +0.119, p<0.001 |
| 28 | C · RRF(BM25, jina-v5-nano) | 0.457 | 0.537 | 0.793 | 0.534 | 0.23 | +0.038 [-0.003; +0.078] p=0.074 | +0.112, p<0.001 |
| 29 | C · RRF(BM25, Qwen3-Emb-0.6B) | 0.455 | 0.554 | 0.787 | 0.544 | 0.23 | +0.036 [-0.003; +0.075] p=0.069 | +0.106, p<0.001 |
| 30 | E · BM25 → mLateOn re-score top-100 | 0.450 | 0.510 | 0.683 | 0.548 | 0.23 | +0.031 [-0.012; +0.074] p=0.159 | +0.002, p=0.873 |
| 31 | E · BM25 → jina-colbert-v2 re-score top-100 | 0.446 | 0.503 | 0.685 | 0.551 | 0.22 | +0.028 [-0.019; +0.072] p=0.229 | +0.005, p=0.658 |
| 32 | B · dense jina-v5-nano | 0.439 | 0.489 | 0.755 | 0.554 | 0.22 | +0.020 [-0.048; +0.089] p=0.571 | +0.074, p=0.067 |
| 33 | A · BM25 → rerank v3.5 (top-24) | 0.436 | 0.489 | 0.681 | 0.535 | 0.22 | +0.017 [-0.017; +0.049] p=0.317 | +0.000, p=1.000 |
| 34 | B · dense Qwen3-Emb-0.6B | 0.431 | 0.493 | 0.741 | 0.551 | 0.21 | +0.012 [-0.055; +0.078] p=0.734 | +0.060, p=0.106 |
| 35 | B · dense BGE-M3 fp32 | 0.424 | 0.498 | 0.762 | 0.527 | 0.22 | +0.005 [-0.060; +0.070] p=0.872 | +0.081, p=0.020 |
| 36 | A · BM25 | 0.419 | 0.482 | 0.681 | 0.509 | 0.21 | — | — |
| 37 | C · RRF(BM25, granite-311m-r2) | 0.400 | 0.475 | 0.760 | 0.483 | 0.20 | -0.019 [-0.064; +0.023] p=0.394 | +0.079, p=0.001 |
| 38 | B · dense granite-311m-r2 | 0.359 | 0.418 | 0.632 | 0.459 | 0.18 | -0.060 [-0.126; +0.006] p=0.076 | -0.048, p=0.254 |
| 39 | S · sparse BGE-M3 fp32 (lexical weights) | 0.355 | 0.388 | 0.625 | 0.474 | 0.17 | -0.064 [-0.119; -0.011] p=0.025 | -0.056, p=0.066 |

Test-сплит (66 из 107; те же системы, без подбора параметров):

| Система | n | nDCG@10 (test) | ΔnDCG@10 vs BM25 (test) | R@50 (test) |
|---|---|---|---|---|
| A · BM25 | 66 | 0.388 | — | 0.670 |
| B · dense BGE-M3 fp32 | 66 | 0.431 | +0.043 [-0.037; +0.123] p=0.294 | 0.752 |
| S · sparse BGE-M3 fp32 (lexical weights) | 66 | 0.360 | -0.029 [-0.092; +0.036] p=0.378 | 0.625 |
| L · late BGE-M3 multi-vector fp32 (full MaxSim) | 66 | 0.489 | +0.101 [+0.028; +0.175] p=0.008 ✓ | 0.783 |
| U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | 66 | 0.472 | +0.083 [+0.013; +0.156] p=0.024 ✓ | 0.786 |
| C · RRF(BM25, BGE-M3 fp32) | 66 | 0.447 | +0.059 [+0.014; +0.105] p=0.011 ✓ | 0.775 |
| E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100 | 66 | 0.517 | +0.128 [+0.055; +0.203] p<0.001 ✓ | 0.785 |
| E · RRF(BM25, BGE-M3 fp32) → BGE-M3 multi-vector fp32 re-score top-100 | 66 | 0.487 | +0.099 [+0.026; +0.173] p=0.009 ✓ | 0.768 |
| F3 · RRF(BM25, BGE-M3 fp32, mLateOn) | 66 | 0.510 | +0.122 [+0.068; +0.178] p<0.001 ✓ | 0.795 |
| B · dense granite-311m-r2 | 66 | 0.371 | -0.017 [-0.101; +0.070] p=0.705 | 0.674 |
| B · dense jina-v5-nano | 66 | 0.471 | +0.082 [-0.002; +0.167] p=0.057 | 0.780 |
| B · dense Qwen3-Emb-0.6B | 66 | 0.456 | +0.068 [-0.013; +0.147] p=0.103 | 0.757 |
| L · late mLateOn (full MaxSim) | 66 | 0.510 | +0.122 [+0.048; +0.196] p=0.001 ✓ | 0.818 |
| L · late jina-colbert-v2 (full MaxSim) | 66 | 0.494 | +0.106 [+0.033; +0.174] p=0.004 ✓ | 0.780 |
| C · RRF(BM25, Qwen3-Emb-0.6B) | 66 | 0.458 | +0.070 [+0.028; +0.111] p=0.002 ✓ | 0.786 |
| C · RRF(BM25, mLateOn) | 66 | 0.470 | +0.082 [+0.044; +0.123] p<0.001 ✓ | 0.815 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | 66 | 0.512 | +0.124 [+0.051; +0.198] p=0.001 ✓ | 0.780 |
| E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | 66 | 0.512 | +0.124 [+0.051; +0.199] p=0.001 ✓ | 0.795 |
| F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | 66 | 0.519 | +0.131 [+0.075; +0.189] p<0.001 ✓ | 0.808 |
| A · BM25 → rerank v3.5 (top-24) | 66 | 0.409 | +0.021 [-0.020; +0.059] p=0.319 | 0.670 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 66 | 0.503 | +0.115 [+0.045; +0.183] p=0.001 ✓ | 0.780 |
| E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 66 | 0.502 | +0.114 [+0.045; +0.182] p=0.002 ✓ | 0.795 |

Поправка Холма (семейство: все системы первой стадии vs BM25, nDCG@10):

```
F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn): Holm p = 0.0035
F3 · RRF(BM25, BGE-M3 fp32, mLateOn): Holm p = 0.0035
C · RRF(BM25, mLateOn): Holm p = 0.0066
F3 · RRF(BM25, jina-v5-nano, mLateOn): Holm p = 0.0128
E · RRF(BM25, jina-v5-nano) → mLateOn re-score top-100: Holm p = 0.0248
E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100: Holm p = 0.0270
F3 · RRF(BM25, Qwen3-Emb-0.6B, jina-colbert-v2): Holm p = 0.0270
F3 · RRF(BM25, jina-v5-nano, jina-colbert-v2): Holm p = 0.0308
E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100: Holm p = 0.0324
E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100: Holm p = 0.0390
L · late mLateOn (full MaxSim): Holm p = 0.0500
E · RRF(BM25, Qwen3-Emb-0.6B) → jina-colbert-v2 re-score top-100: Holm p = 0.0984
C · RRF(BM25, jina-colbert-v2): Holm p = 0.1012
E · RRF(BM25, granite-311m-r2) → jina-colbert-v2 re-score top-100: Holm p = 0.1408
E · RRF(BM25, Qwen3-Emb-0.6B) → BGE-M3 multi-vector fp32 re-score top-100: Holm p = 0.2037
E · RRF(BM25, jina-v5-nano) → jina-colbert-v2 re-score top-100: Holm p = 0.2680
E · RRF(BM25, jina-v5-nano) → BGE-M3 multi-vector fp32 re-score top-100: Holm p = 0.2680
C · RRF(BM25, BGE-M3 fp32): Holm p = 0.2988
L · late BGE-M3 multi-vector fp32 (full MaxSim): Holm p = 0.3417
E · RRF(BM25, granite-311m-r2) → BGE-M3 multi-vector fp32 re-score top-100: Holm p = 0.3417
S · sparse BGE-M3 fp32 (lexical weights): Holm p = 0.3780
E · RRF(BM25, BGE-M3 fp32) → BGE-M3 multi-vector fp32 re-score top-100: Holm p = 0.4032
L · late jina-colbert-v2 (full MaxSim): Holm p = 0.4032
F3 · RRF(BM25, granite-311m-r2, mLateOn): Holm p = 0.4032
F3 · RRF(BM25, granite-311m-r2, jina-colbert-v2): Holm p = 0.7545
C · RRF(BM25, Qwen3-Emb-0.6B): Holm p = 0.7545
U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector): Holm p = 0.7545
C · RRF(BM25, jina-v5-nano): Holm p = 0.7545
B · dense granite-311m-r2: Holm p = 0.7545
E · BM25 → mLateOn re-score top-100: Holm p = 0.9533
E · BM25 → jina-colbert-v2 re-score top-100: Holm p = 1.0000
C · RRF(BM25, granite-311m-r2): Holm p = 1.0000
B · dense jina-v5-nano: Holm p = 1.0000
B · dense Qwen3-Emb-0.6B: Holm p = 1.0000
B · dense BGE-M3 fp32: Holm p = 1.0000
```

Парные сравнения моделей (главное для выбора пары):

| A | B | метрика | Δ(A−B) [95% CI] | p |
|---|---|---|---|---|
| L · late mLateOn (full MaxSim) | L · late jina-colbert-v2 (full MaxSim) | ndcg@10 | +0.027 [-0.016; +0.071] | 0.233 |
| L · late mLateOn (full MaxSim) | L · late jina-colbert-v2 (full MaxSim) | recall@50 | +0.046 [+0.006; +0.088] | 0.031 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | E · RRF(BM25, granite-311m-r2) → jina-colbert-v2 re-score top-100 | ndcg@10 | +0.016 [-0.021; +0.053] | 0.407 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | E · RRF(BM25, granite-311m-r2) → jina-colbert-v2 re-score top-100 | recall@50 | -0.002 [-0.021; +0.017] | 0.809 |
| B · dense Qwen3-Emb-0.6B | B · dense granite-311m-r2 | ndcg@10 | +0.072 [+0.034; +0.112] | <0.001 |
| B · dense Qwen3-Emb-0.6B | B · dense granite-311m-r2 | recall@50 | +0.109 [+0.058; +0.162] | <0.001 |
| B · dense jina-v5-nano | B · dense Qwen3-Emb-0.6B | ndcg@10 | +0.008 [-0.025; +0.040] | 0.636 |
| B · dense jina-v5-nano | B · dense Qwen3-Emb-0.6B | recall@50 | +0.014 [-0.025; +0.053] | 0.497 |
| C · RRF(BM25, Qwen3-Emb-0.6B) | C · RRF(BM25, granite-311m-r2) | ndcg@10 | +0.056 [+0.024; +0.088] | <0.001 |
| C · RRF(BM25, Qwen3-Emb-0.6B) | C · RRF(BM25, granite-311m-r2) | recall@50 | +0.027 [-0.001; +0.056] | 0.073 |
| E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | ndcg@10 | +0.007 [-0.007; +0.026] | 0.453 |
| E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | recall@50 | +0.006 [-0.020; +0.034] | 0.685 |
| E · RRF(BM25, jina-v5-nano) → mLateOn re-score top-100 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | ndcg@10 | +0.002 [-0.004; +0.010] | 0.520 |
| E · RRF(BM25, jina-v5-nano) → mLateOn re-score top-100 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | recall@50 | +0.016 [-0.006; +0.040] | 0.175 |
| F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | F3 · RRF(BM25, granite-311m-r2, mLateOn) | ndcg@10 | +0.039 [+0.010; +0.070] | 0.011 |
| F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | F3 · RRF(BM25, granite-311m-r2, mLateOn) | recall@50 | +0.009 [-0.006; +0.025] | 0.251 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | E · BM25 → mLateOn re-score top-100 | ndcg@10 | +0.057 [+0.022; +0.099] | 0.002 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | E · BM25 → mLateOn re-score top-100 | recall@50 | +0.091 [+0.043; +0.145] | <0.001 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | L · late mLateOn (full MaxSim) | ndcg@10 | -0.003 [-0.022; +0.011] | 0.746 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | L · late mLateOn (full MaxSim) | recall@50 | -0.036 [-0.075; +0.000] | 0.066 |
| A · BM25 → rerank v3.5 (top-24) | A · BM25 | ndcg@10 | +0.017 [-0.017; +0.049] | 0.317 |
| A · BM25 → rerank v3.5 (top-24) | A · BM25 | recall@50 | +0.000 [+0.000; +0.000] | 1.000 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | ndcg@10 | -0.004 [-0.039; +0.030] | 0.799 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | recall@50 | +0.000 [+0.000; +0.000] | 1.000 |
| E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 → rerank v3.5 (top-24) | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | ndcg@10 | -0.005 [-0.039; +0.029] | 0.768 |
| E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 → rerank v3.5 (top-24) | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | recall@50 | +0.000 [+0.000; +0.000] | 1.000 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | A · BM25 → rerank v3.5 (top-24) | ndcg@10 | +0.067 [+0.024; +0.114] | 0.004 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | A · BM25 → rerank v3.5 (top-24) | recall@50 | +0.093 [+0.044; +0.147] | <0.001 |

## 3a. BGE-M3 fp32 CPU (FlagEmbedding)

- Документы: агент K, официальный FlagEmbedding `BGEM3FlagModel` 1.4.2, fp32 на CPU CORE (Ryzen 7 5800X, 12 потоков,
  nice 10), `BAAI/bge-m3` @ `5617a9f6`; входы — docs.jsonl этого прогона (13 172 единицы, sha256 `1ce2b341…`),
  max_len 512 (1 966 465 токенов); 2 654 с, 4,96 ед/с, 741 токен/с; валидация §64 — 13 172 / 13 172 строк по всем трём
  выходам, 0 пропусков и битых векторов. Хранение: dense 1024 (fp32), sparse (lexical weights), multi-vector 1024 на
  токен в float16 (1 953 293 токена, 3,5 ГБ). Подписи: dense `fe00573d…`, sparse `d83f099e…`, multi-vector
  `c69b8760…`.
- Запросы: FlagEmbedding того же прогона (max_len 128, `queries.npz`); энкодер лаборатории (`BGEM3Encoder`, fp32 CPU)
  дал на всех 189 запросах те же векторы (dense cos = 1,000000, те же числа токенов, sparse L1 = 0).
- Оценка: dense — точный косинус; sparse — скалярное произведение lexical weights (как `compute_lexical_matching_score`);
  multi-vector — полный MaxSim по всем 1,95 млн токенов (CPU, 8 потоков, 210 мс на запрос в пакетном режиме), счёт
  ColBERT усреднён по токенам запроса, как в FlagEmbedding; unified = 0,4·dense + 0,2·sparse + 0,4·ColBERT (веса
  карточки модели) — точно по всем единицам, без предотбора.
- Паритет RX580 (факты агента K, в V0 не перепроверялись): dense Q8_0 — PASS; multi-vector Q8_0 — FAIL (token cos p1
  0,93–0,95, знаки препинания); F16 — PASS по всем выходам (≈ 3× латентность, 659 MiB). В V0 BGE-M3 оценён только в
  эталонной точности fp32 на CPU; сервисный вариант на RX580 — не оценивался.
- Итог для выбора: dense BGE-M3 равноценен Qwen3-0.6B и jina-v5-nano; sparse BGE-M3 хуже BM25 (лексический канал
  лучше оставить BM25 с анализатором `vkm_text`); multi-vector BGE-M3 хуже mLateOn и в 8 раз объёмнее; unified не
  лучше схемы сервиса (−0,035 к E с granite → mLateOn, p = 0,058).

Парные сравнения BGE-M3 (текстовый трек, 107 запросов; строки BGE-M3 в общей таблице § 3):

| A | B | метрика | Δ(A−B) [95% CI] | p |
|---|---|---|---|---|
| B · dense BGE-M3 fp32 | A · BM25 | ndcg@10 | +0.005 [-0.060; +0.070] | 0.872 |
| B · dense BGE-M3 fp32 | A · BM25 | recall@50 | +0.081 [+0.015; +0.149] | 0.020 |
| B · dense BGE-M3 fp32 | B · dense jina-v5-nano | ndcg@10 | -0.015 [-0.057; +0.026] | 0.491 |
| B · dense BGE-M3 fp32 | B · dense jina-v5-nano | recall@50 | +0.007 [-0.038; +0.052] | 0.780 |
| B · dense BGE-M3 fp32 | B · dense Qwen3-Emb-0.6B | ndcg@10 | -0.007 [-0.046; +0.032] | 0.743 |
| B · dense BGE-M3 fp32 | B · dense Qwen3-Emb-0.6B | recall@50 | +0.020 [-0.016; +0.060] | 0.302 |
| S · sparse BGE-M3 fp32 (lexical weights) | A · BM25 | ndcg@10 | -0.064 [-0.119; -0.011] | 0.025 |
| S · sparse BGE-M3 fp32 (lexical weights) | A · BM25 | recall@50 | -0.056 [-0.115; +0.003] | 0.066 |
| L · late BGE-M3 multi-vector fp32 (full MaxSim) | L · late mLateOn (full MaxSim) | ndcg@10 | -0.022 [-0.056; +0.013] | 0.224 |
| L · late BGE-M3 multi-vector fp32 (full MaxSim) | L · late mLateOn (full MaxSim) | recall@50 | -0.025 [-0.067; +0.015] | 0.235 |
| U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | A · BM25 | ndcg@10 | +0.053 [-0.004; +0.112] | 0.073 |
| U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | A · BM25 | recall@50 | +0.115 [+0.049; +0.184] | <0.001 |
| U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | L · late mLateOn (full MaxSim) | ndcg@10 | -0.039 [-0.074; -0.003] | 0.032 |
| U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | L · late mLateOn (full MaxSim) | recall@50 | -0.014 [-0.051; +0.023] | 0.482 |
| U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | ndcg@10 | -0.035 [-0.071; +0.002] | 0.058 |
| U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | recall@50 | +0.022 [-0.019; +0.066] | 0.311 |
| C · RRF(BM25, BGE-M3 fp32) | C · RRF(BM25, jina-v5-nano) | ndcg@10 | +0.007 [-0.025; +0.040] | 0.665 |
| C · RRF(BM25, BGE-M3 fp32) | C · RRF(BM25, jina-v5-nano) | recall@50 | -0.017 [-0.040; +0.005] | 0.133 |
| E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | ndcg@10 | +0.001 [-0.007; +0.008] | 0.877 |
| E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | recall@50 | +0.002 [-0.025; +0.027] | 0.900 |
| E · RRF(BM25, BGE-M3 fp32) → BGE-M3 multi-vector fp32 re-score top-100 | E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100 | ndcg@10 | -0.031 [-0.065; +0.002] | 0.065 |
| E · RRF(BM25, BGE-M3 fp32) → BGE-M3 multi-vector fp32 re-score top-100 | E · RRF(BM25, BGE-M3 fp32) → mLateOn re-score top-100 | recall@50 | -0.002 [-0.025; +0.020] | 0.845 |
| E · RRF(BM25, granite-311m-r2) → BGE-M3 multi-vector fp32 re-score top-100 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | ndcg@10 | -0.022 [-0.054; +0.010] | 0.170 |
| E · RRF(BM25, granite-311m-r2) → BGE-M3 multi-vector fp32 re-score top-100 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | recall@50 | -0.012 [-0.031; +0.004] | 0.166 |

## 4. Текстовый реранк (jina-reranker-v3.5 через VKM API)

| Реранк | вызовов OK | 413 → top-12 / top-7 | ошибок (fail-open) | латентность p50 / p95, с | кандидатов | отклонено | усечено | Δ nDCG@10 к своей 1-й стадии, p | Δ MRR@10, p |
|---|---|---|---|---|---|---|---|---|---|
| A · BM25 → rerank v3.5 (top-24) | 107 | 7 / 1 | 0 (0) | 26.0 / 32.0 | 2397 | 36 (NO_RERANK_TEXT) | 1413 | +0.017, p=0.317 | +0.025, p=0.402 |
| E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 107 | 26 / 4 | 0 (0) | 29.9 / 33.7 | 2188 | 19 (NO_RERANK_TEXT) | 1414 | -0.004, p=0.798 | -0.011, p=0.708 |
| E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 → rerank v3.5 (top-24) | 107 | 27 / 3 | 0 (0) | 15.0 / 16.7 | 2193 | 20 (NO_RERANK_TEXT) | 1445 | -0.005, p=0.768 | -0.003, p=0.923 |

- Кандидаты — top-24 единиц после дедупа по каноническому ID (первый объект единицы); BLOCK_GROUP уходит passage-ом из
  своих блоков (≤ 20 объектов). API сам урезает каждого кандидата до (4096 − 256) / n токенов (n = 24 → 160 ≈ 500
  символов при средней длине BLOCK_GROUP 852 символа).
- 413 → повтор с 12, затем с 7 кандидатами (колонка «413 → top-12 / top-7»); fail-open (порядок первой стадии) не
  понадобился. Рисунки без подписи отклоняются как `NO_RERANK_TEXT` и остаются после переранжированных кандидатов.
- VKM API на CORE перезапускался ≈ 08:58Z (редеплой); два прогона реранка оборвались и продолжены с места остановки —
  результаты по запросу независимы.
- Реранк не «пустой»: поверх BM25 он меняет nDCG@10 у 69 из 107 запросов (40 лучше, 29 хуже), поверх схемы E — у
  77–80 (36–37 лучше, 41–43 хуже); для сравнения mLateOn против BM25 — 59 лучше, 32 хуже.
- Возможные причины слабого эффекта (не проверены в V0): жёсткое усечение кандидатов; уменьшенная глубина у 20–28%
  запросов (fallback); неразмеченные страницы (judged@10 ≈ 0,22–0,25) — реранк может поднимать страницы, релевантные
  по существу, но отсутствующие в qrels. В V1: глубина 8–12 с бюджетом 320–480 токенов, passage = лучшие блоки единицы,
  пул-разметка top-10.

## 5. Срезы и категории (nDCG@10)

Срезы (запрос может входить в несколько):

| срез | n | A · BM25 | B · dense granite-311m-r2 | B · dense Qwen3-Emb-0.6B | L · late mLateOn (full MaxSim) | L · late jina-colbert-v2 (full MaxSim) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) |
|---|---|---|---|---|---|---|---|---|---|---|
| bibliography | 5 | 0.605 | 0.535 | 0.467 | 0.689 | 0.632 | 0.689 | 0.689 | 0.641 | 0.685 |
| en_to_ru | 7 | 0.000 | 0.303 | 0.344 | 0.453 | 0.406 | 0.471 | 0.463 | 0.333 | 0.441 |
| long_context | 5 | 0.589 | 0.481 | 0.513 | 0.511 | 0.435 | 0.511 | 0.511 | 0.615 | 0.607 |
| math | 12 | 0.571 | 0.430 | 0.551 | 0.619 | 0.580 | 0.626 | 0.639 | 0.641 | 0.547 |
| physics | 44 | 0.399 | 0.361 | 0.486 | 0.570 | 0.525 | 0.549 | 0.574 | 0.542 | 0.536 |
| ru_to_en | 6 | 0.000 | 0.163 | 0.587 | 0.671 | 0.490 | 0.500 | 0.664 | 0.539 | 0.479 |
| russian | 93 | 0.471 | 0.369 | 0.421 | 0.499 | 0.483 | 0.505 | 0.503 | 0.522 | 0.503 |
| skru1 | 9 | 0.424 | 0.303 | 0.336 | 0.416 | 0.318 | 0.427 | 0.427 | 0.413 | 0.383 |
| vkm | 65 | 0.404 | 0.351 | 0.378 | 0.429 | 0.420 | 0.437 | 0.431 | 0.457 | 0.460 |

Категории (n мал — только ориентир):

| категория | n | A · BM25 | B · dense granite-311m-r2 | B · dense Qwen3-Emb-0.6B | L · late mLateOn (full MaxSim) | L · late jina-colbert-v2 (full MaxSim) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | E · RRF(BM25, Qwen3-Emb-0.6B) → mLateOn re-score top-100 | F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 → rerank v3.5 (top-24) |
|---|---|---|---|---|---|---|---|---|---|---|
| bibliography | 4 | 0.506 | 0.418 | 0.334 | 0.611 | 0.540 | 0.611 | 0.611 | 0.551 | 0.607 |
| creep_rheology | 8 | 0.482 | 0.258 | 0.461 | 0.535 | 0.547 | 0.532 | 0.535 | 0.524 | 0.497 |
| damage | 4 | 0.730 | 0.548 | 0.613 | 0.627 | 0.757 | 0.659 | 0.660 | 0.759 | 0.687 |
| en_to_ru | 7 | 0.000 | 0.303 | 0.344 | 0.453 | 0.406 | 0.471 | 0.463 | 0.333 | 0.441 |
| exact_entity | 5 | 0.720 | 0.629 | 0.715 | 0.840 | 0.847 | 0.840 | 0.840 | 0.789 | 0.819 |
| formula_method | 8 | 0.608 | 0.615 | 0.690 | 0.649 | 0.641 | 0.667 | 0.665 | 0.685 | 0.555 |
| geodesy | 1 | 1.000 | 0.631 | 0.500 | 0.500 | 0.000 | 0.500 | 0.500 | 0.631 | 0.000 |
| hydrogeology | 2 | 0.288 | 0.000 | 0.000 | 0.091 | 0.000 | 0.091 | 0.091 | 0.098 | 0.161 |
| inverse_problems | 3 | 0.459 | 0.267 | 0.236 | 0.435 | 0.351 | 0.437 | 0.435 | 0.417 | 0.425 |
| lexical | 7 | 0.515 | 0.389 | 0.330 | 0.695 | 0.569 | 0.652 | 0.672 | 0.619 | 0.510 |
| long_context | 5 | 0.589 | 0.481 | 0.513 | 0.511 | 0.435 | 0.511 | 0.511 | 0.615 | 0.607 |
| mathematics | 5 | 0.507 | 0.456 | 0.582 | 0.552 | 0.639 | 0.560 | 0.573 | 0.608 | 0.558 |
| mine_surveying | 6 | 0.617 | 0.301 | 0.475 | 0.433 | 0.410 | 0.465 | 0.433 | 0.679 | 0.525 |
| ru_semantic | 6 | 0.040 | 0.119 | 0.217 | 0.125 | 0.101 | 0.137 | 0.117 | 0.188 | 0.194 |
| ru_technical | 6 | 0.565 | 0.410 | 0.408 | 0.604 | 0.530 | 0.604 | 0.604 | 0.580 | 0.600 |
| ru_to_en | 5 | 0.000 | 0.196 | 0.704 | 0.805 | 0.588 | 0.599 | 0.797 | 0.647 | 0.575 |
| salt_mechanics | 7 | 0.493 | 0.521 | 0.603 | 0.637 | 0.603 | 0.644 | 0.631 | 0.608 | 0.675 |
| skru1_specific | 6 | 0.420 | 0.325 | 0.350 | 0.419 | 0.372 | 0.434 | 0.435 | 0.440 | 0.452 |
| terminology_mismatch | 6 | 0.046 | 0.070 | 0.059 | 0.178 | 0.163 | 0.203 | 0.209 | 0.153 | 0.242 |
| vkm_specific | 6 | 0.325 | 0.250 | 0.224 | 0.267 | 0.486 | 0.268 | 0.267 | 0.282 | 0.387 |

## 6. Визуальный трек (26 запросов)

| # | Система | nDCG@10 | R@10 | R@50 | MRR@10 | judged@10 | ΔnDCG@10 vs BM25 [95% CI], p | ΔR@50 vs BM25, p |
|---|---|---|---|---|---|---|---|---|
| 1 | F3 · RRF(BM25, Qwen3-Emb-0.6B, jina-colbert-v2) | 0.646 | 0.753 | 0.904 | 0.692 | 0.13 | +0.097 [+0.027; +0.169] p=0.017 ✓ | +0.000, p=1.000 |
| 2 | F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | 0.642 | 0.760 | 0.904 | 0.689 | 0.13 | +0.093 [+0.015; +0.173] p=0.036 ✓ | +0.000, p=1.000 |
| 3 | F3 · RRF(BM25, BGE-M3 fp32, mLateOn) | 0.623 | 0.692 | 0.904 | 0.679 | 0.12 | +0.074 [-0.002; +0.151] p=0.079 | +0.000, p=1.000 |
| 4 | C · RRF(BM25, jina-colbert-v2) | 0.616 | 0.753 | 0.885 | 0.661 | 0.13 | +0.068 [+0.010; +0.130] p=0.040 ✓ | -0.019, p=1.000 |
| 5 | C · RRF(BM25, mLateOn) | 0.584 | 0.772 | 0.904 | 0.578 | 0.14 | +0.036 [-0.035; +0.106] p=0.335 | +0.000, p=1.000 |
| 6 | F3 · RRF(BM25, granite-311m-r2, mLateOn) | 0.584 | 0.705 | 0.942 | 0.622 | 0.13 | +0.035 [-0.070; +0.130] p=0.498 | +0.038, p=0.629 |
| 7 | U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector) | 0.566 | 0.699 | 0.856 | 0.581 | 0.12 | +0.018 [-0.111; +0.142] p=0.796 | -0.048, p=0.504 |
| 8 | B · dense BGE-M3 fp32 | 0.552 | 0.660 | 0.785 | 0.582 | 0.12 | +0.004 [-0.137; +0.143] p=0.960 | -0.119, p=0.100 |
| 9 | A · BM25 | 0.549 | 0.696 | 0.904 | 0.593 | 0.12 | — | — |
| 10 | B · dense Qwen3-Emb-0.6B | 0.540 | 0.686 | 0.833 | 0.572 | 0.12 | -0.009 [-0.128; +0.116] p=0.892 | -0.071, p=0.403 |
| 11 | L · late BGE-M3 multi-vector fp32 (full MaxSim) | 0.536 | 0.667 | 0.817 | 0.563 | 0.12 | -0.012 [-0.142; +0.111] p=0.859 | -0.087, p=0.253 |
| 12 | L · late jina-colbert-v2 (full MaxSim) | 0.535 | 0.769 | 0.904 | 0.518 | 0.14 | -0.013 [-0.116; +0.092] p=0.807 | +0.000, p=1.000 |
| 13 | C · RRF(BM25, mLateOn) → m0 (top-8 images) | 0.520 | 0.772 | 0.904 | 0.481 | 0.14 | -0.028 [-0.136; +0.078] p=0.617 | +0.000, p=1.000 |
| 14 | E · RRF(BM25, granite-311m-r2) → mLateOn re-score top-100 | 0.518 | 0.712 | 0.942 | 0.515 | 0.12 | -0.031 [-0.140; +0.082] p=0.595 | +0.038, p=0.501 |
| 15 | L · late mLateOn (full MaxSim) | 0.514 | 0.712 | 0.885 | 0.512 | 0.12 | -0.035 [-0.145; +0.078] p=0.544 | -0.019, p=1.000 |
| 16 | B · dense granite-311m-r2 | 0.460 | 0.612 | 0.788 | 0.465 | 0.11 | -0.088 [-0.217; +0.036] p=0.202 | -0.115, p=0.185 |

| Реранк | вызовов OK | 413 → top-12 / top-7 | ошибок (fail-open) | латентность p50 / p95, с | кандидатов | отклонено | усечено | Δ nDCG@10 к своей 1-й стадии, p | Δ MRR@10, p |
|---|---|---|---|---|---|---|---|---|---|
| C · RRF(BM25, mLateOn) → m0 (top-8 images) | 26 | 0 / 0 | 0 (0) | 131.3 / 140.8 | 208 | 0 (—) | 0 | -0.064, p=0.171 | -0.097, p=0.217 |

| A | B | метрика | Δ(A−B) [95% CI] | p |
|---|---|---|---|---|
| C · RRF(BM25, mLateOn) → m0 (top-8 images) | C · RRF(BM25, mLateOn) | ndcg@10 | -0.064 [-0.152; +0.019] | 0.171 |
| C · RRF(BM25, mLateOn) → m0 (top-8 images) | C · RRF(BM25, mLateOn) | recall@50 | +0.000 [+0.000; +0.000] | 1.000 |
| F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | F3 · RRF(BM25, granite-311m-r2, mLateOn) | ndcg@10 | +0.058 [-0.005; +0.134] | 0.146 |
| F3 · RRF(BM25, Qwen3-Emb-0.6B, mLateOn) | F3 · RRF(BM25, granite-311m-r2, mLateOn) | recall@50 | -0.038 [-0.096; +0.000] | 0.503 |
| C · RRF(BM25, jina-colbert-v2) | C · RRF(BM25, mLateOn) | ndcg@10 | +0.032 [-0.011; +0.079] | 0.172 |
| C · RRF(BM25, jina-colbert-v2) | C · RRF(BM25, mLateOn) | recall@50 | -0.019 [-0.058; +0.000] | 1.000 |
| L · late jina-colbert-v2 (full MaxSim) | L · late mLateOn (full MaxSim) | ndcg@10 | +0.022 [-0.054; +0.103] | 0.601 |
| L · late jina-colbert-v2 (full MaxSim) | L · late mLateOn (full MaxSim) | recall@50 | +0.019 [+0.000; +0.058] | 1.000 |

- Первая стадия визуального трека — текстовая (подписи и контекст рисунков, текст блоков); визуальные эмбеддинги —
  NOT_RUN. m0 видел только изображения (кроп рисунка или целая страница, размер по умолчанию API), без подписи.
- В 8 из 26 вызовов среди 8 кандидатов не было ни одного кропа рисунка (только страницы). 8 изображений — 88–142 с
  (≈ 11–18 с на изображение: шёл параллельно с текстовым реранком; у F без нагрузки — 7,7 с).
- V0 не подтверждает пользу m0 поверх текстовой первой стадии (MRR@10 −0,097, n.s.). В V1: кандидаты-кропы вместо
  страниц, больше визуальных запросов (сейчас 26), сравнение с late-моделью на подписях.

## 7. Инженерные показатели

RX580 (CORE, llama.cpp Vulkan, Q8_0). Кодирование документов шло одновременно 2–3 энкодерами на одной карте, поэтому
пропускная способность — нижняя граница для монопольного режима. Латентность запроса — отдельно, на простаивающей
карте (40 последовательных запросов, клиент в сети хоста).

| Модель | Роль, dim | Кодирование 13 172 единиц | Запрос p50 / p95, мс | VRAM, MiB | Токенов на единицу | Хранилище векторов (fp32 → fp16) |
|---|---|---|---|---|---|---|
| granite-311m-r2 | dense, 768 | 318 с, 41,4 ед/с | 25 / 49 | 188 | — | 40 МБ → 20 МБ |
| jina-v5-nano | dense, 768 | 330 с, 39,9 ед/с | 20 / 45 | 211 | — | 40 МБ → 20 МБ |
| Qwen3-Emb-0.6B | dense, 1024 | 1 615 с, 8,2 ед/с | 39 / 53 | 1 163 | — | 54 МБ → 27 МБ |
| mLateOn | late, 128 | 902 с, 14,6 ед/с | 26 / 47 | 188 | 147,6 (1 944 241 всего) | 995 МБ → 498 МБ |
| jina-colbert-v2 | late, 128 | 1 337 с, 9,9 ед/с | 40 / 94 | 369 | 111,3 (1 466 218 всего) | 751 МБ → 375 МБ |

VRAM — по `/health` сервисов (занято на карте всего ≈ 2,1 из 8 GiB при трёх экземплярах). Хранилище — чистые векторы;
derived Parquet агента K (fp32 + метаданные) занимает 58 МБ на dense-модель и 1 020 МБ для mLateOn.

BGE-M3 fp32 на CPU CORE (эталон, не сервисный вариант): 13 172 единицы за 2 654 с (4,96 ед/с, 741 токен/с, 12 потоков);
хранение — dense 54 МБ (fp32 → 27 МБ fp16), multi-vector 1 953 293 × 1024 → 4,0 ГБ fp16 (в 8 раз больше mLateOn),
sparse — 725 924 ненулевых весов (6,6 МБ Parquet); полный MaxSim — 210 мс на запрос (пакетно, CPU, 8 потоков).

CPU (WORKSTATION/WSL, nice 10, 8 потоков): BM25 — сборка 2,1 с, запрос 1,1 мс; точный dense-поиск по 13 172 × 768 —
1,1–1,5 мс на запрос; полный MaxSim — 175 мс (mLateOn, 1,94 млн токенов) и 243 мс (jina-colbert-v2, 1,47 млн токенов,
32 токена запроса) на запрос. Для полного корпуса (ожидаемо в 10–20 раз больше токенов) полный MaxSim на CPU
становится секундами на запрос → в сервисе нужна схема E (гибрид → late re-score top-100), которая на V0 не хуже
полного MaxSim (§ 3).

Реранк (EDGE, через VKM API): см. § 4 и § 6 — латентность вызова включает очередь сервиса (текстовый сервис
последовательный; визуальный реранк шёл параллельно с текстовым, это замедляло оба).

## 8. NOT_RUN и ограничения

- 56 запросов (38 текстовых, 18 визуальных) — NOT_RUN: их релевантные страницы в источниках, которых нет в canary.
  Снимки нативного прохода и финального OCR в окно V0 не попали (V1 — на финальном снимке, как решено координатором).
- CPU fp32 эталонные прогоны исходных HF-моделей по полному канону — NOT_RUN по решению координатора (паритет GGUF ↔
  эталон уже закрыт гейтом агента K на dev- и canary-пробах).
- RTX-прогоны (jina-v5-small, pplx-embed, BGE-M3 sparse/unified, Qwen3-VL-Embedding, jina-v5-omni) — NOT_RUN: RTX
  занят GLM-OCR; окно простоя не наступило.
- BM25 в OpenSearch (`vkm-exp-*`) — NOT_RUN: использован локальный BM25 с тем же анализатором; экспериментальных
  индексов не создавалось.
- BGE-M3 на RX580 (Q8_0 / F16) — NOT_RUN по качеству: в V0 только эталон fp32 на CPU (§ 3a); прочие learned sparse модели — NOT_RUN.
- Варианты рендера единиц B–D (контекст раздела/соседей), MRL-усечение и квантизация int8/binary, подбор весов
  слияния на train, глубина реранка, пул-разметка — NOT_RUN (окно V0).
- Визуальный трек: визуальные эмбеддинги первой стадии — NOT_RUN; m0 проверен только поверх одной текстовой первой
  стадии.

Ограничения: 107 текстовых запросов → 95% CI разности систем ±0,02–0,06 nDCG@10 (уже для близких систем, шире против
BM25); многие срезы и категории содержат 1–9 запросов; judged@10 низкий (см. § 1); canary — 15 источников, темы
представлены неравномерно. Поэтому V1 на финальном снимке обязателен до окончательных решений.

## 9. Воспроизведение

Скрипты прогона — [`scripts/`](scripts/): `prepare_canon.py` (канон canary → единицы `vkm-units-v1` → docs для
кодирования), `embed_queries.py` и `lat_probe.py` (внутри образа сервиса RX580 на CORE, сеть хоста: векторы запросов,
латентность), кодирование документов — harness агента K (`python -m vkm_corpus.embeddings.cli encode --config <svc>
--docs docs.jsonl --data-root <dir> --roles dense late`), `run_v0.py first | bgem3 | rerank <systems> |
visual <systems> | eval`, `tables_v0.py`, `build_report.py`; BGE-M3: `bgem3_queries.py` (запросы энкодером
лаборатории) и `bgem3_qparity.py` (сверка с `queries.npz` FlagEmbedding). Окружение: `J_V0` (рабочий каталог прогона), `VKM_API_TOKEN_FILE`
(read-only токен API), `VKM_API_URL` (или адрес CORE из `ssh -G core`, порт 8000), `J_BGEM3_ROOT` /
`J_BGEM3_QNPZ` (артефакты и запросы BGE-M3 агента K).

Сырые ранжирования (unit_id top-100 по каждому запросу и системе), ответы реранка, логи — вне git, в рабочем каталоге
лаборатории (`work/corpus_platform/impl/retrieval_lab/v0/`, git-ignored); derived-эмбеддинги — формата агента K
(`derived/embeddings/<kind>/<model>/<rev>/<config>/`) в каталоге данных WORKSTATION. Все числа этого файла
воспроизводятся из [`results_v0.json`](results_v0.json) (агрегаты, значимость, пер-запросные nDCG@10 / R@50 / MRR@10).
