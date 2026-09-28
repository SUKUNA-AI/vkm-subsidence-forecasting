# VKM RETRIEVAL / EMBEDDINGS / LATE-INTERACTION LAB — постановка пользователя (28.09.2026)

Дополнительная задача к VKM Corpus Platform v0, выполняется ПАРАЛЛЕЛЬНО основной платформе; основной
OCR/extraction pipeline не ломать и не задерживать. Уточнение пользователя: RX580 на CORE — 8 GB VRAM; одновременная
резидентность dense + late-interaction моделей — нормальный целевой сценарий, а не edge case; сначала держать обе модели
постоянно загруженными и обслуживать конкурентные запросы, reload — только если измерения докажут необходимость.
(Секции по номерам исходной постановки; текст сжат без потери требований.)

## 0. Цель
Экспериментально выбрать retrieval-архитектуру научного корпуса ВКМ, сравнив парадигмы: 1) lexical BM25; 2) dense
embeddings; 3) learned sparse; 4) late interaction / ColBERT; 5) cross-encoder reranking; 6) отдельный
multimodal/visual path. Выбор — по benchmark НА НАШЕМ корпусе, а не по MTEB. После выбора production-конфигурации:
document embeddings считаются ОДИН раз, хранятся как versioned derived artifacts, проецируются в OpenSearch и
пересчитываются только при изменении content_hash или embedding signature.

## 1. Контекст
Agent C строит pipeline (native extraction, PDF/DjVu/EPUB, GLM-OCR client, raw OCR, provenance, canonical objects,
idempotency). GLM-OCR сервер на RTX 5070 Ti поднят; полный OCR сознательно не запущен (нельзя uncontrolled OCR в обход
canonical pipeline). Retrieval research — параллельно, не ждать полного OCR.

## 2. Новые субагенты
- **AGENT J — RETRIEVAL BENCHMARK ENGINEER**: benchmark design, qrels, dense models, learned sparse, late interaction,
  fusion, метрики, index/storage trade-offs, production model selection (не low-level RX580). Выходы:
  `docs/implementation_work/AGENT_J_RETRIEVAL_BENCHMARK_DESIGN.md`, `AGENT_J_RETRIEVAL_RAW_RESULTS.md`,
  `AGENT_J_RETRIEVAL_ANALYSIS.md`, `AGENT_J_MODEL_SELECTION.md`.
- **AGENT K — RX580 RETRIEVAL BACKEND ENGINEER**: CORE GPU AMD RX 580 **8 GB** (Polaris). Цель — постоянный
  retrieval-ускоритель: production query embeddings, late-interaction query embeddings, при возможности concurrent dense
  + late-interaction inference, background document embedding jobs, часть финального full corpus encoding. Право:
  собирать latest llama.cpp/ggml, Vulkan/RADV, патчить Vulkan backend, писать недостающие kernels, исследовать HIP и
  legacy/custom ROCm для gfx803, custom model conversion, architecture-specific backend, custom quant/export. CPU
  fallback — НЕ желаемая production-архитектура; Ryzen 7 5800X оставить под OpenSearch, Neo4j, API, MCP, фоновые
  сервисы. Выходы: `AGENT_K_RX580_BACKEND_RESEARCH.md`, `AGENT_K_RX580_MODEL_MATRIX.md`, `AGENT_K_RX580_IMPLEMENTATION.md`,
  `AGENT_K_RX580_PARITY_RESULTS.md`, `AGENT_K_RX580_CONCURRENCY_RESULTS.md`.

## 3. Роли GPU
- **RTX 5070 Ti 16 GB** — reference/quality/bulk: canonical reference inference, benchmark всех embedding и
  late-interaction моделей, visual embeddings, throughput tests, финальная bulk generation, parity reference для RX580.
  Во время full GLM-OCR приоритет у OCR; после OCR — максимально для embedding generation.
- **RX580 8 GB** — persistent retrieval GPU: production dense query encoder, production late-interaction encoder,
  concurrent inference, background embedding, дополнительный full-corpus worker, возможно две одновременно resident
  модели. НЕ предполагать «одна GPU = одна модель».

## 4. Обязательный residency experiment (Agent K, для финалистов)
MODE A dense only; MODE B late-interaction only; MODE C dense + late одновременно resident; MODE D dense + late +
concurrent requests (одновременное кодирование). Без заранее введённого global GPU mutex и load/unload. Для C/D:
total/per-model/idle/peak VRAM, p50/p95 latency, throughput, OOM, reload count, GPU utilization, system RAM, CPU
overhead. Если две модели нормально живут в 8 GB — это предпочтительная production-архитектура.

## 5. Qwen3-Embedding-4B — исключить.

## 6. Dense models — обязательно
1. `jinaai/jina-embeddings-v5-text-small` (или официальный retrieval-вариант; ~677M, 1024d, Matryoshka).
2. `jinaai/jina-embeddings-v5-text-nano` (или retrieval-вариант; ~239M, 768d) — ОБЯЗАТЕЛЕН: quality, RX580
   throughput, VRAM, Q8/Q6.
3. `ibm-granite/granite-embedding-311m-multilingual-r2`. 4. `ibm-granite/granite-embedding-97m-multilingual-r2`
   (ultra-light кандидат; не предполагать проигрыш из-за размера).
5. `perplexity-ai/pplx-embed-v1-0.6b`. 6. `perplexity-ai/pplx-embed-context-v1-0.6b` (contextual — отдельно).
7. `lightonai/mDenseOn`. 8. `Qwen/Qwen3-Embedding-0.6B`. 9. `BAAI/bge-m3` (dense, learned sparse, multi-vector).

## 7. Secondary — максимум 2–3 свежие open models, только с WHY_THIS_MODEL rationale (не 30 моделей).

## 8. Late interaction — обязательно
`perplexity-ai/pplx-embed-v1-late-0.6b`; `lightonai/mLateOn`; `jinaai/jina-colbert-v2`; `jina-colbert-v2-96`;
`jina-colbert-v2-64` (одна family, разные storage-quality конфигурации); `BAAI/bge-m3` multi-vector.

## 9. ColBERT: query → token vectors, document → token vectors, MaxSim. Может помочь для терминологии, формул, RU↔EN,
длинных пассажей, многоконцептных запросов; НЕ автоматический победитель — benchmark должен доказать пользу.

## 10. Candidate pipelines (минимум)
A BM25; B dense; C BM25+dense; D BM25+dense+sparse; E BM25+dense → fusion → late; F BM25+dense+sparse → fusion → late;
G BM25+dense → Jina reranker-v3.5; H BM25+dense → late → reranker-v3.5; I BM25+dense+sparse → late → reranker-v3.5.
Не оставлять ступень без измеримого выигрыша.

## 11. Visual track (отдельный benchmark)
Primary `Qwen/Qwen3-VL-Embedding-2B`; дополнительно при разумной стоимости `jinaai/jina-embeddings-v4` или иной
зрелый multimodal. Объекты: page renders, maps, mine plans, geological sections, InSAR maps, graphs, radargrams, tables,
diagrams. Pipeline: text query → visual query embedding → vector candidates → page/figure images → Jina reranker-m0.
Не смешивать с text benchmark.

## 12. Benchmark до full OCR: после canary Agent C (15-source canary + native-text sources + canonical objects) —
`RETRIEVAL_BENCHMARK_V0`, желательно несколько тысяч canonical objects.

## 13. Text benchmark: 120–200 запросов; категории: exact entities; lexical; Russian semantic; Russian technical;
RU→EN; EN→RU; terminology mismatch; salt mechanics; creep/rheology; damage; geodesy; mine surveying; hydrogeology;
InSAR; inverse problems; mathematics; bibliography; VKM-specific; SKRU-1-specific; formula/method description;
long-context scientific queries.

## 14. Visual benchmark: 30–60 запросов (напр. карта шахтного поля со стволами; карта блока 201; геологический разрез;
график оседания; InSAR карта; схема камер и целиков; таблица механических свойств; радарограмма); qrels — к Page/Figure ID.

## 15. Qrels: graded 3 central / 2 strongly relevant / 1 peripheral / 0 irrelevant. LLM-generated qrels — не truth
автоматически. Источники: source metadata, reviewed evidence, existing source mappings, explicit page inspection,
manual/agent verification. Спорные — NEEDS_REVIEW.

## 16. Hard negatives обязательны (похожая статья о соли, но другой объект; тот же автор, другая работа; то же слово,
другой процесс; общий учебник вместо site-specific evidence; похожий график с другой переменной).

## 17. Единицы эмбеддинга — canonical objects, не произвольные 512-токенные куски: Block / semantic paragraph group;
Figure = caption + nearby explanatory text; Table = caption + normalized table + context; Formula = LaTeX/text +
surrounding explanation; Page — optional page-level vector.

## 18. Contextual experiment (pplx context): A object text only; B + document title + section title; C B + neighbor
context; D B + structured source metadata. Не тащить весь документ в каждый chunk.

## 19. Matryoshka: FULL/MID/SMALL (Jina small 1024/512/256; nano 768/384–512/256; Granite 311 768/512/384–256; Granite 97
384/256; Qwen 0.6 1024/512/256; PPLX native + разумные сжатия). При практически равном качестве — меньшая размерность.

## 20. Weight quantization: RTX — reference precision; RX580 — Q8, Q6, Q5 где применимо; не начинать с Q4.
## 21. Vector precision отдельно: FP32 / FP16 / INT8 / binary — после reference quality.

## 22. Метрики: Recall@10/20/50/100; MRR@10; nDCG@10/20; P@5/10; slices: Russian, RU→EN, EN→RU, VKM, SKRU1, physics, math,
bibliography, long context.
## 23. Engineering: parameters, size, VRAM, RAM, startup; RTX docs/s, queries/s, batch throughput; RX580 query p50/p95,
batch throughput, VRAM, GPU util, CPU, backend, quant; index dimension/precision/size; ColBERT token dim,
vectors/document, storage multiplier, MaxSim latency.
## 24. Fusion: RRF и score-normalized weighted fusion; веса не подбирать на held-out test (train/dev/test).

## 25. RX580 — приоритет исследования: 1 Vulkan/RADV; 2 llama.cpp/ggml Vulkan; 3 patch missing model/operator support;
4 custom Vulkan kernels; 5 HIP; 6 legacy/custom ROCm gfx803; 7 иной поддерживаемый Polaris-backend. «ROCm officially
unsupported» — не ответ.
## 26. RX580 model matrix: Model / Backend / Precision / Load / VRAM / Embed works? / Parity / Latency / Concurrent-capable
/ Notes — для всех serious finalists.
## 27. Residency matrix (пары): напр. Jina nano + mLateOn; Granite 311 + Jina-ColBERT; PPLX dense + PPLX late; Qwen0.6 +
Jina-ColBERT; BGE-M3 unified. Колонки: dense, late, quant dense/late, idle/peak VRAM, concurrent PASS/FAIL, p95 dense,
p95 late, throughput.
## 28. Shared family/backbone: проверить reuse weights/tokenizer/runtime/intermediate (PPLX dense+late, BGE-M3 multi-mode);
сложный shared-memory backend — только если выгода велика.
## 29. Concurrent inference: две resident модели считают одновременно; без заранее введённого semaphore/mutex/single
worker; если железо/бэкенд сериализует — зафиксировать как факт.
## 30. RX580 parity против RTX reference: cosine agreement, top-10/top-50 overlap, Spearman, Kendall, Recall delta, nDCG
delta; bit-identical не требуется.
## 31. RX580 участвует в финальном corpus encoding только после parity gate на representative canonical subset →
FINAL_CORPUS_ENCODER_APPROVED.

## 32–33. Полный OCR — только через pipeline Agent C. Пока RTX занят OCR: RX580 — backend development, model tests,
query/dense/late benchmark, small document batches; RTX-benchmark — в свободные окна, controlled batch, без разрушения
OCR throughput.
## 34–36. После FULL OCR DONE + canonical objects frozen + text hashes frozen + winner selected: RTX + RX580 одновременно
для final embedding generation; не считать объект дважды; задания через operational job queue (PostgreSQL; PENDING,
CLAIMED_RTX5070, CLAIMED_RX580, DONE, FAILED; atomic claim; без Kafka/Redis/Celery); performance-aware scheduler
(минимизировать wall-clock).

## 37. Embedding signature: object_id, content_hash, model_id, model_revision, embedding mode, dimension, pooling,
normalization, document instruction, precision, model quantization, pipeline version, config hash.
## 38. Query config signature: model, revision, query instruction, dimension, normalization, tokenizer, max length,
late-interaction config.
## 39. Storage: OpenSearch НЕ единственный store; canonical derived artifacts
`VKM_DATA_ROOT/derived/embeddings/{dense,sparse,multivector,visual}/<model>/<revision>/<config-hash>/part-*.parquet`.
## 40. Dense artifact: object_id, source_id, page_id, object_type, text_hash, model_id, model_revision, dimension,
precision, quant, config_hash, vector, worker, backend, created_at. 41. Sparse: object_id, text_hash, model, revision,
token/term ids, weights, config. 42. Multi-vector: object_id, text_hash, model, revision, token count, dimension,
multi-vector data, config (Arrow nested arrays или sharded binary/mmap; главное reproducibility). 43. Visual:
object_id, artifact_sha, model, revision, dimension, vector, processing config.
## 44. OpenSearch = projection; потеря индекса → rebuild из derived artifacts без GPU. 45. Experimental index на
model/config; после выбора production index очищается от лишних vector fields. 46. Re-embed: same content_hash + same
signature → NO INFERENCE; changed → re-embed only changed object.

## 47. Dense winner — decision matrix: quality, RU, cross-lingual, VKM/SKRU1, long-context, RX580 performance, VRAM,
dimension, index size, license, complexity (не average MTEB). 48. Late winner: nDCG/Recall gain, latency, storage
multiplier, RX580 VRAM, resident pair compatibility, complexity; negligible gain → не включать production ColBERT.
## 49. Если dense + late winners оба помещаются в RX580 8 GB, без постоянного reload, с acceptable concurrent latency и
значимым improvement — оставить ОБЕ resident 24/7 (предпочтительная архитектура). 50. Если BGE-M3 или иная
multifunction модель даёт dense+sparse+multi-vector почти того же качества при меньшей сложности — допустима unified.

## 51. EDGE (Jina reranker-v3.5 + m0) остаётся отдельным reranking host; CORE RX580 — candidate generation / late
interaction. 52. Возможный text flow: QUERY → BM25 + dense (RX580) + optional sparse → fusion → optional late (RX580) →
TOP 30 → EDGE reranker-v3.5 → TOP K. 53. Visual flow: QUERY → OCR/caption lexical + visual embeddings → fusion →
candidates → EDGE reranker-m0 → TOP K. 54. Explainability: debug trace BM25/dense/sparse/fusion/late/reranker ranks.
## 55. MCP: search_text, search_hybrid, search_visual; по возможности retrieval_trace; raw vector не основной API.
## 56. CORE embedding/retrieval service: /health, /model-info, /embed/query, /search/dense, /search/hybrid, /search/late,
/metrics; при двух моделях health показывает обе (loaded, resident, VRAM).
## 57. License audit: model_id, revision, license, restrictions, upstream URL, notes (особенно Jina).
## 58. Reproducibility: query set, qrels, snapshot hashes, model revisions, configs, raw results, metrics, decision report.
## 59. Data split: при подборе весов/порогов — DEV и HELD-OUT TEST.
## 60. После benchmark Agent H проверяет: честность qrels, leakage, unfair preprocessing, разные max lengths, разные query
instructions, inconsistent normalization, index artifacts, cherry-picking.
## 61. Итоговые таблицы. DENSE: Model, Params, Dim, Recall@20, Recall@50, nDCG@10, Russian, RU→EN, VKM, RTX throughput,
RX580 backend, RX580 p50/p95, VRAM, Index size, License. LATE: Model, Token dim, Gain, Storage x, Latency, VRAM, Pair
residency, License.
## 62. RX580 final report: exact model, VRAM = 8 GB, driver, Mesa, RADV, Vulkan, gfx803, backend commit, patches, quant,
models tested, models resident together, concurrent tests, VRAM maps, latencies, quality parity.
## 63. После winner freeze: все canonical searchable objects → embeddings (RTX + RX580 parallel workers); losers для всего
корпуса не пересчитывать. 64. Перед OpenSearch import: expected = embedded object count; no duplicates; no missing IDs;
same model revision/dimension/normalization/signature; valid vectors; checksums. 65. Final OpenSearch build: BM25,
dense, hybrid, sparse/late если выбраны, reranking integration.

## 66–70. Acceptance. DENSE: все обязательные модели; реалистичные qrels; BM25 baseline; category breakdown; winner
обоснован. LATE: PPLX late, mLateOn, Jina-ColBERT варианты, BGE multi-vector протестированы; gain/storage/latency;
решение обосновано. RX580: реальный GPU inference; 8 GB распознаны; финалисты протестированы; parity; p50/p95;
resident service работает. TWO-MODEL RESIDENCY: PASS только после явного теста dense + late одновременно resident
(PASS/FAIL/NOT_SELECTED; при PASS — concurrent requests тоже). FULL CORPUS: production config frozen; весь канон
embedded; без лишнего inference; derived vector store вне OpenSearch; OpenSearch rebuildable без GPU; incremental
re-embed работает; обе GPU используются после OCR при RX580 parity approved.

## 71. Stop: после production retrieval stack остановиться (не World-0, не PhysicalWorld generation, не forecast,
не OGS, не theorem experiments). 72. Главная цель — измеренно лучшая для нашего корпуса retrieval-система: canonical
objects → versioned embedding artifacts → BM25/dense/optional sparse → fusion → optional late interaction → Jina
reranking → MCP/API; RTX 5070 Ti = reference + OCR + bulk; RX580 8 GB = постоянный retrieval accelerator, в идеале с
двумя одновременно resident моделями.
