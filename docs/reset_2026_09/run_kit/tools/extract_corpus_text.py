"""Per-page text-layer extraction of every registered source (as run in the cloud VM 26.09.2026).

Usage: python extract_corpus_text.py [<RESOURCES_ROOT> <OUT_DIR>]
Without arguments: RESOURCES_ROOT = $VKM_RESOURCES_ROOT, OUT_DIR = $VKM_WORK/corpus (see _roots.py).
Writes <OUT_DIR>/<SOURCE_ID>/pNNNN.txt, all.txt, and extraction_summary.json.
docx (VKM-SRC-023) -> pandoc plain text + media; render to PDF separately with LibreOffice (see SWEEP_PROTOCOL.md).
DjVu (VKM-SRC-037) and scanned VKM-SRC-025 need OCR (ocr_source_pages.py in the resources repo).
"""
import csv, json, pathlib, subprocess, sys
import pymupdf

if len(sys.argv) == 3:
    root, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
else:
    from _roots import root as env_root
    root, out = env_root('VKM_RESOURCES_ROOT'), env_root('VKM_WORK') / 'corpus'
out.mkdir(parents=True, exist_ok=True)
reg = list(csv.DictReader(open(root / '00_registry/SOURCE_REGISTER.csv', encoding='utf-8')))
summary = {}
for r in reg:
    p, sid = root / r['canonical_path'], r['resource_id']
    if not p.exists():
        summary[sid] = {'path': r['canonical_path'], 'status': 'MISSING'}
        continue
    d = out / sid
    d.mkdir(exist_ok=True)
    if p.suffix == '.pdf':
        doc = pymupdf.open(p)
        chars = []
        with open(d / 'all.txt', 'w', encoding='utf-8') as fa:
            for i, pg in enumerate(doc, 1):
                t = pg.get_text()
                (d / f'p{i:04d}.txt').write_text(t, encoding='utf-8')
                fa.write(f'\n\n===== PDF PAGE {i} =====\n' + t)
                chars.append(len(t.strip()))
        summary[sid] = {'path': r['canonical_path'], 'pages': len(doc), 'pages_text': sum(c > 40 for c in chars),
                        'chars': sum(chars), 'images': sum(len(pg.get_images()) for pg in doc)}
    elif p.suffix == '.docx':
        subprocess.run(['pandoc', str(p), '-t', 'plain', '--wrap=none', '-o', str(d / 'all.txt'), f'--extract-media={d}/media'], check=True)
        subprocess.run(['pandoc', str(p), '-t', 'gfm', '--wrap=none', '-o', str(d / 'all.md')], check=True)
        summary[sid] = {'path': r['canonical_path'], 'chars': len((d / 'all.txt').read_text(encoding='utf-8'))}
    else:
        summary[sid] = {'path': r['canonical_path'], 'status': 'NEEDS_OCR_' + p.suffix}
json.dump(summary, open(out / 'extraction_summary.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1))
