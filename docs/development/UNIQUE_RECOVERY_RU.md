# Явная инвентаризация уникальных файлов и synthetic restore drill

`infra/recovery/unique_inventory.py` — отдельный инструмент стандартной библиотеки
Python. Он не обходит весь `work/`, PRIVATE, WSL или диски. Оператор перечисляет
конкретные файлы в локальном manifest. `plan` и `verify` не создают и не изменяют
файлы. Команды реального копирования, восстановления, удаления и cleanup отсутствуют.

Наличие инструмента не доказывает полноту реального backup. Реальная инвентаризация
workstation/CORE/EDGE, проверка независимой копии и restore настоящих artifacts
в этой сессии не выполнялись.

## Классификация

| Поле / значение | Значение |
|---|---|
| input `CANONICAL` | Оригинал или уникальный невоспроизводимый результат; нужна независимая копия |
| input `REPRODUCIBLE` | Явное операторское заявление: есть pinned recipe и входы; код ничего не пересоздаёт |
| output `UNBACKED_UNIQUE` | Вычисляется для CANONICAL, если независимая копия не доказана; вручную задать этот класс нельзя |
| output `CANONICAL` | Свежие полные хеши подтвердили независимую копию текущего оригинала |
| `DECLARED_NOT_DRILLED` | Reproduction recipe объявлен, но не выполнен; это не restore PASS |

CANONICAL без доступного оригинала, с ошибкой чтения, явной изменяемостью или
изменением в процессе проверки остаётся UNBACKED_UNIQUE. Имя файла, размер и mtime
не доказывают сохранность. Одинаковые байты на одной файловой системе, hardlink и
копия в том же failure domain не считаются независимым backup.

Для независимой копии одновременно нужны разные filesystem device IDs, разные
объявленные failure domains, разные file identities и совпадение свежих полных
SHA-256/размеров. Оператор обязан подтвердить независимость failure domains:
разделы одного физического диска и mount aliases сами по себе её не доказывают.
Receipt явно содержит `physical_failure_domains=OPERATOR_ATTESTATION_REQUIRED`.
Удалённый объект, недоступный как локально смонтированный regular file, этим
инструментом не подтверждается; sidecar с чужим хешем не заменяет чтение копии.

## Manifest оператора

Manifest хранится вне PUBLIC Git. Корни передаются через специально выбранные
переменные окружения; receipt не печатает их значения, относительные пути или
содержимое файлов. Выбирайте нейтральные `logical_id` и `failure_domain`: они
попадают в отчёт. Все locators — explicit file paths, без glob, directory walk,
`..`, alternate streams, device names и symlink/junction traversal. При отсутствии
переменной корня запись получает ROOT_UNAVAILABLE.

Минимальный пример формы, с условными относительными именами:

```json
{
  "schema": "vkm-unique-inventory/1",
  "roots": [
    {
      "root_id": "workstation",
      "location_env": "VKM_RECOVERY_ORIGINALS",
      "failure_domain": "workstation-disk",
      "role": "ORIGINAL"
    },
    {
      "root_id": "independent-copy",
      "location_env": "VKM_RECOVERY_COPY",
      "failure_domain": "edge-backup-disk",
      "role": "COPY"
    }
  ],
  "items": [
    {
      "logical_id": "unique-launcher-001",
      "classification": "CANONICAL",
      "source": {"root_id": "workstation", "path": "approved-items/launcher.py"},
      "mutable": false,
      "copies": [
        {"root_id": "independent-copy", "path": "snapshot/launcher.py"}
      ]
    }
  ]
}
```

Для pinned оригинала добавляется `expected_sha256` из ранее проверенного manifest.
Без него результат доказывает только равенство текущих байтов (`CURRENT_BYTES_ONLY`),
а не их тождество историческому оригиналу. REPRODUCIBLE дополнительно требует
`reproduction: {"recipe_sha256": "<64 hex>", "input_ids": ["<logical_id>"]}`.
Recipe должен идентифицировать точный код, конфигурацию, версии моделей/зависимостей
и входы. Его SHA здесь только объявляется, выполнение не проверяется. Все input IDs
должны присутствовать в manifest; дубликаты, циклы и неоднозначный регистр IDs запрещены.

`mutable=true` всегда требует отдельного quiescent snapshot и не получает backup
PASS. Живой лог, sqlite/WAL, DuckDB или изменяющееся дерево не превращаются в
согласованный snapshot несколькими `stat`. Сначала оператор останавливает запись
или получает согласованный immutable snapshot подходящим для приложения способом.

## Plan и byte verification

Обе команды только читают и возвращают JSON в stdout:

```text
python infra/recovery/unique_inventory.py plan --manifest <local-manifest.json> --byte-budget 16777216
python infra/recovery/unique_inventory.py verify --manifest <local-manifest.json> --byte-budget 16777216 --confirm-plan <plan_sha256>
```

`--byte-budget` обязателен по смыслу: без положительного явного бюджета результат
BLOCKED, без чтения содержимого inventory items. Plan читает только metadata
перечисленных файлов; на Windows открывает защищённые metadata handles, но не
читает байты. Manifest ограничен 1 MiB, 100 roots, 10 000 items и 16 copies/item;
его control-plane чтение ограничено отдельно от бюджета содержимого файлов.

`max_bytes_to_read` — удвоенная сумма размеров доступных immutable originals/copies
плюс по одному байту на файл для каждого прохода, чтобы безопасно обнаружить рост.
Недостаточный бюджет не
запускает частичную проверку. План связывает manifest, корни, metadata и бюджет;
изменение этих входов требует нового `plan_sha256`. READY означает готовность
запустить проверку, а не готовность backup: статусы каждой записи нужно читать отдельно.

Verify повторяет план, затем последовательно читает полные байты, без hash cache
по size/mtime. Сравниваются identity до/после чтения, затем все доступные файлы
полностью хешируются второй раз: coarse/cached timestamps не заменяют контроль bytes.
Фактические байты учитываются бюджетом; исключения возвращаются фиксированным
кодом без текста ОС, путей и содержимого. POSIX использует `openat/O_NOFOLLOW`
на каждом компоненте. Windows отвергает reparse points, удерживает handles
родителей без delete sharing, а чтение файла запрещает concurrent write/delete.
Если безопасный open недоступен из-за ACL/runtime, запись не получает PASS.

Ограничение: это ограниченная по объёму проверка quiescent файлов, не distributed
snapshot и не защита от привилегированного процесса, изменяющего filesystem.
Receipt подтверждает состояние в момент проверки; дальнейшая запись требует
нового verification. В stdout отсутствует полное содержимое, но размеры, нейтральные
ID и SHA тоже могут быть чувствительными metadata — сохранять receipt согласно
access class исходных файлов, не публиковать автоматически.

Коды выхода: `0` — READY/PASS/PASS_WITH_REPRODUCTION_NOT_RUN; `1` — FAIL/BLOCKED;
ошибка синтаксиса CLI — `2`. Partial reproduction не объявляется полностью прошедшим
restore: поле `restore_status` остаётся NOT_RUN, а декларации перечислены поштучно.

## Synthetic drill без настоящих originals

Единственная пишущая команда принимает только новый sandbox внутри настроенного
системного temporary directory и явный бюджет. Она не принимает manifest, source
path или произвольный payload. Родитель sandbox должен существовать; существующий
sandbox, symlink/junction и выход за temp отвергаются. Удаления/cleanup нет.
Sandbox должен оставаться под исключительным контролем оператора: это не сервис
совместной записи с недоверенным процессом, работающим с теми же правами ОС.

PowerShell, в существующем Python окружении:

```powershell
$recoverySandbox = Join-Path $env:TEMP ('vkm-recovery-drill-' + [guid]::NewGuid().ToString('N'))
python infra/recovery/unique_inventory.py synthetic-drill --sandbox $recoverySandbox --byte-budget 65536
```

POSIX, имя `vkm-recovery-drill-unique-id` оператор должен выбрать новым:

```sh
python infra/recovery/unique_inventory.py synthetic-drill --sandbox "${TMPDIR:-/tmp}/vkm-recovery-drill-unique-id" --byte-budget 65536
```

Drill создаёт фиксированную синтетику, копирует её в synthetic backup, восстанавливает
в третий новый файл и сравнивает полные SHA всех трёх. Original повторно проверяется
без перезаписи. В sandbox остаются три файла и `receipt.json`; повторное исполнение
в том же каталоге не перезаписывает их. `scope=GENERATED_SYNTHETIC_BYTES_ONLY`,
`real_restore=NOT_RUN`, `independent_backup=NOT_RUN`, `power_loss_durability=NOT_RUN`.
Drill не проверяет независимый диск/хост или восстановление 38 GB настоящих данных.

## Что перечислить в настоящем inventory

Оператор формирует небольшой поэтапный manifest, начиная с файлов, потерю которых
нельзя компенсировать из Git/канона:

| Категория | Кандидатная классификация / условие |
|---|---|
| Игнорируемые launchers/export scripts в work, не попавшие в Git | CANONICAL, пока нет проверенного tracked эквивалента |
| Уникальные geometry/model outputs и ручные corrections/review decisions | CANONICAL; generated не означает reproducible |
| Оригиналы и их multi-file manifests | CANONICAL, каждый companion file указан явно |
| Environment definitions, exact locks, version/recipe metadata | CANONICAL; venv целиком не выбирается автоматически |
| MCP/service configs | Только явно разрешённые файлы, защищённая локальная копия; secrets не выводятся |
| Tracked source code | REPRODUCIBLE только при доступном точном Git object и pinned recipe; unpushed/dirty code проверять отдельно |
| Кеши, projections, embeddings, solver outputs | REPRODUCIBLE только если все входы/recipe действительно доступны; иначе CANONICAL |

После ручного inventory оператор проверяет пропуски: PUBLIC, PRIVATE, WSL и Windows
metadata не считаются автоматически покрытыми одним CORE backup. Переход к реальному
restore требует отдельного согласованного плана: защищённый sandbox, проверенная
независимая копия, immutable originals, бюджет IO/памяти, application-level checks и
отдельный receipt. Этот инструмент не выполняет такой переход сам.

## Целевые тесты

`tests/corpus/test_unique_inventory.py` проверяет budget/plan, fresh SHA при сохранённых
size/mtime, позднюю мутацию, missing/error/mutable originals, hardlinks, реальные
POSIX links/Windows junctions, unsafe paths, duplicate IDs, reproduction cycles,
отсутствие приватных значений в receipt и synthetic restore с неизменным original.
Тест независимого remote topology моделирует только device/domain различие;
полные file reads и hashes в нём настоящие, физический remote backup не утверждается.
Native fixture tests не делают массового scan или corpus processing.
