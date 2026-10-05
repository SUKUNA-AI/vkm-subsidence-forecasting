# Передача: продолжение production/recovery/науки — 05.10.2026 (Claude)

Исполнительный проход по handoff `VKM_CLAUDE_HANDOFF_2026-10-05` (пакет проверен
своим `validate_delivery.py`: PASS, 13 файлов). Работа продолжена с фактического
локального checkpoint, без reset/clean/stash и без восстановления файлов из Git.

## 1. Исходная точка и сохранность

| Объект | Значение |
|---|---|
| Ветка | `gpt/production-data-2026-10-01` (draft PR #10, не слит) |
| HEAD на старте | `bd815893` (= remote) + 25 незакоммиченных путей Codex |
| Закрытый checkpoint dirty-состояния | локальный ref `refs/wip/claude-start-2026-10-05` = `84a57f03` (без `.mcp.json.example`; не публикуется) |
| `.mcp.json.example` | не изменялся, не индексировался |

Незакоммиченный first-LIVE checkpoint Codex закоммичен **ровно в протестированных
байтах**: 19/19 SHA-256 совпали с freeze из `first_live_protective_cpu_2026-10-05.json`;
повторный прогон снапшота: 175 passed (Linux, CPython 3.13.5) — `b70d406`, `2ee373d`.

## 2. Кто что делал

- Координатор (Claude): инвентаризация, read-only диагностика EDGE, код E0,
  контроллер v5, нативное исправление witness, сборки образов v5–v8, bounded SHADOW
  drills v6–v8, receipts, коммиты.
- Субагент B: L0 — фиксированный Engine-create adapter (`engine_create.py`) и тесты.
- Субагент C: H0 — импорт 60 исторических H-меток с lineage.
- Субагент D: read-only диагностика R3 WinError 5.
- Challenger E0/E1 (не автор): 3 раунда кода + 4 pre-drill проверки пакетов. Нашёл,
  в т.ч., MUST_FIX в генераторе receipt и **NO-GO** на пакет v5 (`config_sha256` owner
  включал эфемерный witness-токен — загруженный owner никогда не совпал бы с планом;
  исправлено в `5ee07e4`); GO на v6, v7, v8; проверил побайтно, что нативный overlay
  v8 = патч `74ae2b6`, применённый к базе, уже работавшей на EDGE.
- Challenger L0 (не автор): 2 раунда — MUST_FIX (утечка Env-секретов в control root),
  SHOULD_FIX F2–F6 и R1–R4 (в т.ч. проверки после парковки оригиналов) — все закрыты.

Находки субагентов перепроверялись координатором (повторные прогоны тестов, сверка
владельцев файлов R3 через ACL), а не объединялись механически.

## 3. Причины EDGE-отказов (раздельно)

**Повторная recovery `ValueError` (04.10).** Механизм воспроизведён на точном коде v4
(sha `6ece9bcd…`) read-only на EDGE: Docker строит `Mounts` в inspect из Go-map, и для
retained OLD (2 mounts) порядок обратный в 27/200 вызовов; `inspect_old()` сравнивал
упорядоченный список и падал в 29/200 (неперехваченные вызовы в `recover()`/`fallback()`).
Наиболее вероятная причина (receipts 04.10 сохранили только класс). Исправлено в
контроллере v5 и **подтверждено на runtime трижды** (v6, v7: повторная recovery —
`ALREADY_RESTORED_VERIFIED_NOOP`).

**Startup exit 2 (04.10, v6, v7).** Bounded-диагностика (`f1dfd68`) показала стадию:
listener child за ~150 мс, затем witness 503 до дедлайна 180 с при живом child. Drill v7
с закрытой классификацией тела 503 (`7be32d2`) **наблюдал**: `Loading model` только
~1 с, затем `loaded_witness_unavailable` 4521 раз — модель загружается, падает
нативный getter. Место броска — из точного upstream `4da6337`: CPU-веса (blk.0–8,
`token_embd` при `-ngl 20`) через mmap лежат в buffer type `CPU_Mapped` с
`device = NULL` (FIXME), а getter требовал device. Исправление `74ae2b6` (только
host-буфер точного типа `CPU_Mapped` → CPU device) **подтверждено drill v8**.
Гипотеза о правах токена опровергнута.

Внешний waiter: v4 ждал ещё 55 с после exit candidate; v5 обнаруживает exit за <1 с.

## 4. Статусы gates (индексы этого задания)

| Gate | Статус | Основание |
|---|---|---|
| **E0** | **CLOSED** | Обе причины установлены и исправлены; диагностика: закрытые стадии/шаги fence/классы, primary≠secondary, sentinel-тесты, отмена загрузки по stop, классификация 503. Challenger: MUST_FIX закрыты. |
| **E1** | **PASSED (SHADOW scope)** | v6 FAILED (стадия наблюдена), v7 FAILED (класс причины наблюдён), **v8 `SHADOW_QUALIFIED_BASELINE_RESTORED`** (05.10 11:58:37–11:58:55Z; запуск подтверждён владельцем, GO challenger): образ `sha256:5eac4947…` из `74ae2b6`; loaded identity = preflight; тот же owner до/после synthetic inference; target weights blk.9–27 + output на CUDA0 (921 МБ), blk.0–8/other на CPU; 401 на неверный credential; отзыв proof/health при гибели child; новый instance и inference после рестарта; exact retained OLD (health 200); простой m0 ~19 с. Receipts `edge_owner_shadow_attempt_2026-10-05*.json`, `edge_owner_shadow_qualified_2026-10-05.json`. Не доказаны mmproj/context/kernels/performance; LIVE-recipe не квалифицирован. |
| **B0** | **BLOCKED → путь согласован** | R3 (9 836 файлов, 2.18 ГБ) не захвачен: попытка шла от `CodexSandboxOffline`, у которой нет `FILE_READ_ATTRIBUTES` на профиль. Владелец разрешил вариант A — §7. Даже после capture B0 не закрыт: зашифрованная копия/restore R3 — отдельный executor; 61 622 пути не классифицированы; SRC-013 original не найден. IndependentBootstrapBackup не выдан. |
| **L0** | **CLOSED (код + synthetic)** | `93ebfbf`: allowlist операций Engine API; профиль из approved полей, Env только имена + BoundFile; post-create inspect до start; write-ahead journal; lost-ACK/409 без повторного create; удаление только journal-owned ID; проверки daemon/binding до парковки оригиналов; Compose не вызывается. 388 PASS Windows и Linux (JUnit в `first_live_engine_create_cpu_2026-10-05.json`). Production block сохранён. |
| **L1** | **NOT_RUN** | Нет утверждённого изолированного stand; daemon pin — заглушки. `_joined_proof`/fingerprint требуют Compose labels — activation невозможна и после L1; Compose `rebind` опасен, пока есть parked originals. |
| **I1** | **NOT_RUN** | all49 actual transport, restart/rollback не выполнялись. |
| **P1** | **NOT_RUN** | Production switch не выполнялся; production m0 — прежний retained container. |
| **S0** | **BLOCKED** | Генеративного владельца с `vkm-evidence-model/1` + `/v1/chat/completions` нет ни на CORE, ни на EDGE; EDGE GPU 4 ГБ занят visual owner; основной GPU запрещён. |
| **S1** | **NOT_RUN** | Bounded набор оригиналов через adapters не прогонялся. |
| **H0** | **CLOSED** | 60/60 H без изменений, lineage импортирован, идемпотентно; новые original checks NOT_RUN; не S26 gold (`586a588`). |
| **S26** | **NOT_READY** | Нет independent original-first gold; S0/S1 не закрыты; кампания не запускалась. |

## 5. Коммиты этой сессии (PUBLIC)

`b70d406` first-LIVE protective · `2ee373d` receipts 04.10 · `586a588` H0 ·
`f1dfd68` E0 диагностика · `98c3d71` E0 receipt · `5ee07e4` config identity без токена ·
`3c0d4c8` receipt v6 · `7be32d2` классификация 503 · `93ebfbf` L0 · `4032029`/`8a21765`
передача · `38ed700` receipt v7 · `74ae2b6` нативный witness fix · `eb88c79` receipt E1 ·
затем этот документ. PRIVATE не изменялся.

Тесты: Linux CPython 3.13.5 (WSL) — широкий прогон tests/evidence + tests/world +
owner/H0/text 2121 passed (46 заявленных NOT_RUN); owner-наборы после последних
правок 365 passed; native/world после патча 748 passed; L0 — 388 PASS на Windows и
Linux; Windows owner — 305 passed / 54 Linux-only. Контроллер drill v5 (work, вне Git):
Linux 66 passed; те же регрессии на байтах v4 — 35 failed (ожидаемо). Нативная delta
скомпилирована на EDGE в pinned builder (network none, exit 0).

## 6. Фактическое состояние серверов

EDGE (наблюдено после v8, ~12:00Z): retained `vkm-rerank-m0` (`d39fc9c8…`, образ
`456889f5…`) running, PID 2722639, health 200 — production reranker прежний, новый owner
в LIVE не переключался. Candidates v4/v6/v7 (exit 2) и v8 (exit 0) остановлены и
сохранены; образы v5–v8 сохранены; prune/cleanup не выполнялись. Read-only наблюдатель
после drill: `work/production_data/owner_runtime_probe_20261004/v8/post_drill_observe.py`
(`ssh edge 'cd /tmp && sudo -n python3 -B -' < …`). CORE не перезагружался и не
переключался; deployed read MCP прежний (README: 45 tools, snapshot 29.09); кандидатные
49 READ tools — NOT_RUN.

Замечание о процессе: после запуска v7 (под действующим разрешением на drills)
классификатор разрешений Claude Code отклонил ожидание его результата («Production
Deploy»); результат v7 получен позже вместе с владельцем. Drill v8 запущен только
после явного подтверждения владельца в чате.

## 7. Решения и действия владельца

1. **R3 capture — вариант A разрешён владельцем.** Право — изменение настроек
   безопасности, команду `icacls` выполняет владелец; exact frozen capture может
   повторить только учётка Codex-sandbox (план приватен ей). Последовательность:
   `work/production_data/recovery_inventory_20261004/R3_RERUN_FOR_CODEX_RU.md`
   (grant `(RA)` без наследования → read-only probe доступа → preflight → `--capture`
   с утверждёнными SHA executor/plan → `/remove:g`).
2. Утверждённый изолированный stand для L1.
3. Решение о квалификации LIVE-recipe visual owner (порт 18083, образ v8) — отдельный
   bounded шаг, затем I1 перед любым switch.

## 8. Следующий конкретный шаг

1. R3 по инструкции (владелец + Codex), затем исполнитель зашифрованной копии/restore R3.
2. L1 на утверждённом stand; затем замена Compose-proof (`_joined_proof`,
   `container_fingerprint`) для Engine-created receivers.
3. LIVE-recipe visual owner и I1 (all49 по транспорту) — до switch.

## 9. CI

Рабочая ветка пушится после каждого блока; hosted workflows (`world-integrity`,
`corpus-offline`, `windows-offline`) запускаются на последней ревизии PR #10.
Результаты в этой сессии не наблюдались: привязка PR в приложении недоступна (`gh` не
авторизован), самостоятельный опрос CI не выполнялся — проверить в PR #10. Локально:
Linux `tests/world` 661 passed / 2 skipped; `verify_canonical_repository.py` с PRIVATE —
36 PASS, 7 SKIPPED_REF_UNAVAILABLE (нет исторических тегов), exit 0. Main не сливался,
protection не менялась.
