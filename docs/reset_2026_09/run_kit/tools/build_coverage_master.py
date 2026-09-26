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
CORPUS = root('VKM_WORK') / 'corpus'          # optional: page texts, only for the read-fully warning
OCR_SOURCES = {'VKM-SRC-025', 'VKM-SRC-037'}  # no text layer; their page texts come from OCR, not from CORPUS
OVERRIDES: dict[str, tuple[str, str]] = {
    'VKM-SRC-013': ('SUPERSEDED_BY_COPY', 'Original page ZIP deliberately deleted after assembling VKM-SRC-025; content reviewed via VKM-SRC-025 (see its row).'),
    'VKM-SRC-022': ('RETIRED_NOT_EVIDENCE', 'LEGACY_RETIRED project-generated package (SKRU1_ACTUAL_DATA_TABLES_v1), intentionally absent; not scientific evidence.'),
}
# Page-level corrections of chunk coverage statements (review finding COVERAGE_DUPLICATES-002). sweep_raw is the
# primary record and stays unchanged; the correction is applied here and named in coverage_basis.
PAGE_CORRECTIONS: dict[tuple[str, str], list[tuple[str, frozenset[int], str]]] = {
    ('VKM-SRC-028', 'c2'): [
        ('read_fully->skimmed', frozenset({132, 140, 148, 151, 158, 160, 165, 167}),
         'figure-only pages: text layer = caption (< 80 chars), drawings not rendered'),
        ('none->skimmed', frozenset({208}), 'processed (pages_done) but given no coverage status; equipment panel text'),
    ],
}
# Open questions that a chunk deferred to another chunk of the same source and that the other chunk answered
# (review finding COVERAGE_DUPLICATES-011; checked against all_records.jsonl, the citation graph and the chronology).
RESOLVED_QUESTIONS: dict[tuple[str, str], str] = {
    ('VKM-SRC-020', 'Раздел 3 (маркшейдерское обеспечение'):
        'RESOLVED_IN_OTHER_CHUNK: pp. 106-127 read, 58 records incl. 3 coordinate_system and 8 monitoring_observation',
    ('VKM-SRC-037', 'Содержание ссылок [11]'):
        'RESOLVED_IN_OTHER_CHUNK: bibliography p. 230 - EV-VN-S037-1219, -1228, -1229; citation rows CG-02004, CG-02012, CG-02013',
    ('VKM-SRC-011', 'Date of construction start of the First Solikamsk mine'):
        'RESOLVED_IN_OTHER_CHUNK: shaft No. 1 laid 1927-11-07 (EV-VN-S011-2706; chronology CHR-0023/CHR-0024)',
}
# other questions that only point to another chunk are tagged, not dropped: nobody checked that they were answered
DEFERRAL = re.compile(r'(другой чанк|другом чанке|вне чанка|outside chunk|next chunk|следующ\w* чанк\w*|чанк\w* c\d)', re.I)
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


def cap(text: str, n: int, sid: str, field: str) -> str:
    """Cut a merged text cell to n characters and say so (no silent truncation)."""
    if len(text) <= n:
        return text
    tail = f' … [truncated; full text: sweep_raw/{sid}/*/coverage.json {field}]'
    return text[:n - len(tail)] + tail


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
        full, skim, unread, viewed, done = set(), set(), set(), set(), set()
        corrections: list[str] = []
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
                    done |= expand(pj.get('pages_done'))   # processed pages; NOT evidence of a full read (review COVERAGE_DUPLICATES-001)
                except Exception:  # noqa: BLE001
                    st = 'PROGRESS_UNREADABLE'
            if cov.exists():
                try:
                    cj = json.loads(cov.read_text(encoding='utf-8'))
                    st = 'DONE'
                    c_full = expand(cj.get('pages_read_fully'))
                    c_skim = expand(cj.get('pages_skimmed'))
                    for action, pages, reason in PAGE_CORRECTIONS.get((sid, c.name), []):
                        c_full -= pages
                        c_skim |= pages
                        corrections.append(f'{c.name} pages {min(pages)}-{max(pages)} ({len(pages)}) {action}: {reason}')
                    full |= c_full
                    skim |= c_skim
                    unread |= expand(cj.get('pages_unreadable'))
                    viewed |= expand(cj.get('pages_rendered_and_viewed'))
                    chapters += [str(x) for x in (cj.get('chapters_covered') or [])]
                    domains |= set(cj.get('domains') or [])
                    scopes |= set(cj.get('mine_site_scope') or [])
                    if cj.get('summary_ru'):
                        summaries.append(cj['summary_ru'])
                    if cj.get('underestimated_before'):
                        under.append(cj['underestimated_before'])
                    for q in (str(x) for x in (cj.get('open_questions') or [])):
                        hit = next((v for (qs, prefix), v in RESOLVED_QUESTIONS.items()
                                    if qs == sid and q.startswith(prefix)), None)
                        if hit:
                            q = f'[{hit}] {q}'
                        elif DEFERRAL.search(q):
                            q = f'[DEFERRED_TO_OTHER_CHUNK, not re-checked] {q}'
                        questions.append(q)
                    feats.update({k: v for k, v in (cj.get('features') or {}).items() if isinstance(v, (int, float))})
                    if cj.get('method'):
                        methods.add(cj['method'])
                    if cj.get('bibliographic'):
                        biblio = cj['bibliographic']
                except Exception:  # noqa: BLE001
                    st = 'COVERAGE_UNREADABLE'
            statuses.append(f'{c.name}:{st}')
        npages = page_count(sid)
        nostatus = done - (full | skim | unread)
        if nostatus and sid not in OVERRIDES:      # COVERAGE_DUPLICATES-002: every processed page needs a status
            sys.exit(f'{sid}: pages processed but without coverage status: {sorted(nostatus)}')
        for p in sorted(full - viewed) if CORPUS.exists() and sid not in OCR_SOURCES else []:
            t = CORPUS / sid / f'p{p:04d}.txt'
            if t.exists() and t.stat().st_size < 80:
                print(f'warning: {sid} p.{p} counted as read fully, text layer < 80 bytes, not rendered', file=sys.stderr)
        rs = recs.get(sid, [])
        kinds = collections.Counter(x.get('kind') for x in rs)
        n_new = sum(1 for x in rs if x.get('is_new') is True)
        covered = full | skim | done
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
            basis = (f'all {len(chunks)} chunk(s) DONE; pages read fully {len(full)}/{npages} (coverage.json), '
                     f'processed {len(covered)}/{npages}, skimmed {len(skim)}, unreadable {len(unread)}, '
                     f'rendered+viewed {len(viewed)}; rule: FULLY_REVIEWED needs processed >= 95 % and read fully >= 85 %')
            if corrections:
                basis += '; page corrections (COVERAGE_DUPLICATES-002): ' + ' | '.join(corrections)
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
            'relevant_chapters': cap(' | '.join(dict.fromkeys(chapters)), 1500, sid, 'chapters_covered'),
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
            'new_findings': cap(' || '.join(under), 2000, sid, 'underestimated_before'),
            'unresolved_issues': cap(' || '.join(questions), 2000, sid, 'open_questions'),
            'summary_ru': cap(' || '.join(summaries), 3000, sid, 'summary_ru'),
        })
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(collections.Counter(r['coverage_level'] for r in rows))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else str(merged_dir() / 'SOURCE_COVERAGE_MASTER.csv'))
