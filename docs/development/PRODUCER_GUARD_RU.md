# Production producer profile

`vkm-corpus run plan|extract --profile production` — отдельный fail-closed профиль.
Без этого флага сохраняется exploratory режим для исследований и синтетических тестов;
его `producer_identity.verified=false` не является production admission.

Параметры production:

- `--expected-commit` — полный SHA commit checkout;
- `--dependency-lock` — дополнительные tracked exact lock-файлы в `requirements/` (повторяемый);
- `--memory-budget-gb`, `--memory-reserve-gb`, `--source-memory-gb`, `--min-free-disk-gb` — GiB;
- `--workers` — желаемая верхняя граница CPU concurrency;
- `--confirm-plan` — обязательный SHA свежего идентичного плана при `extract`.

В `PipelineConfig` соответствуют поля `profile`, `expected_commit`, `dependency_locks`,
`memory_budget_gb`, `memory_reserve_gb`, `source_memory_gb`, `min_free_disk_gb`, `workers`.
Базовый `requirements/corpus.lock.txt` обязателен; при GPU layout дополнительно обязателен
`requirements/corpus-layout.lock.txt`. Дополнительный lock не отменяет эти требования.
Каждый pin сверяется с установленной distribution metadata; отсутствующий пакет,
другая версия, несовместимые pins или неполный pin блокируют production.
Проверка не устанавливает зависимости и не загружает модели.

Git-команды проверяются по коду возврата. HEAD должен совпадать с ожидаемым commit.
Tracked и untracked изменения `src/`, `scripts/`, `infra/`, `pyproject.toml`,
`requirements/`, `setup.cfg`, `setup.py` запрещены. Игнорируемые исполняемые файлы,
включая старые `.pyc`, также запрещены: нужен чистый checkout, запуск с отключённой
записью bytecode. Переменные перенаправления Git не наследуются.

До чтения кешей plan проверяет SHA-256 и размер каждого ACTIVE источника заново;
путь должен находиться внутри resources root после разрешения symlink. Изменение файла
в ходе проверки блокируется. Plan SHA включает проверенные source identities, полный commit,
параметры, hashes lock-файлов и фактические версии зависимостей. В `RUN_CONFIG` и плане
сохраняется `producer_identity` с `identity_sha256`; resource guard записывает измеренный
запас и effective concurrency. Изменение доступной RAM само по себе не меняет plan SHA.

Перед каждым worker повторно проверяются identity кода/окружения и source bytes.
После подтверждения сохраняется `approved_plan.json`; child до открытия кешей сверяет
его identities. Изменение файла вместе с SHA в registry не подменяет ранее подтверждённый
набор входов. Флаг `--no-ocr` также входит в plan hash.
Этот механизм требует неизменяемых оригиналов и checkout на время операции; это не
атомарная файловая snapshot-транзакция. Output-affecting изменения алгоритмов по-прежнему
требуют осознанного обновления extraction generation/rule: каждый code-only commit
не означает автоматического пересчёта всех моделей и caches.

CPU concurrency ограничивается `floor((memory_budget_gb-memory_reserve_gb)/source_memory_gb)`.
Проверяется текущая доступная память с учётом cgroup и свободный диск staging.
Каждый worker получает проверенный `RLIMIT_AS` в свежем процессе до импорта pipeline;
ошибка установки лимита прерывает операцию, не допускает продолжения без ограничения.
Это лимит виртуального адресного пространства worker; coordinator reserve — планирование,
а не общий cgroup hard limit процесса. Для GPU layout и тяжёлых библиотек величины требуют
отдельной квалификации. Production enforcement сейчас допускает Linux; Windows production
явно отклоняется до отдельной реализации Job Object/cgroup-equivalent. Exploratory Windows
тесты остаются доступными. `PARTIAL` production возвращает ненулевой код.

`vkm-corpus registry verify` всегда проверяет свежие bytes ACTIVE записей и возвращает
ненулевой код при любой непроверенной ACTIVE записи. RETIRED/ABSENT_BY_REGISTER не открываются
и перечисляются отдельно; успешная сверка с такими пропусками имеет `PASS_WITH_SKIPPED`.
Статистический SHA cache доступен только явным `fresh=False` внутреннего Python API и
не является production доказательством неизменности файла.

Синтетическая квалификация: `tests/corpus/test_producer_guard.py`, существующие
`test_pipeline_cli.py`, `test_pipeline_cache.py`, `test_registry_import.py`.
Ни один из этих тестов не открывает реальный PRIVATE корпус и не запускает GPU/OCR.

Read-only integration API: `vkm_corpus.pipeline.context.producer_identity(cfg)` возвращает
`code_revision`, `code_dirty`, `profile`, `verified`, `dependencies`, `config`, `identity_sha256`;
`resource_guard(cfg)` проверяет runtime prerequisites отдельно. При отказе выбрасывается
`ProducerGuardError`. Эти функции не создают run, не пишут файлы и не исполняют pipeline.
