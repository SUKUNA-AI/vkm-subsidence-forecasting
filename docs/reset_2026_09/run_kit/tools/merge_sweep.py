"""Merge per-chunk sweep outputs into consolidated tables + deterministic quote verification.

Usage: python merge_sweep.py  (reads $VKM_WORK/run/sweep/**, writes $VKM_WORK/run/merged/)
Page texts for quote checks: $VKM_WORK/corpus/<SID>/pNNNN.txt and the per-page OCR text kept under the
work directory of the resources checkout (VKM_RESOURCES_ROOT). Roots: see _roots.py.
"""
from __future__ import annotations

import collections
import csv
import json
import pathlib
import re
import unicodedata

from _roots import root

WORK, RES = root('VKM_WORK'), root('VKM_RESOURCES_ROOT')
SWEEP = WORK / 'run' / 'sweep'
OUT = WORK / 'run' / 'merged'
CORPUS = WORK / 'corpus'
OCR = RES / 'work' / 'ocr'
OUT.mkdir(parents=True, exist_ok=True)


def norm(s: str) -> str:
    s = unicodedata.normalize('NFKC', s or '')
    s = s.replace('­', '').replace('ё', 'е').replace('Ё', 'Е')
    s = re.sub(r'-\s*\n\s*', '', s)           # hyphenation
    s = re.sub(r'[\s ]+', ' ', s)
    s = re.sub(r'[«»“”„"\'`’‘]', '"', s)
    s = re.sub(r'[–—−‑]', '-', s)
    return s.strip().lower()


_page_cache: dict[tuple[str, int], str] = {}


def page_text(sid: str, page: int | None) -> str:
    if page is None:
        return ''
    key = (sid, page)
    if key in _page_cache:
        return _page_cache[key]
    txt = ''
    for cand in (CORPUS / sid / f'p{page:04d}.txt', OCR / sid / f'p{page:04d}.txt'):
        if cand.exists():
            t = cand.read_text(encoding='utf-8', errors='replace')
            if len(t.strip()) > len(txt.strip()):
                txt = t
    _page_cache[key] = norm(txt)
    return _page_cache[key]


def verify_quote(rec: dict) -> str:
    q = norm(rec.get('quote') or '')
    if len(q) < 8:
        return 'NO_QUOTE'
    sid = rec.get('source_id') or ''
    try:
        page = int(rec.get('pdf_page')) if rec.get('pdf_page') not in (None, '') else None
    except (TypeError, ValueError):
        page = None
    pages = [page] if page else []
    if page:
        pages += [page - 1, page + 1]
    for p in pages:
        if p and p > 0:
            t = page_text(sid, p)
            if not t:
                continue
            if q in t:
                return 'EXACT' if p == page else 'EXACT_ADJACENT_PAGE'
            # fuzzy: 85 % of 6-gram word shingles present
            w = q.split()
            if len(w) >= 6:
                sh = [' '.join(w[i:i + 6]) for i in range(len(w) - 5)]
                hit = sum(1 for s in sh if s in t) / len(sh)
                if hit >= 0.6:
                    return f'FUZZY_{int(hit * 100)}' + ('' if p == page else '_ADJ')
    if not pages:
        return 'NO_PAGE'
    if not any(page_text(sid, p) for p in pages if p and p > 0):
        return 'NO_PAGE_TEXT'
    return 'NOT_FOUND'


def load():
    recs, covs, bad = [], [], []
    for d in sorted(SWEEP.glob('*/*')):
        if not d.is_dir():
            continue
        sid_dir, chunk = d.parent.name, d.name
        cov = d / 'coverage.json'
        if cov.exists():
            try:
                c = json.loads(cov.read_text(encoding='utf-8'))
                c.setdefault('source_id', sid_dir)
                c['_dir'] = f'{sid_dir}/{chunk}'
                covs.append(c)
            except Exception as e:  # noqa: BLE001
                bad.append({'file': str(cov), 'error': repr(e)})
        rf = d / 'records.jsonl'
        if rf.exists():
            for i, line in enumerate(rf.read_text(encoding='utf-8').splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception as e:  # noqa: BLE001
                    bad.append({'file': str(rf), 'line': i, 'error': repr(e)[:200]})
                    continue
                r.setdefault('source_id', sid_dir)
                r['_dir'] = f'{sid_dir}/{chunk}'
                r['_line'] = i
                recs.append(r)
    return recs, covs, bad


def main():
    recs, covs, bad = load()
    counters = collections.Counter()
    for n, r in enumerate(recs, 1):
        sid = r.get('source_id', 'UNK')
        counters[sid] += 1
        r['vn_id'] = f"EV-VN-{sid.replace('VKM-SRC-', 'S')}-{counters[sid]:04d}"
        r['quote_check'] = verify_quote(r)
    with open(OUT / 'all_records.jsonl', 'w', encoding='utf-8') as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    json.dump(covs, open(OUT / 'all_coverage.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(bad, open(OUT / 'parse_errors.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    by_kind = collections.defaultdict(list)
    for r in recs:
        by_kind[r.get('kind', 'UNKNOWN_KIND')].append(r)
    common = ['vn_id', 'kind', 'source_id', 'pdf_page', 'printed_page', 'locator_extra', 'status', 'evidence_type',
              'scope', 'scale', 'confidence', 'extraction_method', 'quote_check', 'is_new', 'existing_evidence_ids',
              'spatial_level', 'spatial_name', 'temporal', 'quote', 'notes', '_dir']
    for kind, rs in by_kind.items():
        data_keys = []
        for r in rs:
            for k in (r.get('data') or {}):
                if k not in data_keys:
                    data_keys.append(k)
        with open(OUT / f'kind_{kind}.csv', 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(common + [f'data.{k}' for k in data_keys])
            for r in rs:
                ss = r.get('spatial_support') or {}
                row = [r.get('vn_id'), kind, r.get('source_id'), r.get('pdf_page'), r.get('printed_page'),
                       r.get('locator_extra'), r.get('status'), r.get('evidence_type'), r.get('scope'), r.get('scale'),
                       r.get('confidence'), r.get('extraction_method'), r.get('quote_check'), r.get('is_new'),
                       ';'.join(r.get('existing_evidence_ids') or []), ss.get('level') if isinstance(ss, dict) else '',
                       ss.get('name') if isinstance(ss, dict) else '', json.dumps(r.get('temporal_support'), ensure_ascii=False),
                       r.get('quote'), r.get('notes'), r.get('_dir')]
                d = r.get('data') or {}
                for k in data_keys:
                    v = d.get(k)
                    row.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                w.writerow(row)
    summ = {
        'n_records': len(recs), 'n_coverage_files': len(covs), 'n_parse_errors': len(bad),
        'by_kind': dict(collections.Counter(r.get('kind') for r in recs).most_common()),
        'by_source': dict(sorted(counters.items())),
        'quote_check': dict(collections.Counter(r['quote_check'] for r in recs).most_common()),
        'new_vs_existing': dict(collections.Counter(str(r.get('is_new')) for r in recs)),
    }
    json.dump(summ, open(OUT / 'merge_summary.json', 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
