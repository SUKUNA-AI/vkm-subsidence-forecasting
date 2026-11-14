#!/usr/bin/env bash
# Isolated WSL/Linux Python 3.12 data + CPU GemPy environments.
# Usage: geo_install.sh /path/to/python3.12 /path/to/pinned/ta1-geopackage /new/private/base /private/receipts
set -euo pipefail
if [[ $# != 4 ]]; then
  echo 'Expected: PYTHON312 TA1_SOURCE PRIVATE_BASE PRIVATE_RECEIPTS' >&2
  exit 2
fi
python312=$(realpath "$1")
ta1_src=$(realpath "$2")
base=$(realpath -m "$3")
receipts=$(realpath -m "$4")
pin=57101150ced8f39c5e229d8234f29c41e42764f7
marker=vkm-geo-synthetic-20261114
[[ "$base" != / && "$base" != "$HOME" && "$base" != "$ta1_src" ]] || exit 2
"$python312" -c 'import sys; assert sys.version_info[:2] == (3,12), "Python 3.12 required"'
[[ $(git -C "$ta1_src" rev-parse HEAD) == "$pin" ]] || { echo 'Unexpected TA1 source commit' >&2; exit 2; }
# Windows checkout may use CRLF on a mounted drive. Normalize it for this
# read-only comparison only; do not modify the repository's Git configuration.
GIT_OPTIONAL_LOCKS=0 git -c core.autocrlf=true -C "$ta1_src" diff --quiet HEAD -- || { echo 'TA1 source has tracked edits' >&2; exit 2; }
command -v uv >/dev/null
mkdir -p "$base" "$receipts"
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2
for name in data gempy; do
  env_path="$base/$name"
  if [[ -e "$env_path" ]]; then
    [[ -x "$env_path/bin/python" && -f "$env_path/.$marker" ]] || { echo "Refusing to change unmarked environment: $env_path" >&2; exit 2; }
  else
    uv venv --python "$python312" "$env_path"
    touch "$env_path/.$marker"
  fi
done
# TA1 0.2.1 requires GeoPandas ^0.14; SQLAlchemy 2.1 removes an API
# used by sqlalchemy-utils 0.41.2. NumPy 1.x isolates old pyogrio wheels.
data_pins=(
  aiofiles==23.2.1 annotated-types==0.8.0 attrs==26.1.0 certifi==2026.10.23
  click==8.5.1 click-plugins==1.1.1.2 cligj==0.7.2 colorlog==6.12.0
  fiona==1.10.1 geoalchemy2==0.15.2 geopandas==0.14.4 greenlet==3.5.6
  macrostrat-database==3.5.4 macrostrat-utils==1.3.3 markdown-it-py==4.3.0
  mdurl==0.1.2 numpy==1.26.4 packaging==26.3 pandas==3.0.6
  psycopg2-binary==2.9.13 pydantic==2.14.3 pydantic-core==2.50.4
  pygments==2.21.0 pykrige==1.7.3 pyogrio==0.7.2 pyproj==3.8.0
  python-dateutil==4.0.0 rich==13.9.4 scipy==1.17.1 shapely==2.2.0
  sqlalchemy==2.0.46 sqlalchemy-utils==0.41.2 sqlparse==0.5.5
  typing-extensions==4.16.0 typing-inspection==0.4.5 tzdata==2026.6
)
uv pip install --python "$base/data/bin/python" "${data_pins[@]}" "$ta1_src"
uv pip install --python "$base/gempy/bin/python" \
  annotated-types==0.8.0 gempy==2026.0.3 gempy-engine==2026.0.3.post1 \
  numpy==2.5.4 pydantic==2.14.3 pydantic-core==2.50.4 python-dotenv==1.2.4 \
  typing-extensions==4.16.0 typing-inspection==0.4.5
for name in data gempy; do
  uv pip check --python "$base/$name/bin/python"
  uv pip freeze --python "$base/$name/bin/python" > "$receipts/${name}_freeze.txt"
done
printf '%s\n' "$pin" > "$receipts/ta1_commit.txt"
printf '%s\n' 'INSTALLED: run geo_smoke.py in each matching interpreter before declaring SMOKE_PASS'
