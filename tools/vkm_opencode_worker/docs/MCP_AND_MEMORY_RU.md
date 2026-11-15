# MCP, индексы и память без второго научного канона

## Матрица сценариев

| Сценарий | Основные инструменты | Приёмка |
|---|---|---|
| code_investigation | local Graphify + Serena; ast-grep при структурной гипотезе | реальные definition/reference/path, покрытие гипотез, file:line |
| structural_change | Serena read, ast-grep matches/rule test, exact edit + адресные тесты | минимальный patch, positive/negative/regression tests, scope |
| source_research | существующий vkm-corpus/NAV/ScientificReader; Context7 только для software API | source/work/page/figure и эпистемический статус |
| independent_review | чтение diff, Serena references, ast-grep; Context7 для версий | воспроизводимые замечания по severity, без “голосования” |

Это начальные роли, не автозапуск всех инструментов. Нужны цель, budget, минимальные APIs и evidence. Native tool lists считываются при setup; они не выводятся из рекламной таблицы.

## Graphify

Использовать именно **Graphify-Labs/graphify local runtime**, не hosted Graphify MCP по умолчанию. Репозиторий описывает deterministic local AST/derived graph и отдельный MCP serve. Он не подключается магически к произвольной имеющейся Neo4j-схеме. Code graph, scientific DOCUMENT/NAV graph и persistent agent memory — разные сущности.

Зафиксировать repo_id/base SHA, source content digest, graphify/parser version, build parameters и graph snapshot hash. Сохранять производный graph в существующем разрешённом derived/work-каталоге, переиспользовать при совпадающих входах. Запросы ограничивать nodes/edges/depth/result bytes. `shortest_path` или сообщество — навигационная подсказка, не доказательство влияния изменения.

Официальный local entrypoint выбранной версии устанавливать по --help; опубликованные варианты включали `python -m graphify.serve <graph.json>`. Не подставлять `--graph` без проверки. Из tools/list выбрать только query/navigation; rebuild/index не выдавать каждому worker автоматически. Не передавать весь private corpus parser-у ради установки code navigation.

## Serena

Проверить версии language backend и project config. Символьные определения/ссылки в Python, C#/C++ и других языках могут иметь разные пределы. Задокументировать фактически проверенные языки; reflection/dynamic imports не объявлять полностью найденными.

Рабочий project root должен быть текущим worktree или явно закреплённым snapshot, не родительским dirty checkout. Сначала read-only find/overview/references. Rename/replace имеют побочные эффекты независимо от OpenCode edit_paths: их включать только с проверенной scope-защитой, иначе изменения производить обычным ограниченным file tool. Workspace caches не научная память.

## ast-grep

Structural pattern не эквивалентен semantic call graph. Использовать известные positive/negative fixtures, язык и rule version. Ограничивать result count/bytes, не читать весь многомегабайтный JSON в контекст. Реальный MCP проект может иметь экспериментальный статус; pin и smoke обязательны. Rewrite должен иметь заранее известный набор файлов и просмотренный diff. Не ставить floating server на каждом запуске.

## Context7

Сначала определить версию зависимости из lock/installed metadata; resolve library → получить документацию той версии. Если нужной версии нет, зафиксировать mismatch, не утверждать version-correct. Использовать для программных API, не для доказательств геологии ВКМ. API questions не должны включать лишние private source pages. Точные имена `resolve-library-id`, `query-docs` и/или иные выбранного сервера брать из tools/list; дефисы OpenCode V2 не заменять выдуманными underscores.

## Existing VKM MCP

Не создавать копию corpus. Привязать `vkm_corpus` descriptor к действующему read-only MCP; current snapshot и source locators брать из настоящего ответа. QGIS/CAD только когда задаче нужны соответствующие native операции; ресурсы serializable/locked согласно проектному harness. Пользовательские permissions на private reading не превращаются в разрешение публикации/production write.

## Память и стоимость

Нативные OpenCode sessions дают историю, но не общую достоверную память Codex/OpenCode. После приёмки Codex сохраняет краткий handoff: task/question, base SHA, input hashes, accepted findings, test receipts, source refs, unresolved UNKNOWN, reuse conditions. Место — существующее approved project storage, не новый “memory server” без нужды.

Сжатие контекста/skills/retrieval могут уменьшать повторяемый вход; многошаговый agent, summary и compaction сами расходуют токены. MCP tool schemas тоже занимают контекст. Поэтому задача не “запускать побольше”, а давать одному специалисту bounded work с minimal tools и reuse принятого результата. Сравнивать cost на **принятый результат**, считать rework и latency. Экономию в процентах до измерения не обещать.
