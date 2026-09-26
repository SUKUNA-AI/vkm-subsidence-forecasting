# Политика данных и путей

1. **Два репозитория.**
   - PRIVATE `vkm-subsidence-forecasting_resourses` хранит зарегистрированные научные бинарники
     (`00_registry/SOURCE_REGISTER.csv`, SHA-256), OCR и text-извлечения, полные evidence-записи
     с цитатами.
   - PUBLIC хранит код, схемы, public-safe каталоги (без колонок `quote`/`ocr_text`), документы,
     тесты, receipts.
2. **Запрет утечки.** В PUBLIC нельзя класть PDF/DjVu/DOCX/архивы корпуса, дословные цитаты,
   OCR-дампы и приватные абсолютные пути. Проверки:
   - `vkm_world.governance.leakage.scan` (тест `tests/world/test_foundation.py::test_public_tree_has_no_private_leakage`) —
     бинарники, колонки и ключи JSON с цитатами, машинные пути;
   - группа `catalogue_sync` верификатора (при заданном `VKM_RESOURCES_ROOT`): PUBLIC-каталоги равны свежей сборке
     из PRIVATE, и ни один PUBLIC-текст не повторяет ≥ 25 слов прозы цитаты PRIVATE подряд. Сборщик public-каталогов
     применяет то же правило и сокращает такие ячейки.

   Пути публикации: `scripts/build_public_catalogues.py`, `scripts/build_evidence_from_legacy.py`,
   `docs/reset_2026_09/run_kit/tools/publish_reports.py`.
3. **Пути.**
   - Рабочие пути в PUBLIC относительны корню репозитория.
   - PRIVATE находится через `VKM_RESOURCES_ROOT`.
   - Запрещено зашивать домашние каталоги, буквы дисков, `/mnt/data`, пути cloud VM в исполняемый
     код и конфиги.
   - Абсолютные пути машины (домашние каталоги, временные каталоги агентов, диски Windows) не допускаются ни в коде,
     ни в каталогах, ни в документах. При публикации их заменяет `sanitize_paths` на логические имена:
     `$VKM_RESOURCES_ROOT`, `$VKM_WORK`, `PRIVATE 11_evidence_vnext/...`.
   - Исключение — только данные и логи исторического run kit в `docs/reset_2026_09/run_kit/`
     (протоколы, `chunk_plan.json`, workflow-скрипты). Код run kit (`*.py`) не исключается: инструменты
     читают корни из окружения (`VKM_PUB`, `VKM_RESOURCES_ROOT`, `VKM_WORK`, модуль `tools/_roots.py`).
     `localize_paths.sh` копирует протоколы в `$VKM_WORK/run` с подстановкой локальных путей и не меняет
     файлы в репозитории.
4. **Временные файлы** — только `work/` (git-ignored) или системный temp.
5. **Git LFS.** Бинарные научные файлы PRIVATE хранятся в LFS. Если окружение не может загружать
   LFS (как cloud-окружение 26.09.2026: `lfs.github.com` → 403), крупные бинарники не создаются.
   Текстовые артефакты коммитятся как обычные файлы.
6. **Retired.**
   - `SKRU1_ACTUAL_DATA_TABLES_v1/**`, `inputs/bootstrap/**`, v3.x reconstructed/model-ready пакеты —
     LEGACY_RETIRED; повторно не импортируются.
   - Состояние старой архитектуры PUBLIC доступно в ветке `legacy` (`d54025d`).
7. **Frozen-релизы** (v2.1, representation R1, Gate B3) не переписываются. В новом `main` они
   упоминаются по SHA/manifest; воспроизведение выполняется из `legacy`/исторических commit.
8. **Каждое преобразование оставляет receipt**: что, из чего, какой командой, SHA входов и выходов.
9. **Сгенерированные файлы детерминированы** (сортировка, отсутствие временных меток внутри хешируемых
   артефактов). Пример: `scripts/export_worldspec_schema.py`.
