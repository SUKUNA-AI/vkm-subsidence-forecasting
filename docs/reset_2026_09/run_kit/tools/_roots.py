"""Roots of the run kit. Executable code carries no host paths (DATA_AND_PATH_POLICY_RU §3).

Variables:
  VKM_PUB             checkout of the PUBLIC repository (vkm-subsidence-forecasting)
  VKM_RESOURCES_ROOT  checkout of the PRIVATE repository (vkm-subsidence-forecasting_resourses)
  VKM_WORK            scratch root outside both repositories (corpus page texts, run/ outputs)

Optional overrides (Phase-1 layout, see run_kit/README_RU.md):
  VKM_SWEEP_DIR       per-chunk sweep outputs <SID>/<chunk>/{records.jsonl,progress.json,coverage.json};
                      default: the committed copy $VKM_RESOURCES_ROOT/11_evidence_vnext/sweep_raw
  VKM_MERGED_DIR      merged outputs (all_records.jsonl, quote checks, coverage master); default $VKM_WORK/merged
  VKM_SYNTH_DIR       synthesis stream directories <STREAM>/; default $VKM_RESOURCES_ROOT/11_evidence_vnext/canonical

Lookup order: the environment, then ``roots.env`` one level above this ``tools/`` directory (written by
``localize_paths.sh`` into ``$WORK/run/roots.env`` next to the localized ``tools/`` copy). The values of the
cloud run of 26.09.2026 are documented in the header of ``localize_paths.sh``.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / 'roots.env'
_LINE = re.compile(r'^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$')


def _from_env_file(name: str) -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        m = _LINE.match(line)
        if m and m.group(1) == name:
            return m.group(2).strip().strip('"').strip("'") or None
    return None


def root(name: str) -> pathlib.Path:
    """Directory named by ``name``; exits with a hint when it is not configured."""
    value = os.environ.get(name) or _from_env_file(name)
    if not value:
        sys.exit(f'{name} is not set: export it or run localize_paths.sh (writes $WORK/run/roots.env); '
                 f'cloud-run values are listed in docs/reset_2026_09/run_kit/localize_paths.sh')
    return pathlib.Path(value)


def _optional(name: str) -> str | None:
    return os.environ.get(name) or _from_env_file(name)


def sweep_dir() -> pathlib.Path:
    """Per-chunk sweep outputs (Phase 1): VKM_SWEEP_DIR, else the committed PRIVATE copy."""
    v = _optional('VKM_SWEEP_DIR')
    return pathlib.Path(v) if v else root('VKM_RESOURCES_ROOT') / '11_evidence_vnext' / 'sweep_raw'


def merged_dir() -> pathlib.Path:
    """Merged sweep outputs: VKM_MERGED_DIR, else $VKM_WORK/merged."""
    v = _optional('VKM_MERGED_DIR')
    return pathlib.Path(v) if v else root('VKM_WORK') / 'merged'


def synth_dir() -> pathlib.Path:
    """Synthesis stream directories: VKM_SYNTH_DIR, else the PRIVATE canonical catalogues."""
    v = _optional('VKM_SYNTH_DIR')
    return pathlib.Path(v) if v else root('VKM_RESOURCES_ROOT') / '11_evidence_vnext' / 'canonical'
