# Чтение рисунков v2

Прикладной конвейер исходных DOCX/PDF/DjVu objects: source SHA, оригинальные вложения и paint state, полный контекст страницы, точные crop transforms, ячейки/подписи и перекрывающиеся тайлы. Все значения, изображения, таблицы и raw model responses остаются в git-ignored `work/` или PRIVATE.

Автоматическое чтение имеет статус `AUTO_EXTRACTED_UNREVIEWED`. Согласие чтецов не является точностью; точность измеряется отдельно по проверенным CSV рис. 14–18. Обрезанные значения не восстанавливаются. Семантическая атрибуция и привязка требуют проверки.

Исполненный inventory —411 source objects:114 исходных объектов прежнего адресного набора,279 native PDF regions и18 DjVu figures. Это результаты адресной навигации, а не доказательство полного покрытия всех релевантных рисунков271 источника. Модельная атрибуция рудника не принимается автоматически. DOCX printed-page numbers остаются UNKNOWN без закреплённого layout; сохранены оригинальные part/paragraph/XML locators.

Массовое OCR далее выполняет **GLM-OCR** по решению владельца30.09.2026. Qwen3.5-9B больше не запускается для этого потока; неизменные ответы сравнительного прогона сохранены. Тяжёлые серверы сериализованы, модель/образ/revision закреплены в [compose](../chart_models_v1/serving/compose.yml), зависимости и веса не скачиваются.

Подготовка и повторное использование готового набора:

```bash
python -B benchmarks/figure_readings_v2/prepare.py --work work/figure_readings_2026-09-29/v2 --resources "$VKM_RESOURCES_ROOT"
# Добавление новых navigation hits без перезаписи прежних jobs/raw:
python -B benchmarks/figure_readings_v2/prepare.py --append --work work/figure_readings_2026-09-29/v2 --resources "$VKM_RESOURCES_ROOT"
python -B benchmarks/figure_readings_v2/enrich_context.py --work work/figure_readings_2026-09-29/v2 --resources "$VKM_RESOURCES_ROOT"
# DjVu: существующий WSL runtime с DjVuLibre, только адресные страницы:
python -B benchmarks/figure_readings_v2/prepare_djvu.py --work work/figure_readings_2026-09-29/v2 --resources "$VKM_RESOURCES_ROOT"
```

Первичная подготовка переиспользует прежний `jobs.json` и PRIVATE `inventory_search_raw*.json`. `--append` сохраняет старые crops/jobs. Замороженные входы не перезаписывать; для новой обработки создавать новый work-каталог/версию. PDF render600dpi ограничен объявленным pixel budget; actual effective DPI записан. Original raster upscale Lanczos не добавляет информации. TILE640px/stride512px означает20% overlap.

Локальное чтение запускается только после атомарного получения общей `work/gpu.lock` с owner `GPT-FIG-V2`. `read_local.py` требует совпадения owner; сервер GLM из compose должен быть готов. URL/model передаются явно; endpoint не является tracked конфигурацией.

```bash
python -B benchmarks/figure_readings_v2/read_local.py --work work/figure_readings_2026-09-29/v2 --reader glm --url "$VKM_OCR_URL" --model glm-ocr --kind CELL
python -B benchmarks/figure_readings_v2/select_jobs.py --work work/figure_readings_2026-09-29/v2 --reader glm --url "$VKM_OCR_URL" --model glm-ocr
python -B benchmarks/figure_readings_v2/evaluate.py --work work/figure_readings_2026-09-29/v2 --gold work/figure_readings_2026-09-29/claude
python -B benchmarks/figure_readings_v2/assemble.py --work work/figure_readings_2026-09-29/v2
python -B -m pytest -q tests/corpus/test_figure_readings_v2.py
```

Серверы после прогона остановить и освободить только собственный GPU-lock. Raw responses неизменны и возобновляются только при совпадении image/prompt/request hashes. `finish_reason=length` не считается читаемым результатом. GLM parser удаляет одну полную Markdown fence, не заменяет буквы, запятые, нули или внутренние пробелы.

На407 пригодных gold cells Qwen350/407=85.995%, GLM344/407=84.521%;34 обрезанные gold cells исключены заранее. Нечитаемое OCR остаётся ошибкой в denominator. Reader agreement78.870% считается отдельно. Табличная цель99% автоматическими чтецами **не достигнута**, цель97% для всех map labels **не установлена**.

ROOT проверил150 gold/error-selected cells (134 однозначны,16 неоднозначны по кодировке похожих glyphs). Отдельно проверены150 non-gold units:126 кандидатов из двух screenshots и24 seeded native-text crops. Из них56 пригодны для literal comparison; GLM42/56=75%, error Wilson95%15.5–37.7%. Остальные94 — UI, mixed/partial или unreadable. Это условная смешанная выборка, не corpus-wide random accuracy; нельзя переносить75% или accuracy gold tables на все карты. Проверенные ранее CSV переиспользуются с их собственным provenance; их экспорт не считается новым независимым100% benchmark.

`assemble.py` выпускает PRIVATE `figures/*.json`, `tables/*.csv`, `labels/*.json`, per-value status, immutable raw references, output manifest и очередь `ASTRA_REVIEW_REQUIRED`. Native text и модельные bbox сохраняются отдельно; invalid model bbox не получает выдуманных координат. GLM text fragments имеют точный crop locator, но не притворяются word-level spatial annotations. Значения для geometric/evidence use требуют отдельной source/scope/unit/semantic проверки. Source ellipses, viewport clipping, UI и смешанные клетки нельзя преобразовывать в полное число.

Квитанция — [figure_readings_v2.json](../../docs/corpus_platform/receipts/figure_readings_v2.json); итог и ограничения — [отчёт](../../docs/corpus_platform/WORK_SESSION_FIGURES_GEOMETRY_2026-09-30_RU.md).
