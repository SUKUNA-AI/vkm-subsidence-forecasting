"""Second-pass quote verification of the merged sweep records (quote_check_v2.csv).

The first pass (merge_sweep.py) labels each quote EXACT / FUZZY_nn / NOT_FOUND against the page text layer and OCR.
This pass refines records that were not found as one string:
  EXACT_SEGMENTS / PARTIAL_SEGMENTS   an elided quote ('...', '…', '[...]') whose segments (>= 12 chars) are all /
                                      at least half found on the page or its neighbours;
  VISUAL_NOT_IN_TEXT_LAYER            value read visually from a figure or a digitized graph (no text layer to match);
  OCR_TEXT_MISMATCH                   OCR-based extraction whose quote does not match the stored OCR text;
  NO_QUOTE                            quote shorter than 8 characters after normalization.
Everything else keeps its first-pass label. Deterministic; reads <merged dir>/all_records.jsonl and writes
<merged dir>/quote_check_v2.csv (see _roots.merged_dir).

Captured on 26.09.2026 from the coordinator's inline command of the Phase-1 cloud run (it produced the committed
PRIVATE 11_evidence_vnext/merged/quote_check_v2.csv); the logic is unchanged.
"""
import collections
import csv
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import merge_sweep as m  # noqa: E402  (norm, page_text; merge runs only under __main__)
from _roots import merged_dir  # noqa: E402


def check(r):
    q = m.norm(r.get('quote') or '')
    if len(q) < 8:
        return 'NO_QUOTE'
    base = r['quote_check']
    if base.startswith('EXACT') or base.startswith('FUZZY'):
        return base
    segs = [s.strip(' .') for s in re.split(r'\.\.\.|…|\[\.\.\.\]', q) if len(s.strip(' .')) >= 12]
    sid = r.get('source_id')
    try:
        p = int(r.get('pdf_page'))
    except (TypeError, ValueError):
        p = None
    if p and len(segs) > 1:
        txt = ' '.join(m.page_text(sid, x) for x in (p - 1, p, p + 1) if x > 0)
        found = sum(1 for s in segs if s in txt)
        if found == len(segs):
            return 'EXACT_SEGMENTS'
        if found >= max(1, len(segs) // 2):
            return 'PARTIAL_SEGMENTS'
    em = (r.get('extraction_method') or '')
    if em in ('VISUAL_READ', 'GRAPH_DIGITIZED_APPROX'):
        return 'VISUAL_NOT_IN_TEXT_LAYER'
    if em.startswith('OCR'):
        return 'OCR_TEXT_MISMATCH'
    return base


def main():
    merged = merged_dir()
    recs = [json.loads(line) for line in open(merged / 'all_records.jsonl', encoding='utf-8')]
    out, rows = collections.Counter(), []
    for r in recs:
        v = check(r)
        out[v] += 1
        rows.append((r['vn_id'], r['source_id'], r.get('pdf_page'), r.get('extraction_method'), r['quote_check'], v))
    with open(merged / 'quote_check_v2.csv', 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['vn_id', 'source_id', 'pdf_page', 'extraction_method', 'quote_check_v1', 'quote_check_v2'])
        w.writerows(rows)
    print({k: v for k, v in out.most_common() if not k.startswith('FUZZY')},
          'FUZZY', sum(v for k, v in out.items() if k.startswith('FUZZY')))


if __name__ == '__main__':
    main()
