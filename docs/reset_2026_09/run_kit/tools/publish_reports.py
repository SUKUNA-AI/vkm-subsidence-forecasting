"""Copy synthesis reports into PUBLIC docs/science and rewrite local links to public catalogue paths.

Links to files that are published (per scripts/public_catalogue_map.json) are rewritten to relative PUBLIC
paths; links to private/build artefacts become plain code spans. «…» quotations over 25 words are flagged.
Usage: python publish_reports.py STREAM:REPORT.md[=DEST.md] [...]
  REPORT.md is relative to the stream directory; DEST.md is relative to docs/science (default: the report's name).
  EXTERNAL reports get the external-search header (decision D-12), e.g.
  EXTERNAL:reports/NORMATIVE_RU.md=external/EXTERNAL_RESEARCH_NORMATIVE_RU.md
With --all every Phase-1 report listed in REPORTS is published.
"""
import json, os, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _roots import root, synth_dir  # noqa: E402

PUB = root('VKM_PUB')
SYN = synth_dir()
sys.path.insert(0, str(PUB / 'src'))
from vkm_world.governance.leakage import sanitize_paths  # noqa: E402
mapping = json.loads((PUB / 'scripts/public_catalogue_map.json').read_text(encoding='utf-8'))
names = [pathlib.Path(src).name for src in mapping]
by_name = {pathlib.Path(src).name: dst for src, dst in mapping.items()
           if names.count(pathlib.Path(src).name) == 1}   # FIX_LOG.csv etc. exist in several streams: resolve stream-first
DEST = PUB / 'docs' / 'science'
DEST.mkdir(parents=True, exist_ok=True)
LINK = re.compile(r'(!?)\[([^\]]*)\]\(([^)\s]+)\)')


def fix(text: str, report_name: str, out_dir: pathlib.Path = DEST, stream: str = '') -> tuple[str, list[str]]:
    notes = []

    def rep(m):
        bang, label, target = m.groups()
        if re.match(r'^(https?:|mailto:|#)', target):
            return m.group(0)
        path = target.split('#')[0]
        name = pathlib.Path(path).name
        dst = (mapping.get(f'{stream}/{path.removeprefix("./")}') or mapping.get(f'{stream}/{name}')
               or by_name.get(name))
        if dst:
            rel = os.path.relpath(PUB / dst, out_dir)
            return f'{bang}[{label}]({rel})'
        if name.endswith('.md'):
            hit = next((q for q in (out_dir / name, DEST / name, DEST / 'external' / name) if q.exists()), None)
            if hit is not None:
                return f'[{label}]({os.path.relpath(hit, out_dir)})'
        notes.append(f'unlinked: {target}')
        return f'{label} (`{target}`, PRIVATE/рабочие материалы)' if label != target else f'`{target}`'
    out = LINK.sub(rep, text)
    for q in re.findall(r'«([^»]+)»', out):          # D-12 / DOCS_LEAKAGE-023: quotations over 25 words
        if len(q.split()) > 25:
            notes.append(f'long quotation {len(q.split())} words: {q[:60]}…')
    return out, notes


REPORTS = [
    'BOREHOLES:BOREHOLE_AND_3D_RECONSTRUCTION_RU.md',
    'CITATIONS:CITATION_GRAPH_RU.md',
    'GEOLOGY_COORDS:COORDINATE_DATUM_AUDIT_RU.md',
    'GEOLOGY_COORDS:GEOLOGY_EVIDENCE_RU.md',
    'HYDRO_THERMAL_GEOPHYS:HYDRO_THERMAL_GEOPHYSICS_EVIDENCE_RU.md',
    'FORMULAS:MATHEMATICAL_FOUNDATION_CATALOGUE_RU.md',
    'MECH_RHEO:MECHANICS_RHEOLOGY_EVIDENCE_RU.md',
    'MONITORING_LIFECYCLE:MINE_LIFECYCLE_AND_INFORMATION_FLOW_RU.md',
    'MINING:MINING_ENGINEERING_EVIDENCE_RU.md',
    'PHYSICS_CAUSAL:CAUSAL_GRAPH_RU.md',
    'PHYSICS_CAUSAL:PHYSICS_COVERAGE_RU.md',
    'OCR_VISUAL_QA:VISUAL_OCR_QA_RU.md',
] + [f'EXTERNAL:reports/{s}_RU.md=external/EXTERNAL_RESEARCH_{s}_RU.md'
     for s in ('BOREHOLES_GEOLOGY', 'GEOPHYS_GPR', 'NORMATIVE', 'RHEOLOGY_MECH', 'SKRU1_SPECIFIC', 'SUBSIDENCE_FORMULAS')]


def header(stream: str, rep: str) -> str:
    if stream == 'EXTERNAL':
        return (f'<!-- Опубликовано из PRIVATE 11_evidence_vnext/canonical/EXTERNAL/{rep} (внешний поиск Phase 1, '
                'только WebSearch/ограниченный доступ, см. решение D-12). -->\n')
    return (f'<!-- Опубликовано из PRIVATE 11_evidence_vnext/canonical/{stream}/{rep} (синтез Phase 1). '
            f'Дословные цитаты источников — только в PRIVATE. -->\n')


for arg in (REPORTS if sys.argv[1:] == ['--all'] else sys.argv[1:]):
    stream, spec = arg.split(':', 1)
    rep, _, dest = spec.partition('=')
    dest = dest or pathlib.Path(rep).name
    text = (SYN / stream / rep).read_text(encoding='utf-8')
    target = DEST / dest
    target.parent.mkdir(parents=True, exist_ok=True)
    out, notes = fix(text, rep, target.parent, stream)
    target.write_text(sanitize_paths(header(stream, rep) + out), encoding='utf-8', newline='\n')
    print(dest, len(notes), notes[:4])
