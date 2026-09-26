#!/usr/bin/env bash
# Переписать абсолютные пути облачной VM в файлах run_kit на локальные.
# Использование (Git Bash / WSL / Linux):
#   PUB=/e/Диплом/vkm-subsidence-forecasting RES=/e/Диплом/vkm-subsidence-forecasting_resourses \
#   WORK=/e/Диплом/work VENV=/e/Диплом/.venv-research bash localize_paths.sh
# Порядок замен важен: путь resources содержит путь public как префикс.
set -euo pipefail
: "${PUB:?}"; : "${RES:?}"; : "${WORK:?}"; : "${VENV:?}"
cd "$(dirname "$0")"
for f in SWEEP_PROTOCOL.md MATH_PROTOCOL.md tools/*.py workflows/*.js; do
  sed -i \
    -e "s#/home/user/vkm-subsidence-forecasting_resourses#${RES}#g" \
    -e "s#/home/user/vkm-subsidence-forecasting#${PUB}#g" \
    -e "s#/home/user/\.venv-research#${VENV}#g" \
    -e "s#/home/user/work#${WORK}#g" "$f"
done
grep -rn "/home/user" --exclude=localize_paths.sh . || echo "OK: облачных путей не осталось"
