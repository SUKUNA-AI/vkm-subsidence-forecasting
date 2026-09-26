#!/usr/bin/env bash
# Локализованные копии run kit облачного прогона для другой машины.
# Отслеживаемые git файлы НЕ изменяются: копии пишутся в $WORK/run/ (раскладка, которую ожидают протоколы
# и workflows: $WORK/run/{SWEEP_PROTOCOL.md,MATH_PROTOCOL.md,tools/,workflows/}), и пути VM переписываются
# только в копиях. Дополнительно пишется $WORK/run/roots.env (VKM_PUB, VKM_RESOURCES_ROOT, VKM_WORK):
# tools/_roots.py читает его, если переменные окружения не заданы.
#
# Значения облачного прогона 26.09.2026 (что заменяется; в коде tools/*.py этих путей нет):
#   PUB  = /home/user/vkm-subsidence-forecasting
#   RES  = /home/user/vkm-subsidence-forecasting_resourses
#   WORK = /home/user/work
#   VENV = /home/user/.venv-research
#
# Использование (Git Bash / WSL / Linux):
#   PUB=/e/Диплом/vkm-subsidence-forecasting RES=/e/Диплом/vkm-subsidence-forecasting_resourses \
#   WORK=/e/Диплом/work VENV=/e/Диплом/.venv-research bash localize_paths.sh
# Порядок замен важен: путь resources содержит путь public как префикс.
# Значения с символами #, & и \ экранируются.
set -euo pipefail
: "${PUB:?}"; : "${RES:?}"; : "${WORK:?}"; : "${VENV:?}"
KIT="$(cd "$(dirname "$0")" && pwd)"
OUT="$WORK/run"
cd "$KIT"
files=(SWEEP_PROTOCOL.md MATH_PROTOCOL.md tools/*.py workflows/*.js)
mkdir -p "$OUT/tools" "$OUT/workflows"
esc() { printf '%s' "$1" | sed -e 's/[\\&#]/\\&/g'; }   # экранирование для правой части s###
copies=()
for f in "${files[@]}"; do
  cp "$f" "$OUT/$f"
  # две фазы: сначала облачные пути -> метки, затем метки -> значения (значение не переписывается повторно)
  sed -i \
    -e "s#/home/user/vkm-subsidence-forecasting_resourses#@@VKM_RES@@#g" \
    -e "s#/home/user/vkm-subsidence-forecasting#@@VKM_PUB@@#g" \
    -e "s#/home/user/\.venv-research#@@VKM_VENV@@#g" \
    -e "s#/home/user/work#@@VKM_WORK@@#g" \
    -e "s#@@VKM_RES@@#$(esc "$RES")#g" \
    -e "s#@@VKM_PUB@@#$(esc "$PUB")#g" \
    -e "s#@@VKM_VENV@@#$(esc "$VENV")#g" \
    -e "s#@@VKM_WORK@@#$(esc "$WORK")#g" "$OUT/$f"
  copies+=("$OUT/$f")
done
cat > "$OUT/roots.env" <<ENV
export VKM_PUB="${PUB}"
export VKM_RESOURCES_ROOT="${RES}"
export VKM_WORK="${WORK}"
ENV
# Самопроверка только по обработанным копиям (не по журналам run kit).
if grep -n "@@VKM_" "${copies[@]}"; then
  echo "ERROR: в копиях выше остались необработанные метки" >&2
  exit 1
fi
case "${PUB}${RES}${WORK}${VENV}" in
  */home/user*) ;;   # локальные значения сами содержат /home/user: проверка по этому префиксу невозможна
  *) if grep -n "/home/user" "${copies[@]}"; then
       echo "ERROR: в копиях выше остались пути облачной VM" >&2
       exit 1
     fi ;;
esac
echo "OK: ${#copies[@]} локализованных копий в $OUT; roots.env записан (source $OUT/roots.env)"
