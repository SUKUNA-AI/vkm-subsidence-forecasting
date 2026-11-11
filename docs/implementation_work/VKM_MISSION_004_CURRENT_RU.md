# VKM-004-DEEP. Действующая поставка всего СКРУ-1

11.11.2026 завершена условная связанная история всего поля СКРУ-1: 37 численных интервалов Named37, 36 интерпретативных геологических интервалов, 575 участков, C1–C5. Поля равновесия, память и вода вычислялись совместно. Конец 2024 года соответствует границе **2025.0**, при исходном условном состоянии 1933 года. Полная научная приёмка остаётся открытой; численное исполнение отделено от допуска геологии и физической траектории.

[VKM-CW-SPEC-001 v1.0](../specifications/VKM_COUPLED_WORLD_SPEC_RU.md) сохраняет исходный статус **CANDIDATE_TO_ADOPT** и дату 05.11.2026. Публикуется текст кандидата; производные исходного пакета из приложения E сюда не включаются. Используется установленный Agent Harness и его действующие схемы. Исходный общий бюджет 36 000 секунд при продолжении не обнулялся.

| Найти | Точный вход внутри PRIVATE-поставки |
| --- | --- |
| Актуальная C1 | `vkm-004://02_geology/depth_constrained_candidate/attempt04_native_gmsh/AREA_MEAN_PROXY/wholemine_depth_conditional.npz`, SHA-256 `4166e2ddb2a7a971a42f1f2b77dbe4de1b5d40a2a977dd960d67aa7a94ff0194`; рядом `interval_lineage.json` и `geometry_receipt.json` |
| Действующий мир и расчёт | World `VKM004-DEPTH-AREA-MEAN-P0-VIRGIN1933-C4-H0`, run `VKM004-DEPTH-NATIVE-HISTORY-001`. Драйвер `vkm-004://06_coupling/source_driven_history/depth_variant/run_native_depth_history.py`; `native_full_01/producer.json` хранит фактические argv и executed-source SHA; `native_full_01/receipt.json` — конечный результат |
| Поля и C1–C5 | `vkm-004://06_coupling/source_driven_history/depth_variant/native_terminal_fields_01/field_schema.json`, `accepted_terminal_fields.npz` и `active_input_projection.json`; источниковые связи — `active_source_joins_v1.json` в родительском каталоге |
| История, память, дополнительные проверки | `vkm-004://06_coupling/source_driven_history/depth_variant/AUTHOR_FINAL_v3.json` и `HANDOFF_FINAL_RU.md`. Основная история, адресная проверка шага времени и причинный C4-контроль имеют разные run/world IDs и горизонты |
| Нативные механические ограничения | `vkm-004://07_mechanics/ansys/depth_H_native_geometry/RESULT_RU.md` и `tet_representation_02/`. Отказ формы WEDGE и точностное расхождение TET сохранены. Выполненные APDL Math GPU-решения не объявляются допуском нативной FEM-сетки |
| Новая ВКР | `vkm-004://11_visual_thesis/diploma_v12/Коряев_ВКР_Геология_20261111_v12.docx` и PDF, **88 страниц**; записка руководителю — 3 страницы. Точные SHA, Word export/reopen — `FINAL_DOCUMENT_MANIFEST.json`; независимое ревью — `vkm-004://12_final/visual_review/v12_final/FINAL_REVIEW.json` |
| Открываемая модель | `vkm-004://12_final/model_portability_v4/viewer_bundle_v4/OPEN_RU.md`: относительные проекты QGIS, ParaView, Civil 3D, шесть расчётных эпох и происхождение полей; portable receipt находится в родительском каталоге |
| Сервисы и текущие запросы | [Научный читатель существующего API](../../src/vkm_corpus/api/scientific.py), `vkm-004://10_services/final_generation/FINAL_PRODUCER_FREEZE.json` и итоговые readback receipts. Рабочий документный CORE и отдельный научный staging имеют разные роли. ProductDB и `pysgru` **NOT_FOUND**, сходное имя библиотеки не является подтверждённым alias |
| Все действия и приёмка | `vkm-004://delivery_ledger.json` — 101 индивидуальный статус; `vkm-004://12_final/FINAL_REPORT_RU.md` — результаты и открытые вопросы. Фактические 31 результата установленного harness поставляются отдельным companion `HARNESS_FINAL_STATUS.json`, без изменения замороженных scientific files |

Логический префикс `vkm-004://` разрешается относительно корня восстановленной поставки. [Полные закрытые артефакты, DOCX/PDF и малый portable viewer](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting_resourses/releases/tag/vkm-004-deep-20261111) хранятся в PRIVATE. Абсолютные пути станции, закрытая геометрия, оригиналы и raw-результаты в PUBLIC не включаются.

Конструкция использует ограничения из корпуса, но пространственная опора ряда глубин и условные координаты скважин не установлены как измеренные. Локальные неподтверждённые искривления контактов и расхождение независимого нативного представления раскрыты в главе и технических receipts. Внутренняя сеточная сходимость поверхности не является доказательством геологической точности или напряжений целиков. Мгновенный энергетический обмен событий C4 и натурное сопоставление остаются открытыми.

После последнего пространственно заданного события 2012,5 года продолжается условная релаксация. Фактические поздние выработки не заменены вымышленным календарём. Число GPU-решений, материальных точек и вариантов геологии учитывается раздельно; оно не является числом независимых валидированных миров.

Прежняя SOURCEPIN-геометрия `vkm-004://02_geology/sourcepin_repair/` и её короткие истории — исторические ветви, а не текущая Hmean. Старая `source_driven_history/full_01` сохраняет остановку 2012,5 PRE_EVENT. [Named37 от 10.11.2026](VKM_NATIVE_MISSION_002_NAMED37_RU.md) и [его PRIVATE Release](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting_resourses/releases/tag/skru1-native-named37-20261110) сохранены; A18 Exact **20/30** не заменяется поздними 9/9, 9/9 и 6/6 другого scope.

Архивные checkpoint открываются через `vkm-004://12_final/source_replay_review/live_context_guard_v2/REPLAY_RU.md` с точными frozen sources. Защита матриц, C2 и истории сохраняется; новая версия читателя не переписывает прежние расчёты.

[World-0](../../src/vkm_world/representations/world0/) и [теория v0.2](../theory/README.md) развиваются в своих версиях. Прогнозный алгоритм, нейросети и ML-пилот в миссии не выполнялись.
