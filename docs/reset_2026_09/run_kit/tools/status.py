"""Progress of the per-chunk sweep (reads the sweep dir; see _roots.sweep_dir)."""
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _roots import sweep_dir  # noqa: E402
root = sweep_dir()
rows = []
for p in sorted(root.glob('*/*/progress.json')):
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:  # noqa: BLE001
        rows.append((p.parent.parent.name, p.parent.name, 'UNREADABLE', 0, 0)); continue
    done = (p.parent / 'coverage.json').exists()
    rows.append((p.parent.parent.name, p.parent.name, 'DONE' if done else d.get('status'), len(d.get('pages_done') or []), d.get('n_records')))
for r in rows:
    print(f'{r[0]:<14}{r[1]:<4}{str(r[2]):<12}{r[3]:>4}p {r[4]} rec')
print('chunks with progress:', len(rows), 'done:', sum(1 for r in rows if r[2] == 'DONE'))
