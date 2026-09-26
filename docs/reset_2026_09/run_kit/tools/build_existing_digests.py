"""Per-source digest of the previous evidence release (claims.jsonl) for sweep readers.
Usage: python build_existing_digests.py [<RESOURCES_ROOT> <OUT_DIR>]   -> <OUT_DIR>/<SOURCE_ID>.tsv
Without arguments: RESOURCES_ROOT = $VKM_RESOURCES_ROOT, OUT_DIR = $VKM_WORK/run/existing (see _roots.py)."""
import collections, json, pathlib, sys
if len(sys.argv) == 3:
    root, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
else:
    from _roots import root as env_root
    root, out = env_root('VKM_RESOURCES_ROOT'), env_root('VKM_WORK') / 'run' / 'existing'
out.mkdir(parents=True, exist_ok=True)
recs = [json.loads(l) for l in open(root / '10_physics_evidence/physical_evidence_v1/claims.jsonl', encoding='utf-8')]
by = collections.defaultdict(list)
for r in recs:
    by[r['source_id']].append(r)
cols = ['evidence_id', 'pdf_page', 'docx_locator', 'physical_variable', 'original_value', 'original_unit', 'evidence_type', 'evidence_scope', 'withdrawn']
for sid, rs in by.items():
    with open(out / f'{sid}.tsv', 'w', encoding='utf-8') as f:
        f.write('\t'.join(cols + ['claim']) + '\n')
        for r in sorted(rs, key=lambda r: (r.get('pdf_page') or 0)):
            vals = [r.get(c) for c in cols] + [(r.get('claim') or '')[:220]]
            f.write('\t'.join(str(x).replace('\t', ' ').replace('\n', ' ') for x in vals) + '\n')
print({k: len(v) for k, v in sorted(by.items())})
