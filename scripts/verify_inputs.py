#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, hashlib, json
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(root: Path, manifest: Path) -> list[dict]:
    rows=[]
    with manifest.open('r', encoding='utf-8-sig', newline='') as f:
        for rec in csv.DictReader(f):
            rel = rec['relative_path']
            path = root / rel
            expected_size = int(rec['size_bytes'])
            expected_hash = rec['sha256']
            exists = path.is_file()
            actual_size = path.stat().st_size if exists else None
            actual_hash = sha256(path) if exists else None
            status = 'PASS' if exists and actual_size == expected_size and actual_hash == expected_hash else 'FAIL'
            rows.append({
                'relative_path': rel,
                'exists': exists,
                'expected_size': expected_size,
                'actual_size': actual_size,
                'expected_sha256': expected_hash,
                'actual_sha256': actual_hash,
                'status': status,
            })
    return rows


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root', default='.')
    args=p.parse_args()
    root=Path(args.root).resolve()
    all_rows=[]
    for rel in ['configs/input_manifest.csv','configs/source_manifest.csv']:
        all_rows.extend(verify_manifest(root, root/rel))
    failed=[r for r in all_rows if r['status']!='PASS']
    report={'root':str(root),'checked':len(all_rows),'passed':len(all_rows)-len(failed),'failed':len(failed),'results':all_rows}
    out=root/'INPUT_VERIFICATION.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['checked','passed','failed']}, ensure_ascii=False))
    if failed:
        for r in failed:
            print('FAIL', r['relative_path'])
        raise SystemExit(1)

if __name__=='__main__':
    main()
