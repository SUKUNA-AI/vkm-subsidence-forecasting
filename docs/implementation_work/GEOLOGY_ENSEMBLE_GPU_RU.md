# Ансамбль неопределённости инженерной геологии v6_2 (GPU)

Дата: 23.10.2026. Объект — принятая модель v6_2 (FULL_CONTEXT_PARTIAL, geometry_version `887a7f33…c9dc7`), её построение описано в [ENGINEERING_GEOLOGY_BUILD_RU.md](ENGINEERING_GEOLOGY_BUILD_RU.md). Реальные входы и результаты лежат в PRIVATE (`12_work_exports/geology_ensemble_gpu_20261023/`). Здесь — метод, код и команды.

Статус всего ансамбля: синтетические реализации (DERIVATION). Это не наблюдения. Проверка покрытия на отбивках корпуса — проверка согласованности модели неопределённости, а не полевая валидация.

## Модель (MODEL_CHOICE, предрегистрирована до подсчёта)

- Шесть независимых латентных гауссовских полей:
  - кровля ТКТ — `ln(глубина ниже устья)`;
  - пять мощностей интервалов ТКТ→СМТ→ПКС→СКЗ→СЗ→ПдКС — обрезанный гауссовский закон `H = max(Y, 0)`.
- Нулевая мощность — событие `Y ≤ 0`. Наблюдённое отсутствие ПКС у скв. 139 — цензурированное наблюдение. Порядок контактов выполняется по построению.
- Отметка контакта = DSM(x) − глубина. Это параметризация SURFACE модели v6_2. Устье — то же, что в v6_2.
- Ковариация: экспоненциальная, сферическая или гауссова; практический радиус, доля самородка и порог.
  - Среднее интегрируется (плоский априор), порог — по Джеффрису.
  - Радиус и самородок задаются на объявленной сетке, каждая реализация берёт свои параметры из апостериорного распределения.
  - Самородок — мелкомасштабная изменчивость, а не ошибка отбивки: точечные отбивки воспроизводятся точно.
- Тип ковариации для каждого поля выбирается по наибольшему маргинальному правдоподобию. Правило применяется внутри каждого fold и один раз на всех опорах для итогового ансамбля.
- Ограничения «не вскрыто» не навязываются. Для них считается доля реализаций, которые их нарушают.
- Компоненты неопределённости устьев, явно и раздельно:
  - G — устье = DSM, как в v6_2;
  - P90 / P200 — плановая привязка как сдвиг положения скважины в круге 90 / 200 м (глубины ниже DSM сохраняются), плюс озёрное устье скв. 74;
  - V — общий вертикальный сценарий ±10 м, не моделируется, указывается рядом с полосами.

Полная предрегистрация с поправками A1 и A2 (до полного подсчёта) лежит в PRIVATE-поставке.

## Код

- `src/vkm_world/geometry_build/gp_ensemble.py`:
  - ковариации, апостериор на сетке параметров (в том числе с положениями опор, своими для каждой реализации);
  - цензурирование (Gibbs);
  - кригинг-предсказание, слоистые отбивки, CRPS, интервал Уилсона;
  - условное моделирование на узлах (backend NumPy или torch/CUDA), квантили, P1-оператор выборки на TIN.
- `scripts/build_geology_ensemble.py` — стадии `cv` (LOO по физическим скважинам и блоки 1,5 км с метками v6_2) и `grid` (ансамбль на 19 128 узлах, полосы на разрезах и ПЛ 12, проверки, сравнение с v6_2, квитанция).
- `scripts/plot_geology_ensemble.py` — карты с масштабом и легендой, разрезы, ПЛ 12, калибровка.
- `scripts/export_ensemble_gpkg.py` — GeoPackage в локальной SH1 без EPSG. Запускается в Python QGIS/GDAL.
- Синтетические тесты — `tests/world/test_gp_ensemble.py`. Покрыты: воспроизведение данных, цензура, порядок, покрытие LOO на синтетике, CRPS против формулы, совпадение backend'ов, оператор TIN.

## Команды

```powershell
python scripts/build_geology_ensemble.py --model-dir <извлечённый engineering_v6_2> --observations <PRIVATE>/12_work_exports/geology_misis_20261023/model/inputs/observations_model_input_v6_2.json --previous-scene work/skru1_linked_world_20261016/delivery_complete/data/scene.js --sections <PRIVATE>/12_work_exports/geology_misis_20261023/graphics/data/sections_xyz.json --v62-cv <PRIVATE>/12_work_exports/geology_misis_20261023/model/v6_2_summary/FULL_CONTEXT_PARTIAL/cross_validation.json --prereg <PREREGISTRATION.json> --output work/geology_ensemble/cv --stage cv
python scripts/build_geology_ensemble.py <те же входы> --output work/geology_ensemble/grid --stage grid
python scripts/plot_geology_ensemble.py --grid work/geology_ensemble/grid --cv work/geology_ensemble/cv --model-dir <engineering_v6_2> --output work/geology_ensemble/figures
<QGIS python> scripts/export_ensemble_gpkg.py --grid work/geology_ensemble/grid --cv work/geology_ensemble/cv --model-dir <engineering_v6_2> --output work/geology_ensemble/gis --epoch 2026-10-23T00:00:00.000Z
```

Сиды выводятся из мастер-сида 20261023 через `numpy.random.SeedSequence`. Квитанции фиксируют:

- хеши входов и выходов;
- версии python, numpy, scipy, torch, CUDA;
- время GPU и пик памяти;
- использованную подстройку Холецкого.

Память GPU ограничивается параметром `--gpu-memory-gb`, по умолчанию 10 ГБ.
