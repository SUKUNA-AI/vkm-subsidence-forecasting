# Закреплённый рендер DOCX → PDF (LibreOffice 24.2.x)

Страницы `r` источника VKM-SRC-023 — это страницы закреплённого рендера (CP-08, H-50). Образ собирается один раз.
Рендер кешируется в STAGING-корне как артефакт `DOCX_RENDERED_PDF`. PDF LibreOffice содержит дату создания, поэтому
каждый новый рендер даёт новые байты и повторять его не нужно.

```text
docker build -t vkm-libreoffice:24.2 infra/workstation/libreoffice
vkm-corpus run render-docx --source VKM-SRC-023      # хост с Docker; VKM_DATA_ROOT = STAGING-корень
```

## Профиль

| Параметр | Значение |
|---|---|
| Базовый образ | `ubuntu:24.04` по digest (в Dockerfile) |
| LibreOffice | 24.2.7.2 из noble-updates |
| Шрифты | DejaVu, Liberation, OpenSymbol — 25 файлов, sha256 списка `190bd13b…` |
| Запуск | `--network none` |
| Id образа при сборке 28.09.2026 | `sha256:d64278f32cb7…` |

Профиль (версия, id образа, хеш списка шрифтов) пишется в `documents.pagination_render_profile` и в recipe
артефакта.

## Сверка с Phase 1

Рендер даёт 117 страниц. Тексты страниц 1–43 побайтно равны манифесту Phase 1. Расхождение начинается со страницы с
первыми OMML-формулами. Phase 1 насчитала 115 страниц на LibreOffice 24.2.7.2 с неизвестным набором шрифтов VM.
Поэтому привязка evidence 023 к страницам остаётся зависимой от рендера (`RENDER_DEPENDENT`).
