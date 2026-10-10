# Изолированная квалификация кандидата API/MCP

`vkm_corpus.update.acceptance` проверяет candidate до запуска strict production
receiver. Harness не читает `CURRENT`/`MAINTENANCE`, не переключает селекторы и не
открывает сетевой endpoint. Реальный путь запроса:

```text
MCP Client → build_read_server → ApiClient → httpx.ASGITransport
  → create_app → ApiService → явно выбранные candidate backends
```

Это локальный Python helper, а не команда, принимающая произвольный URL, Python
callback из JSON или shell. Вызовы реальных backends требуют отдельного разрешённого
runtime. Обычный CPU CI использует синтетические временные данные; сетевые/GPU/GDAL
проверки остаются отдельными `services`/`gpu`/`qgis_runtime` gates.

## Входы и доверенная граница

Оператор создаёт и отдельно утверждает `AcceptancePlan.sha256`. План содержит:

- `CandidatePin`: exact full `code_commit`, реальные `code_tree_sha256`,
  `dependencies_sha256`, `access_config_sha256`, `policy_sha256`,
  `duckdb_file_sha256`, полный набор native `ComponentIdentity` и отдельные
  `ServiceIdentity` для RETRIEVAL/RERANK/CONTROL; data component и service process
  не подменяют друг друга;
- `scope`: `SYNTHETIC` или `SHADOW_PRODUCTION`;
- точный набор `read_tool_names()` — сейчас 49, с ограниченными arguments и
  ожидаемыми object IDs. Только `get_corpus_status` использует иной native status
  contract и не требует object ID. Отсутствующие исходные probe objects означают
  незавершённую квалификацию, а не разрешение заменить вызов `PASS`;
- разрешённый и запрещённый read principals, source ID для policy challenges;
- per-call/total deadline и бюджеты байтов ответов;
- точный SHA отдельного `vkm-deployment-drill/1`,
  `deployment_profile_sha256` и для production — `isolation_attestation_sha256`;
- для CONTROL — typed `control_spec` с actual schema SHA, явными обязательными
  worker roles/kinds и пределом возраста heartbeat; это часть approved plan.

План, policy, access contexts, candidate service и receipt directory принадлежат
оператору. Они не строятся из инструкций документа и не выдаются агентам как
произвольно изменяемая конфигурация. SHA удостоверяет байты, но не полномочия:
утверждение plan SHA и права на registrar остаются внешней доверенной границей.
Ни `SYNTHETIC PASS`, ни ручной словарь `{"status":"PASS"}` не являются production
qualification. Receipt не удостоверяет научную правильность extracted values.

## Wiring для deployment orchestrator

1. Собрать **отдельный** `ApiService` из candidate DuckDB, NAV, evidence и native
   remote clients. Production service object и его selectors не переиспользовать.
   `SourcePolicyStore.sources` обязан перечислять действительные источники этого
   candidate. Backends должны быть доступны только в явно разрешённом scope.
2. Создать native observer **из тех же backend instances и selected paths**.
   Возвращаемое значение — фактический `dict[component, ComponentIdentity JSON]`,
   ровно совпадающий с pin. Чтение ожидаемых sidecars вместо runtime identity не
   квалифицирует GRAPH/SEARCH/packs. Для dataset catalogue применяется его native
   observer; наличие DATASET не означает наличие dataset API.
   Actual service observer создаётся внутри concrete transport через
   `await NativeServiceObserver.bind(cloned_deps, control_spec=plan.control_spec)`.
   Он использует собственные клиенты actual backend instances, а не отдельный URL
   или expected JSON. Альтернативный observer допустим только для SYNTHETIC tests.
3. Создать `CandidateFence(pin, duckdb_path=..., policy_path=..., api_config=...,
   observer=..., production=scope == "SHADOW_PRODUCTION", max_duckdb_bytes=...)`.
   Положительный byte budget ограничивает каждый полный hash (default 8 GiB);
   выполняются полные проверки до/после кампании. Между запросами проверяются
   native identity, policy bytes, effective access configuration и file signature.
   Перед первым hash устанавливается `NativeFileWatch` для DuckDB и policy:
   совпадение size/mtime/ctime не скрывает событие записи. Недоступный watch,
   mutation или overflow навсегда закрывает lease; fallback к одним timestamps
   отсутствует. Допустимы только квалифицированные Linux local filesystems,
   не сетевые/FUSE/DrvFS. Transport закрывает lease при выходе, включая ошибку
   startup; новая попытка требует нового fence.
   Production требует Linux и clean checkout на exact commit; установленное
   дерево serving packages и фактические версии dependencies сравниваются с pin.
4. Выполнить отдельный **изолированный deployment fault drill** кодом deployment
   adapter. Зарегистрировать его immutable JSON и все journal receipts в доверенном
   content-addressed каталоге. Drill должен иметь scope/context того же плана,
   совпадение before/after-restore native identity и точные checks:
   `admission_closed`, `drained`, `partial_apply_failed`,
   `restored_exact_previous`, `rollback_admission` — все PASS. Его transitions
   имеют `{phase, journal_sha256}`, `journal_receipts` содержит те же hashes в
   том же порядке. Registrar проверяет hash-linked journal chain, один request key,
   exact restore native/bindings hashes и реальные partial mutation hashes.
   Последовательность допускает потерю ACK: APPLYING → FAULT_INJECTED без APPLIED,
   затем повторный close/drain → RESTORING → RESTORED → FAILED_RESTORED.
   Synthetic drill не повышается до production scope.
5. Создать `AcceptanceRegistrar(existing_directory,
   approved_plan_sha256=plan.sha256)`. Каталог должен находиться вне PUBLIC Git и
   быть недоступен для записи corpus/agent inputs. Содержимое metadata может
   раскрывать operational identities, даже если текст источников не сохраняется.
6. Вызвать:

```python
async with PrivateASGIProbeTransport(candidate_service, api_config, fence, plan) as transport:
    result = await qualify_shadow(plan, transport=transport, fence=fence, registrar=registrar)
```

`PrivateASGIProbeTransport` создаёт собственный service с candidate guard, а не
публичный compatibility endpoint. Скопированный `admission_barrier` сбрасывается
в `None`: приостановленный live receiver не участвует в private bootstrap.
Приватный wrapper буферизует ограниченный response и выпускает bytes только после
повторной проверки policy/files/native services. Ошибка возвращает 503 без
исходного payload. Async service observation выполняется также вокруг MCP probes
и перед регистрацией; `asyncio.run` внутри ASGI не используется.
`ApiConfig` включает реальные права intended
deployment, но probe transport использует только read tokens. Каждый запрос
проходит настоящие MCP/tool-schema/API/auth/policy слои. Positive READ tool
response должен быть полным, без warnings и dependency errors, с ожидаемыми IDs
и snapshot. Image probes проверяют actual PNG/JPEG. Status probe отдельно
проверяет pinned shadow admission и native canonical status.

Фиксированные API challenges: anonymous GET → 401; denied principal GET → 403;
permitted GET → 200; read-token reprocess POST → 403. Последний запрос не
выполняет reprocessing: реальная write authorization обязана отвергнуть его.

7. Только при `status == "PASS"` использовать `receipt_sha256` как
   `GenerationManifest.acceptance_sha256`. Сам receipt не включает generation SHA:
   он связывает sorted component identities, поэтому hash cycle отсутствует.
   Production receiver повторно проверяет actual code/dependencies/access,
   policy, component/service sets и DuckDB bytes. Дальнейший switch/rollback принадлежит
   отдельному deployment coordinator и его barrier.

## Durable receipts, retries и отказы

`vkm-serving-acceptance/1` имеет точные `SERVING_CHECKS` и `read_tool_names()`,
candidate identity, plan/drill hashes и `raw_probe_receipts`. Каждый probe receipt
содержит hash arguments, raw response SHA/size и observed component/service-map SHA.
Текст, image bytes, bearer tokens и частные arguments в receipt не копируются.
Это operational qualification, а не повторная научная проверка этих payloads.

Публикация использует fsync staging file → no-replace hard link → directory fsync;
существующий hash-файл допускает только идентичные байты. Потерянный ACK не даёт
права повторно запускать действия переключения. Можно повторить
`registrar.register(json.loads(registrar.read(receipt_sha256)), the_same_plan)`:
передавать нужно исходное тело отчёта, без добавленного helper поля `receipt_sha256`.
Registrar повторно проверяет plan,
candidate/drill/probe bindings и восстанавливает durable acknowledgement.
Новая серия READ probes — новый attempt, потому что raw API responses содержат
собственные request IDs; старое acceptance не переносится на изменившиеся bytes.

Отсутствующий transport/drill → durable `NOT_RUN` attempt. Timeout, неполный
toolset, SKIP/NOT_RUN/generic PASS вместо actual ответа, политика или identity,
изменившаяся в процессе, ошибочный drill, checksum или fsync → нет serving
acceptance. `attempt_receipt_sha256` никогда не подставляется вместо
`receipt_sha256`; downstream consumer обязан требовать PASS + SHADOW_PRODUCTION.

При отмене task, аварии процесса или потере durability остаются только отдельные
immutable probe receipts/незавершённый attempt; отсутствие acceptance закрывает
admission. Registrar не меняет production selectors, данные или source files.

## Текущие ограничения

- Реальные CORE/EDGE/native model endpoints и production fault drill здесь не
  выполнялись; CPU tests не дают `SHADOW_PRODUCTION` evidence.
- Native service observer связан с actual RERANK/RETRIEVAL/CONTROL backends.
  Старый provider без authenticated native `/identity`, owned model proof,
  необходимой PostgreSQL native identity/свежих workers закрывает qualification.
  Наличие этого кода не квалифицирует существующий deployment; `/status` и
  `/readyz` не заменяют native identity и реальный full READ drill.
- Isolation attestation, registrar ownership и привязка observer к actual selected
  remote clients проверяются trusted deployment integration. In-process helper не
  является защитой от привилегированного оператора, подменившего свой Python code.
- Windows выполняет pure/synthetic contract tests; durable production receiver
  и registrar сейчас квалифицируются только на Linux.
- Coroutine deadline и ограничение serialized response не заменяют внешний
  process-tree/memory guard. Native observer и underlying clients должны иметь
  собственные bounded I/O timeouts; будущий runtime запускает harness в заранее
  ограниченном процессе. Отмена coroutine не удостоверяет остановку внешнего GPU
  вызова, поэтому timeout всегда закрывает acceptance.
