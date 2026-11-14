# Локальные CV-компоненты для подготовки ВКМ

Проверки используют синтетическое изображение 256×256, без книг, canonical,
обучения и внешних API. Файлы сохраняются в новом приватном каталоге попытки.
Официальные исходники и checkpoint закрепляются в host-local manifest;
в PUBLIC размещаются только наши команды и проверяющие скрипты.

## SAM 2.1

Проверен официальный `facebookresearch/sam2` commit
`2b90b9f5ceec907a1c18123530e92e794ad901a4`, SAM-2 1.0, Hiera Tiny.
Источник checkpoint — официальный registry `facebook/sam2.1-hiera-tiny`;
его revision, URL, размер и вычисленный SHA сохраняет приватный manifest.
Лицензия SAM 2 — Apache-2.0. Требования и особенности установки:
[upstream INSTALL](https://github.com/facebookresearch/sam2/blob/2b90b9f5ceec907a1c18123530e92e794ad901a4/INSTALL.md).

Исправный PyTorch выбран после реальной проверки GPU. Отдельный overlay
может читать совместимые пакеты существующего Python через собственный `.pth`;
родительская среда остаётся неизменной. Зафиксировать путь и версии родителя,
иначе такой overlay нельзя считать самодостаточным.

```bash
uv venv --python "$EXISTING_TORCH_PYTHON" "$SAM_ENV"
# Добавить в собственный SAM_ENV/site-packages .pth с проверенным parent site-packages.
SAM2_BUILD_CUDA=0 uv pip install --python "$SAM_ENV/bin/python" --no-deps --no-build-isolation "$PINNED_SAM_SOURCE"
# Установить только отсутствующие hydra-core==1.3.2 и iopath==0.1.10.
"$SAM_ENV/bin/python" scripts/env_setup/cv_smoke.py sam \
  --checkpoint "$OFFICIAL_CHECKPOINT" --gpu-lock "$SHARED_GPU_LOCK" --out "$NEW_PRIVATE_OUTPUT"
```

Проверяются настоящий CUDA forward, маска, включение prompt-точки,
overlay и устройство/VRAM. `SAM2_BUILD_CUDA=0` означает отсутствие optional
расширения удаления мелких отверстий/островков; post-processing в этом smoke
явно выключен. Это ограничение не скрывается за успешным inference.

## X-AnyLabeling

Использован `CVHub520/X-AnyLabeling` commit
`12404361ee272d54fa412497699a52068fc17a58`, версия 4.3.2, GPL-3.0-only.
Qt/ONNX runtime изолирован от QGIS. Пакет из проверенного checkout:

```powershell
python -m venv "$env:XANY_ENV"
& "$env:XANY_ENV/Scripts/python.exe" -m pip install "$env:PINNED_XANY_SOURCE[cpu]"
& "$env:XANY_ENV/Scripts/python.exe" scripts/env_setup/cv_smoke.py xany `
  --encoder "$env:OFFICIAL_SAM_ONNX_ENCODER" --decoder "$env:OFFICIAL_SAM_ONNX_DECODER" --out "$env:NEW_PRIVATE_OUTPUT"
& "$env:XANY_ENV/Scripts/python.exe" -m anylabeling.app --no-auto-update-check `
  --work-dir "$env:PRIVATE_XANY_CONFIG" --filename "$env:SYNTHETIC_ANNOTATION_JSON"
```

Два ONNX файла берутся из официального релиза X-AnyLabeling v2.5.0,
указанного в pinned `sam2_hiera_tiny.yaml`. Они не подменяются PyTorch `.pt`.
Smoke сохраняет и читает аннотацию штатным LabelFile и выполняет штатный
SegmentAnything2ONNX backend на CPU. Открытие GUI подтверждается отдельно;
выбор модели мышью в GUI не следует выдавать за проверенный по одному import.

## UIUC и Uncharted

`uiuc_component_smoke.py` проверяет оригинальный legend-conditioned polygon
UNet из `xiyuez2/Darpa_Unet_Release`, pin
`7f6f6450108e43df83a1db2a51b6c7b893c65f11`. Checkpoint
`jaccard_index_value=0.9229.ckpt`: 372605901 B,
SHA-256 `453a711c315ff456965d8bb383c07dd5b0038a3d50615b8dfe5bc55f48160973`.
Исходная архитектура и state_dict загружаются строго; `weights_only=True`.
Результат — повторно читаемый float TIFF, NPY, mask и overlay. Это standalone
компонент UIUC, без полного pipeline, YOLO detector и Slurm. Интерфейс выбранной
модели не предусматривает TA1/GPKG export. Лицензия release repo/весов явно
не объявлена; исходники и веса не включаются в PUBLIC и не переиздаются.

Uncharted pin `7a50dc0cecde38b6c5db9c8f7d01b3e628bcf8b9`, MIT.
`cv_smoke.py critical` исполняет локальные rank/denoise/union и UIUC unpatch
на синтетике; exit 2 явно сообщает, что это не нейросетевой pipeline PASS.
Полный Uncharted forward требует официальные `config.yaml/config.json/model_final.pth`
и отдельный legacy Detectron2 runtime. Если документированные S3/registry
недоступны, сохраняется точный blocker; Google Vision/OpenAI/CDR и платные
модули не включаются. Второй GIS/CAD MCP и новая агентская платформа не нужны.

Повтор использует прежние среды и веса, новую папку результатов и общую
GPU-блокировку. Откат затрагивает только собственный overlay/launcher;
родительский Torch, QGIS, драйвер, CAD и чужие кэши не удаляются.
