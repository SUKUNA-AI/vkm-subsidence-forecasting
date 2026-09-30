"""Copy synthesis reports into PUBLIC docs/science and rewrite local links to public catalogue paths.

Links to files that are published (per scripts/public_catalogue_map.json) are rewritten to relative PUBLIC
paths; links to private/build artefacts become plain code spans. Quotations and leakage block the whole batch.
Usage: python publish_reports.py STREAM:REPORT.md[=DEST.md] [...]
  REPORT.md is relative to the stream directory; DEST.md is relative to docs/science (default: the report's name).
  EXTERNAL reports get the external-search header (decision D-12), e.g.
  EXTERNAL:reports/NORMATIVE_RU.md=external/EXTERNAL_RESEARCH_NORMATIVE_RU.md
With --all every Phase-1 report listed in REPORTS is published.
"""
import csv, hashlib, json, os, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _roots import root, synth_dir  # noqa: E402

PUB = root('VKM_PUB')
SYN = synth_dir()
sys.path.insert(0, str(PUB / 'src'))
from vkm_world.governance.leakage import (  # noqa: E402
    FORBIDDEN_COLUMNS, VERBATIM_LIMIT_WORDS, longest_shared_run, quote_shingles, sanitize_paths, words)
from vkm_world.governance.publication import (  # noqa: E402
    contained_path, load_catalogue_map, publish_batch, relative_path)
mapping = load_catalogue_map(PUB / 'scripts/public_catalogue_map.json')
names = [pathlib.Path(src).name for src in mapping]
by_name = {pathlib.Path(src).name: dst for src, dst in mapping.items()
           if names.count(pathlib.Path(src).name) == 1}   # FIX_LOG.csv etc. exist in several streams: resolve stream-first
DEST = PUB / 'docs' / 'science'
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


def main(arguments) -> int:
    try:
        canon = root('VKM_RESOURCES_ROOT')/'11_evidence_vnext/canonical'
        shingles = set()
        for relative in mapping:
            source = contained_path(canon, relative)
            if not source.is_file():
                raise ValueError('required canonical quote input is missing')
            if source.suffix == '.csv':
                with source.open(encoding='utf8', newline='') as stream:
                    rows = csv.reader(stream, strict=True)
                    fields = next(rows, [])
                    if not fields:
                        raise ValueError('canonical quote input has no CSV header')
                    qi = [i for i, field in enumerate(fields) if field.strip().lower() in FORBIDDEN_COLUMNS]
                    shingles |= quote_shingles(row[i] for row in rows for i in qi if i < len(row))
        outputs, entries, diagnostics = {}, [], []
        for arg in (REPORTS if arguments == ['--all'] else arguments):
            stream, spec = arg.split(':', 1)
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', stream):
                raise ValueError('invalid report stream')
            rep, _, dest = spec.partition('=')
            relative_path(rep)
            dest = relative_path(dest or pathlib.Path(rep).name)
            if pathlib.Path(dest).suffix != '.md':
                raise ValueError('report destination must be Markdown')
            source = contained_path(SYN, f'{stream}/{rep}')
            target = contained_path(DEST, dest)
            rel = target.relative_to(PUB.resolve()).as_posix()
            if rel in outputs:
                raise ValueError('duplicate report destination')
            original = source.read_bytes()
            out, notes = fix(original.decode('utf8'), rep, target.parent, stream)
            out = sanitize_paths(header(stream, rep) + out)
            if (any(sum(not token.isdigit() for token in words(q)) >= VERBATIM_LIMIT_WORDS
                    for q in re.findall(r'«([^»]+)»', out))
                    or longest_shared_run(words(out), shingles)[0] >= VERBATIM_LIMIT_WORDS):
                raise ValueError('report repeats a long quotation')
            outputs[rel] = out.encode('utf8')
            entries.append({'source': f'{stream}/{rep}', 'source_sha256': hashlib.sha256(original).hexdigest(),
                            'target': rel, 'target_sha256': hashlib.sha256(outputs[rel]).hexdigest(), 'status': 'OK'})
            diagnostics.append((dest, len(notes)))
        if not outputs:
            raise ValueError('no reports selected')
        manifest = {'schema': 'vkm.public_reports_manifest/1',
                    'generator': 'docs/reset_2026_09/run_kit/tools/publish_reports.py',
                    'private_root_rel': '11_evidence_vnext/canonical', 'files': entries, 'leakage_problems': []}
        outputs['docs/science/PUBLIC_REPORT_MANIFEST.json'] = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1) + '\n').encode('utf8')
        publish_batch(PUB, outputs)
        for dest, unlinked in diagnostics:
            print(dest, f'{unlinked} unresolved local links')
        return 0
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        print(f'report publication aborted: {type(exc).__name__}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
