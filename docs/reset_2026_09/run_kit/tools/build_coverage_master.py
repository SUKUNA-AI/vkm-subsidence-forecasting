"""Build SOURCE_COVERAGE_MASTER.csv from register + old inventory + extraction summary + sweep coverage/progress.

Deterministic. Coverage level is derived by explicit rules (stated in coverage_basis); the coordinator
may override a level only through OVERRIDES below with a written reason.
Usage: python build_coverage_master.py <out.csv>
"""
from __future__ import annotations

import collections
import csv
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _roots import merged_dir, root, sweep_dir  # noqa: E402

RES = root('VKM_RESOURCES_ROOT')
SWEEP = sweep_dir()
MERGED = merged_dir() / 'all_records.jsonl'
# identical to run_kit/corpus_extraction_summary.json (checked 26.09.2026); used when the corpus text is not local
_EXTR_LOCAL = root('VKM_WORK') / 'corpus' / 'extraction_summary.json'
_EXTR_KIT = pathlib.Path(__file__).resolve().parent.parent / 'corpus_extraction_summary.json'
EXTR = json.load(open(_EXTR_LOCAL if _EXTR_LOCAL.exists() else _EXTR_KIT, encoding='utf-8'))
OVERRIDES: dict[str, tuple[str, str]] = {
    'VKM-SRC-013': ('SUPERSEDED_BY_COPY', 'Original page ZIP deliberately deleted after assembling VKM-SRC-025; content reviewed via VKM-SRC-025 (see its row).'),
    'VKM-SRC-022': ('RETIRED_NOT_EVIDENCE', 'LEGACY_RETIRED project-generated package (SKRU1_ACTUAL_DATA_TABLES_v1), intentionally absent; not scientific evidence.'),
}
DOMAIN_KINDS = {
    'boreholes': ('borehole', 'horizon_pick'),
    'mining_information': ('mining_geometry', 'engineering_rule'),
    'monitoring': ('monitoring_observation',),
    'geology': ('stratigraphic_unit', 'spatial_entity'),
    'mechanics': ('parameter', 'stress_measurement'),
    'rheology': ('rheology_law',),
    'hydro': ('hydro',),
    'thermal': ('thermal',),
    'geophysics': ('geophysics', 'gpr', 'seismic'),
    'surveying': ('monitoring_observation', 'coordinate_system', 'subsidence_method'),
    'bibliography': ('citation',),
    'equations': ('formula', 'rheology_law', 'subsidence_method'),
    'chronology': ('chronology_event',),
    'backfill': ('backfill',),
}


def page_count(sid):
    e = EXTR.get(sid, {})
    if sid == 'VKM-SRC-023':
        return 115
    if sid == 'VKM-SRC-037':
        return 236
    return e.get('pages')


def expand(pages):
    out = set()
    for p in pages or []:
        if isinstance(p, int):
            out.add(p)
        elif isinstance(p, str):
            for part in re.split(r'[,\s]+', p):
                m = re.match(r'^(\d+)\s*[-–]\s*(\d+)$', part)
                if m:
                    out |= set(range(int(m.group(1)), int(m.group(2)) + 1))
                elif part.isdigit():
                    out.add(int(part))
        elif isinstance(p, dict):
            out |= expand([p.get('pages')])
    return out


def main(out_path):
    reg = list(csv.DictReader(open(RES / '00_registry/SOURCE_REGISTER.csv', encoding='utf-8')))
    inv = {r['source_id']: r for r in csv.DictReader(open(
        RES / '10_physics_evidence/physical_evidence_v1/source_inventory.csv', encoding='utf-8'))}
    recs = collections.defaultdict(list)
    if MERGED.exists():
        for line in open(MERGED, encoding='utf-8'):
            r = json.loads(line)
            recs[r.get('source_id')].append(r)
    rows = []
    for r in reg:
        sid = r['resource_id']
        i = inv.get(sid, {})
        chunks = sorted((SWEEP / sid).glob('*/')) if (SWEEP / sid).exists() else []
        full, skim, unread, viewed = set(), set(), set(), set()
        chapters, domains, scopes, summaries, questions, under = [], set(), set(), [], [], []
        feats = collections.Counter()
        methods, statuses, biblio = set(), [], None
        for c in chunks:
            prog = c / 'progress.json'
            cov = c / 'coverage.json'
            st = 'NO_OUTPUT'
            if prog.exists():
                try:
                    pj = json.loads(prog.read_text(encoding='utf-8'))
                    st = pj.get('status', 'UNKNOWN')
                    full |= expand(pj.get('pages_done'))
                except Exception:  # noqa: BLE001
                    st = 'PROGRESS_UNREADABLE'
            if cov.exists():
                try:
                    cj = json.loads(cov.read_text(encoding='utf-8'))
                    st = 'DONE'
                    full |= expand(cj.get('pages_read_fully'))
                    skim |= expand(cj.get('pages_skimmed'))
                    unread |= expand(cj.get('pages_unreadable'))
                    viewed |= expand(cj.get('pages_rendered_and_viewed'))
                    chapters += [str(x) for x in (cj.get('chapters_covered') or [])]
                    domains |= set(cj.get('domains') or [])
                    scopes |= set(cj.get('mine_site_scope') or [])
                    if cj.get('summary_ru'):
                        summaries.append(cj['summary_ru'])
                    if cj.get('underestimated_before'):
                        under.append(cj['underestimated_before'])
                    questions += [str(x) for x in (cj.get('open_questions') or [])]
                    feats.update({k: v for k, v in (cj.get('features') or {}).items() if isinstance(v, (int, float))})
                    if cj.get('method'):
                        methods.add(cj['method'])
                    if cj.get('bibliographic'):
                        biblio = cj['bibliographic']
                except Exception:  # noqa: BLE001
                    st = 'COVERAGE_UNREADABLE'
            statuses.append(f'{c.name}:{st}')
        npages = page_count(sid)
        rs = recs.get(sid, [])
        kinds = collections.Counter(x.get('kind') for x in rs)
        n_new = sum(1 for x in rs if x.get('is_new') is True)
        covered = full | skim
        if sid in OVERRIDES:
            level, basis = OVERRIDES[sid]
        elif not chunks:
            level, basis = 'UNSEEN', 'no sweep output'
        elif any(not s.endswith(':DONE') for s in statuses):
            level, basis = 'PARTIAL_IN_PROGRESS', 'chunks not all DONE: ' + ', '.join(statuses)
        else:
            frac = len(covered) / npages if npages else 1.0
            fullfrac = len(full) / npages if npages else 1.0
            if frac >= 0.95 and fullfrac >= 0.85:
                level = 'FULLY_REVIEWED'
            elif frac >= 0.95 and fullfrac >= 0.25:
                level = 'RELEVANT_SECTIONS_REVIEWED'
            elif frac >= 0.95:
                level = 'LOW_RELEVANCE_CONFIRMED'
            else:
                level = 'PARTIAL_IN_PROGRESS'
            basis = (f'all {len(chunks)} chunk(s) DONE; pages read fully {len(full)}/{npages}, skimmed {len(skim)}, '
                     f'unreadable {len(unread)}, rendered+viewed {len(viewed)}')
        dom_counts = {d: sum(kinds.get(k, 0) for k in ks) for d, ks in DOMAIN_KINDS.items()}
        rows.append({
            'source_id': sid, 'filename': r['canonical_path'], 'original_filename': r['original_filename'],
            'title': (biblio or {}).get('title') or i.get('title', ''), 'authors': (biblio or {}).get('authors') or i.get('authors', ''),
            'year': (biblio or {}).get('year') or i.get('year', ''), 'document_type': r.get('source_class', ''),
            'register_scope': r.get('evidence_scope', ''), 'geographic_scope': i.get('object_site', ''),
            'mine_attribution': ';'.join(sorted(scopes)), 'pages': npages or '',
            'text_state': ('OCR (tesseract rus 300dpi)' if sid in ('VKM-SRC-025', 'VKM-SRC-037') else
                           'DOCX→PDF render + text' if sid == 'VKM-SRC-023' else
                           (f"text layer {EXTR.get(sid, {}).get('pages_text')}/{EXTR.get(sid, {}).get('pages')}" if EXTR.get(sid, {}).get('pages') else 'n/a')),
            'relevant_chapters': ' | '.join(dict.fromkeys(chapters))[:1500],
            'tables': feats.get('tables', 0), 'figures': feats.get('figures', 0), 'maps': feats.get('maps', 0),
            'cross_sections': feats.get('cross_sections', 0), 'equations': dom_counts['equations'],
            'boreholes': dom_counts['boreholes'], 'mining_information': dom_counts['mining_information'],
            'monitoring': dom_counts['monitoring'], 'geology': dom_counts['geology'], 'mechanics': dom_counts['mechanics'],
            'rheology': dom_counts['rheology'], 'hydro': dom_counts['hydro'], 'thermal': dom_counts['thermal'],
            'geophysics': dom_counts['geophysics'], 'surveying': dom_counts['surveying'],
            'chronology': dom_counts['chronology'], 'backfill': dom_counts['backfill'],
            'bibliography_entries': dom_counts['bibliography'], 'domains': ';'.join(sorted(domains)),
            'reviewed_pages': len(full), 'skimmed_pages': len(skim), 'unreadable_pages': len(unread),
            'rendered_viewed_pages': len(viewed), 'review_method': ';'.join(sorted(methods)),
            'chunks': ' '.join(statuses), 'n_records': len(rs), 'n_new_records': n_new,
            'coverage_level': level, 'coverage_basis': basis,
            'new_findings': ' || '.join(under)[:2000], 'unresolved_issues': ' || '.join(questions)[:2000],
            'summary_ru': ' || '.join(summaries)[:3000],
        })
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(collections.Counter(r['coverage_level'] for r in rows))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else str(merged_dir() / 'SOURCE_COVERAGE_MASTER.csv'))
