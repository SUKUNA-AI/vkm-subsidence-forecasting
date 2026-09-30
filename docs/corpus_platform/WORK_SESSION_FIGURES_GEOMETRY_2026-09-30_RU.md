# Рисунки, геометрия и геология — результат рабочей сессии30.09.2026

Исполнены проверка платформы, узкий cleanup, обновление схем/README, локальное чтение рисунков, source extraction B/C, QGIS-мост и тематическое ревью H. Метрическая и семантическая приёмка геометрии, полная хронология и перенос в evidence/WorldSpec остаются открытыми. Следующий массовый OCR — GLM-OCR с проверкой по исходным изображениям, по решению владельца в этой сессии. PhysicalWorld, Ansys/OGS, ML и CAD не запускались.

## Платформа и cleanup

Read-only smoke охватил22 вида инструментов/61 вызов на снимке `snap-20260929T175107Z-574daaac`: поиск, dense/late, граф/NAV, page visual, m0, figure_series и provenance доступны. Полный повторный live sweep C1/N1 и новый retrieval benchmark не выполнялись. EDGE text v3.5 UNAVAILABLE не подменяет канонический CORE mLateOn. В локальном коде исправлена misleading trace-подпись реранкера; на CORE/EDGE исправление не выкладывалось, сервисы и индексы не изменялись. Подробности — [integrity](PLATFORM_INTEGRITY_2026-09-30_RU.md).

Аудит26 cleanup-кандидатов: проверены925 tracked text files и5481 AST imports, references, frozen objects и dependency bridges. Удалён один подтверждённо retired документ отдельным commit `e20ca82`; current-мосты, run-kit и frozen history сохранены. [Manifest](../legacy/LEGACY_REMOVAL_MANIFEST_2026-09-30.md) содержит основания и восстановление из Git. Неподтверждённое legacy не удалялось по одному имени/дате.

## A — исполненное чтение и границы точности

Inventory411 objects:114 прежних адресных source objects,279 native PDF regions и18 DjVu figures. Сохранены оригинальные DOCX parts/relations/drawings, PDF words/paths/paint state/XObjects/masks, source SHA, точные transforms и контекст страницы. На DOCX номер печатной страницы без фиксированного layout UNKNOWN. Найденный EMF колонки фактически содержит bitmap, поэтому native vector по одному расширению не заявляется.

Active jobs:668 CELL,24 LABEL,6243 TILE,411 CLASSIFY. GLM реально прочитал668 ячеек,24 дополнительных label crops и4553 выбранных геометрических/схематических тайла; остальные1690 TILE не выданы за обработанные. Qwen ответы классификации и сравнительных чтений сохранены; после выбора GLM новых Qwen jobs не запускалось. OCR-серверы остановлены, собственный GPU-lock освобождён.

По407 пригодным проверенным gold cells: Qwen350/407=85.995%, GLM344/407=84.521%, reader agreement78.870%.34 source-truncated gold cells исключены заранее; ошибки чтецов и нечитаемое OCR не удаляются из denominator. Цель99% автоматического чтения таблиц не достигнута. GLM parser удаляет только целую output fence, сохраняя буквы, нули и пунктуацию.

ROOT лично проверил150 gold/error-selected cells:134 однозначны,16 имеют неоднозначность кодировки похожих glyphs. Это purposive review, не независимый random benchmark. Ещё150 non-gold units проверены по изображениям:126 кандидатов двух screenshots и24 seeded native-text crops.56 пригодны для literal comparison, GLM42/56=75%; Wilson95% для ошибки15.5–37.7%.94 отклонены как UI, mixed cells, обрезанные или нечитаемые области. Эта условная выборка не оценивает accuracy всех карт корпуса; цель97% map labels не установлена.

Итоговые PRIVATE JSON/CSV сохраняют per-value provenance и статус. Проверенные ранее gold CSV переиспользованы со ссылками на их собственную eye-review; экспорт gold не является новым100% benchmark. Непроверенное значение, скрытый суффикс, неизвестная единица и частичная подпись не становятся числом для геометрии. Очередь Astra содержит exact crop/full image hashes, locator, варианты чтения, вопрос и влияние на WorldSpec; её наличие не означает завершённую научную приёмку.

Coverage ограничен адресной навигацией и расширенным локальным набором; не доказано, что найден каждый релевантный рисунок271 источника. Методы — [figure_readings_v2](../../benchmarks/figure_readings_v2/README.md), агрегаты — [receipt](receipts/figure_readings_v2.json).

## B/C — геометрия источников

Обработано135 source objects. Frozen package409 GeoJSON layers,326 непустых;207165 features:207126 автоматических graphic candidates,20 табличных points с UNKNOWN units и19 явно прочитанных source annotations. Это не207165 подтверждённых выработок/геологических объектов. Исходные координаты независимых frames сохранены без назначения EPSG или метров.

У приоритетных планов сохранены native linework, цветовые компоненты, ROI и overlays. Отдельно извлечены подписи блоков/панели/стволов и полосы колонки; независимая semantic приёмка остаётся в очереди Astra. Внутренние DOCX таблицы252 имеют XML locators/merges и статус PLANNED; план не стал исполнением. Source197 model INS maps не названы наблюдениями или GCP. Source011 не получает автоматически SKRU1 attribution.

Диагностическое pixel-to-pixel similarity использует155 автоматически отобранных соответствий: RMS1.170 target px, conditional LOO1.185 target px. Отбор RANSAC и общий рисунок делают это диагностикой, а не независимой геодезической точностью. Принятой привязки table row→polygon, local metres/UTM и бюджета ошибки в метрах нет; CRS, абсолютные Z и непроверенная временная история UNKNOWN. Поверхности/объёмы и интерполяция не строились.

DXF:409 отдельных source-frame файлов,293417 entities, координатный roundtrip delta0.0. Это приёмка сериализации. Некоторые raster contour candidates имеют некорректные rings: строгий GIS-import обнаруживает их; исходные vertices сохраняются только в помеченном source archive, без MakeValid. Из них нельзя получать принятые площади/overlay/registered geometry. [Метод](../../benchmarks/geometry_skru1_v1/README.md), [receipt](receipts/geometry_skru1_v1.json).

## QGIS и H

`vkm-qgis` —15 узких MCP tools: отдельный PyQGIS/GDAL worker, GeoPackage, features, GCP fit/LOO, transforms, overlay, render/export. Настоящий установленный runtime и stdio MCP проверены:15 tools/13 calls PASS, unit tests32 PASS/2 SKIP. Отсутствие runtime возвращается явным статусом. Source archive содержит341 слой (15 пустых базовых+326 непустых source),207165 features; исходные координаты проходят roundtrip с max JSON representation delta2.27e-13 source units. Frozen GeoJSON manifest неизменён. Topology FAIL:1938 invalid candidates в79 слоях; MakeValid не применялся, downstream render/overlay/transform блокируются. Геометрическая/научная готовность не заявляется. Актуальные hashes — [QGIS receipt](receipts/qgis_mcp_2026-09-30.json), [README](../qgis/README_RU.md).

H:60 исходных меток заморожены до unblind, ROOT проверил31 строку/30 страниц; метки после открытия pooled T1 не подгонялись. Exact agreement39/60=65%, linear κ0.7143, quadratic κ0.843. Обнаружено нарушение полного blind-протокола: агент до freeze видел seed-метки62 тем из другого файла; степень влияния UNKNOWN. Результат описывает согласие, а не полностью независимое слепое качество и не доказательство evidence. [Freeze](../../benchmarks/topic_v1/H_REVIEW_FREEZE_2026-09-30.json), [notes](../../benchmarks/topic_v1/H_REVIEW_NOTES_2026-09-30.md), [result](../../benchmarks/topic_v1/H_REVIEW_RESULT_2026-09-30.json).

## Проверки и размещение

Первый общий WSL прогон:1513 PASS,76 SKIP и1 FAIL из-за рассинхронизации draw.io/spec. Спецификации синхронизированы, failing module затем13/13 PASS; финальная Windows тематическая выборка67/67 PASS. Финальный WSL world+targeted:245 PASS/4 SKIP (OpenCV; эти проверки пройдены в Windows). Полный Windows world прогон имеет8 failures из-за прежних symlink/newline/synthetic-Git предпосылок; они не скрыты изменением тестов. Диагностический повтор с pytest fixtures внутри ignored work дал5 failures из-за родительского Git; повтор с штатным внешним temporary directory прошёл, код тестов не менялся. Основной canonical verifier выполнен в WSL:35 PASS/7 недоступных локальных tagged refs, exit0; ссылки, leakage и catalogue_sync PASS. SKIP live-service tests/неустановленных optional runtimes не названы PASS. [Общая квитанция](receipts/session_verification_2026-09-30.json). Исторические counts Phase1 не являются текущим прогоном.

PUBLIC содержит код, synthetic tests, методы, схемы и санитизированные агрегаты. ROI, legend swatches и координаты пар регистрации вынесены в PRIVATE hash-bound конфигурацию; исполненный код и migration receipt сохранены, frozen source geometry не пересчитывалась. Синтетические geometry/config проверки:17 PASS. Original images, literal values, source coordinates, GeoJSON/GPKG/DXF, raw OCR и full review queue остаются в ignored work/PRIVATE. Пользовательские неотслеживаемые файлы не перезаписывались; PRIVATE checkout, frozen history и main не изменялись.
