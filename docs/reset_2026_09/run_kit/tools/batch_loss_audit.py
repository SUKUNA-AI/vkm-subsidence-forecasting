"""Detect possibly lost sweep batches (scratch-script collision incident, decision D-11).

For every chunk: pages listed as done/read vs pages that actually carry records. A page with zero
records but high domain-vocabulary density is flagged SUSPECT_LOST_BATCH (to be re-read).
Pages with no records and low density are NORMAL_EMPTY. Output: <merged dir>/batch_loss_audit.csv
"""
import csv, json, pathlib, re, collections, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _roots import merged_dir, root, sweep_dir  # noqa: E402
SWEEP = sweep_dir()
CORPUS = root('VKM_WORK') / 'corpus'
OCR = root('VKM_RESOURCES_ROOT') / 'work' / 'ocr'
VOCAB = re.compile(r'скважин|пласт|целик|камер|закладк|оседани|сдвижен|нивелир|репер|напряжен|ползуч|прочност|модул|плотност|'
                   r'георадар|сейсм|рассол|водонос|температур|мощност|отметк|панел|блок|формул|табл|рис\.', re.I)


def page_text(sid, p):
    best = ''
    for c in (CORPUS / sid / f'p{p:04d}.txt', OCR / sid / f'p{p:04d}.txt'):
        if c.exists():
            t = c.read_text(encoding='utf-8', errors='replace')
            if len(t.strip()) > len(best.strip()):
                best = t
    return best


def expand(v):
    out = set()
    for p in v or []:
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


rows = []
for d in sorted(SWEEP.glob('*/*/')):
    sid, cid = d.parent.name, d.name
    done = set()
    for f in ('progress.json', 'coverage.json'):
        if (d / f).exists():
            try:
                j = json.loads((d / f).read_text(encoding='utf-8'))
            except Exception:  # noqa: BLE001
                continue
            done |= expand(j.get('pages_done')) | expand(j.get('pages_read_fully'))
            skim = expand(j.get('pages_skimmed'))
    per_page = collections.Counter()
    if (d / 'records.jsonl').exists():
        for line in (d / 'records.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(line)
                per_page[int(r.get('pdf_page'))] += 1
            except Exception:  # noqa: BLE001
                pass
    for p in sorted(done):
        if per_page.get(p, 0):
            continue
        hits = len(VOCAB.findall(page_text(sid, p)))
        rows.append({'source_id': sid, 'chunk': cid, 'page': p, 'vocab_hits': hits,
                     'verdict': 'SUSPECT_LOST_BATCH' if hits >= 12 else 'NORMAL_EMPTY'})
out = merged_dir() / 'batch_loss_audit.csv'
out.parent.mkdir(parents=True, exist_ok=True)
with open(out, 'w', encoding='utf-8', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['source_id', 'chunk', 'page', 'vocab_hits', 'verdict'])
    w.writeheader(); w.writerows(rows)
c = collections.Counter(r['verdict'] for r in rows)
print(dict(c))
sus = collections.defaultdict(list)
for r in rows:
    if r['verdict'] == 'SUSPECT_LOST_BATCH':
        sus[(r['source_id'], r['chunk'])].append(r['page'])
for k, v in sorted(sus.items()):
    print(k, v)
