# SKRU-1 Main — Claude Code Cloud Instructions

## Current scientific direction

Current chain:

`scientific evidence -> Physical Evidence Consolidation -> Physical World v1 -> OpenGeoSys reference case -> physical ensemble -> preregistered forecasting benchmark`.

Текущий приоритет — первые четыре звена. Старые v3/v3.2 reconstructed/model-ready artifacts не возвращать.

## Read first

Перед изменениями прочитай:

1. `README_FIRST.md`;
2. `AGENTS.md`;
3. `docs/CANONICAL_RESEARCH_STATE_RU.md`;
4. `docs/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md`;
5. `docs/REPOSITORY_CONSOLIDATION_2026-09-25_RU.md`;
6. governance docs, experiment protocol и acceptance criteria;
7. relevant `.claude/skills/`.

## Cloud-only execution

Пользователь не обязан иметь OGS локально. Если parent task разрешает OGS execution, разверни runtime в Claude Code cloud VM самостоятельно.

Предпочтительно:

```bash
python3 -m venv .venv-ogs
. .venv-ogs/bin/activate
python -m pip install --upgrade pip
python -m pip install 'ogstools[ogs]'
ogs --version
```

Не коммить `.venv-ogs`.

При failure сохрани receipt и исследуй fallback только внутри cloud VM. Не перекладывай установку на пользователя.

## OGS execution permission

Старое правило AGENTS.md о запрете solver runs означает: solver нельзя запускать самовольно в обычных задачах.

Если parent task **явно** поставлена как Physical Evidence / Physical World / OpenGeoSys cloud research и требует executable verification, это является отдельным явным разрешением на ограниченные OGS research runs.

Разрешены:

- provisioning OGS;
- official/sample sanity checks;
- toy feature tests;
- integrated minimal OGS case;
- первый evidence-backed reference skeleton, если задача это требует;
- deterministic physics/units/geometry calculations.

Не разрешены без следующего отдельного gate:

- production physical ensemble;
- forecasting benchmark;
- ML training/tuning;
- calibration against evaluator/test truth;
- заявления о field validation.

## Scientific rules

- `FACT`, `DERIVATION`, `MODEL_CHOICE`, `ENGINEERING_ASSUMPTION`, `UNKNOWN` всегда разделены.
- Каждый numeric parameter имеет provenance или explicit assumption status.
- Analogue/VKM-regional evidence не становится SITE_SPECIFIC автоматически.
- Ranges не схлопываются в point estimate без причины.
- OGS implementation limitations не переписывают scientific evidence.

## Execution ladder

Для OGS двигайся ступенчато:

1. install/version verification;
2. minimal mechanics;
3. material regions;
4. gravity/loading;
5. BC/IC;
6. time stepping;
7. required rheology/time dependence;
8. output extraction;
9. integrated toy case;
10. evidence-backed reference skeleton.

Каждый шаг имеет config/input, command, log, exit code, expected-output check и receipt.

## Context7

Context7 использовать для актуальной документации OGS. Не полагаться на старый синтаксис из памяти.

## Two-repository rule

Scientific binaries и curated evidence принадлежат private resources repo. Main repo хранит contracts, code, validation, solver configs и current derived research artifacts.

Если resources repo доступен из cloud task, используй его как evidence source. Если нет — используй versioned handoff/export и явно отметь ограничение доступа.

## Completion condition

Работа не считается завершённой только потому, что создан YAML или написан отчёт. Для критических OGS capabilities, заявленных как реализуемые, требуется реальный cloud execution либо доказанный blocker.
