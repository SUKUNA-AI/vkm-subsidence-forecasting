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
  контроллер v5, сборка образов v5/v6 и bounded SHADOW drill, receipts, коммиты.
- Субагент B: L0 — фиксированный Engine-create adapter (`engine_create.py`) и тесты.
- Субагент C: H0 — импорт 60 исторических H-меток с lineage.
- Субагент D: read-only диагностика R3 WinError 5.
- Challenger E0 (не автор): 3 раунда + 2 pre-drill проверки пакета. Нашёл, в т.ч.,
  MUST_FIX в генераторе receipt и **NO-GO** на пакет v5: `config_sha256` owner включал
  эфемерный witness-токен — успешно загруженный owner никогда не совпал бы с планом.
  Исправлено в `5ee07e4`, после чего дан GO на v6.
- Challenger L0 (не автор): 2 раунда — MUST_FIX (утечка Env-секретов в control root),
  SHOULD_FIX F2–F6 (поздний create, security-поля, crash-loop, сокет, mount/cap-политика)
  и R1–R4 (проверки после парковки оригиналов и др.) — все закрыты.

Находки субагентов перепроверялись координатором (повторные прогоны тестов, сверка
владельцев файлов R3 через ACL), а не объединялись механически.

## 3. Две причины EDGE-отказа 04.10 (раздельно)

**Повторная recovery `ValueError`.** Механизм воспроизведён на точном коде v4 (sha
`6ece9bcd…`) read-only на EDGE: Docker строит `Mounts` в inspect из Go-map, и для
retained OLD (2 mounts) порядок обратный в 27/200 вызовов; `inspect_old()` сравнивал
упорядоченный список и падал в 29/200. Неперехваченные вызовы — в `recover()` и
`fallback()`. Статус: наиболее вероятная причина (receipts 04.10 сохранили только
класс; транзиентный сбой docker CLI не исключён). Исправлено в контроллере v5 и
**подтверждено на runtime 05.10**: повторная recovery — `ALREADY_RESTORED_VERIFIED_NOOP`,
watchdog `FAILED_DRILL_BASELINE_RESTORED` (вместо `FAILED_RECOVERY_…`).

**Startup exit 2.** 04.10 стадия была выведена только по таймингу. 05.10 образ с
bounded-диагностикой показал её **наблюдаемо**: listener child поднят за ~150 мс,
затем `/__vkm_loaded_witness` 4579 раз отвечал 503 до дедлайна 180 с; child жив,
лишних mapped-библиотек нет. Корень — **выведен из точного upstream `4da6337`**:
CPU-веса (blk.0–8 и `token_embd` при `-ngl 20`) через mmap попадают в buffer type
`ggml_backend_cpu_buffer_from_ptr_type` с `.device = NULL // FIXME`; нативный witness
getter требует непустой device → исключение → 503. Не исключено: незавершённая
загрузка (middleware `Loading model` тоже 503); для различения добавлена закрытая
классификация тела 503 (`7be32d2`). Гипотеза о правах токена опровергнута таймингом.

Внешний waiter: v4 ждал ещё 55 с после exit candidate; v5 обнаружил exit за <1 с.

## 4. Статусы gates (индексы этого задания)

| Gate | Статус | Основание |
|---|---|---|
| **E0** | **CLOSED** | Обе причины установлены (recovery — механизм воспроизведён и исправление подтверждено на runtime; startup — стадия наблюдена, корень выведен из исходника). Диагностика: закрытые стадии/шаги fence/классы, primary≠secondary, sentinel-тесты, отмена загрузки по stop. Baseline сохранён. Challenger: MUST_FIX закрыты. |
| **E1** | **FAILED** (v6, baseline restored); v7 — **результат не наблюдён** | Drill v6 05.10 10:25–10:28Z: образ `sha256:d8f19975…` из `5ee07e4`, план одобрен challenger. Loaded identity/placement/inference не получены. OLD восстановлен (новый PID 2681294, health 200), простой m0 187 с. Receipt `edge_owner_shadow_attempt_2026-10-05.json`. Диагностический drill v7 (образ `sha256:417f4d11…` из `7be32d2`, классификация 503; GO challenger) запущен 10:40:59Z, PID контроллера 2688002. Дальнейшее ожидание и чтение результата отклонены классификатором разрешений Claude Code («Production Deploy») — состояние после v7 должен проверить владелец (§6a). |
| **B0** | **BLOCKED** | R3 (9 836 файлов, 2.18 ГБ) не захвачен. Причина WinError 5 установлена: попытка шла от учётки `CodexSandboxOffline`, у которой нет `FILE_READ_ATTRIBUTES` на `C:\Users\SUKUNA-AI` (Python-гард открывает все родительские каталоги). Нужно решение владельца (§7). R0/R1/R2 — как в checkpoint 04.10; 61 622 пути не классифицированы; SRC-013 original не найден. IndependentBootstrapBackup не выдан. |
| **L0** | **CLOSED (код + synthetic)** | `93ebfbf`: Engine-create adapter (allowlist операций Engine API; профиль из approved полей, Env только имена + BoundFile; post-create inspect до start; write-ahead journal; lost-ACK/409 без повторного create; удаление только journal-owned ID; проверки daemon/binding до парковки оригиналов; Compose не вызывается). 388 PASS Windows и Linux (JUnit в `first_live_engine_create_cpu_2026-10-05.json`). Challenger 2 раунда: MUST_FIX F1 (секреты Env в control root) и SHOULD_FIX F2–F6, R1–R4 закрыты. Production block сохранён. |
| **L1** | **NOT_RUN** | Реальный изолированный Engine lifecycle не выполнялся (нет утверждённого synthetic stand); значения daemon pin — заглушки до L1. `_joined_proof`/fingerprint ещё требуют Compose labels — activation невозможна и после L1; Compose `rebind` опасен, пока есть parked originals. |
| **I1** | **NOT_RUN** | all49 actual transport, restart/rollback не выполнялись (зависит от E1/B0/L1). |
| **P1** | **NOT_RUN** | Production switch не выполнялся; stable baseline сохранён. |
| **S0** | **BLOCKED** | Генеративного владельца с `GET /identity` `vkm-evidence-model/1` + `POST /v1/chat/completions` нет ни на CORE, ни на EDGE (read-only inventory). EDGE GPU 4 ГБ занят visual owner; основной GPU запрещён; тяжёлый deploy не разрешён. |
| **S1** | **NOT_RUN** | Bounded набор оригиналов через adapters не прогонялся в этой сессии. |
| **H0** | **CLOSED** | 60/60 H без изменений, lineage импортирован, идемпотентно; новые original checks NOT_RUN; не S26 gold (`586a588`). |
| **S26** | **NOT_READY** | Нет independent original-first gold, S0/S1 не закрыты; кампания не запускалась. |

## 5. Коммиты этой сессии (PUBLIC)

`b70d406` first-LIVE protective (байты checkpoint) · `2ee373d` receipts 04.10 ·
`586a588` H0 · `f1dfd68` E0 диагностика · `98c3d71` E0 receipt · `5ee07e4` config
identity без токена · `3c0d4c8` receipt E1 05.10 · `7be32d2` классификация 503 ·
`93ebfbf` L0 Engine-create adapter · (этот документ и PROJECT_STATE — следующим коммитом).
PRIVATE не изменялся.

Тесты (финальные прогоны): Linux CPython 3.13.5 (WSL) — owner/bridge/preflight/
model_owner/text/offline-checks 365 passed (44 NOT_RUN: нет исходников CareerOps);
Windows — 305 passed / 54 Linux-only; first-LIVE/L0/схемы — 388 PASS на обеих ОС; H0 — 47 passed.
Широкий Linux-прогон рабочего дерева перед коммитом L0 (tests/evidence + tests/world +
owner/H0/text): 2121 passed, 46 skipped (заявленные NOT_RUN).
Контроллер drill v5 (work, вне Git): Linux 66 passed; те же регрессии на байтах v4 — 35 failed (ожидаемо).
CI на финальной ревизии — см. §9.

## 6. Фактическое состояние серверов после последнего наблюдённого действия

Последнее **наблюдённое** состояние EDGE (после v6, 10:28–10:35Z): retained
`vkm-rerank-m0` (`d39fc9c8…`, образ `456889f5…`) running, PID 2681294, health 200;
candidates v4/v6 остановлены (exit 2), сохранены; образы v5 `8787828d…`,
v6 `d8f19975…`, v7 `417f4d11…` сохранены; prune/cleanup не выполнялись. После
запуска v7 (10:40:59Z) состояние **не наблюдалось** — см. §6a. CORE не перезагружался и не переключался; deployed
read MCP — прежний (README: 45 tools, snapshot 29.09); кандидатные 49 READ tools — NOT_RUN.

## 6a. Проверка после drill v7 (выполнить владельцу)

Контроллер v7 работает автономно (bounded 420 + 180 с) и сам восстанавливает exact
retained OLD; этот путь подтверждён на runtime 05.10 (v6). Наблюдение после него —
только чтение (docker inspect, loopback health, фазы журнала, закрытая диагностика):

```bash
ssh edge 'cd /tmp && sudo -n python3 -B -' < work/production_data/owner_runtime_probe_20261004/v7/post_drill_observe.py
```

Ожидаемо: `old.Running=true`, `old_health=200`, фазы журнала заканчиваются
`EXACT_RETAINED_BASELINE_RESTORED` (+ `RECOVERY_VERIFIED_ALREADY_RESTORED`).
Ключевой ответ — `fence_step` в `candidate_public_diagnostic_from_logs`:
`WITNESS_GETTER_UNAVAILABLE` подтверждает выведенный корень (getter, device NULL),
`WITNESS_SERVER_LOADING` — что загрузка не завершается. Если OLD не running —
вернуть exact retained container: `ssh edge docker start d39fc9c8013a47f986a0358dce614c92758cb65c0c09379ea8756752ee1df500`.

## 7. Решения, нужные от владельца

1. **R3 capture.** Вариант A (рекомендован агентом D, сохраняет утверждённые SHA
   executor/plan): точечно выдать учётке sandbox только право чтения атрибутов на
   один каталог, без наследования, затем повторить exact frozen capture и снять право.
   Это изменение настроек безопасности — выполняется владельцем:
   `icacls "C:\Users\SUKUNA-AI" /grant "DESKTOP-2CJ805E\CodexSandboxOffline:(RA)"`,
   после — `/remove:g`. Вариант B: перепин Python вне профиля → новый executor SHA и ревью.
2. **Нативное исправление witness getter** (host buffer без device → CPU device) и
   пересборка native cache — отдельная квалификация (сборка CUDA llama.cpp на EDGE).
3. Утверждённый изолированный stand для L1.

## 8. Следующий конкретный шаг

E1: исправить `get_loaded_witness` в `0002-vkm-owned-loaded-witness.patch` и
согласованный Python-контракт placement (`TargetWeightTensor.device`), пересобрать
native cache и образ, повторить bounded drill v7 с тем же контроллером; классификация
503 из `7be32d2` подтвердит или опровергнет вывод о корне до исправления.

## 9. CI

Рабочая ветка запушена (`bd81589..4032029`, затем этот docs-коммит); hosted workflows
(`world-integrity`, `corpus-offline`, `windows-offline`) запускаются на финальной
ревизии ветки PR #10. Результаты в этой сессии **не наблюдались**: привязка PR в
приложении недоступна (`gh` не авторизован), самостоятельный опрос CI не выполнялся.
Проверить в PR #10. Локально перед push: Linux `tests/world` 661 passed / 2 skipped;
`verify_canonical_repository.py` с PRIVATE — 36 PASS, 7 SKIPPED_REF_UNAVAILABLE
(локально нет исторических тегов), exit 0. Main не сливался, protection не менялась.
