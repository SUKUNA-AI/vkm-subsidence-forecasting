"""Split an OCR all.txt (markers '===== PDF_PAGE N (OCR) =====') back into pNNNN.txt page files.
Usage: python split_ocr_all.py <all.txt> <out_dir>"""
import pathlib, re, sys
src, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
parts = re.split(r'^===== PDF_PAGE (\d+) \(OCR\) =====\n?', src.read_text(encoding='utf-8'), flags=re.M)
n = 0
for i in range(1, len(parts), 2):
    (out / f'p{int(parts[i]):04d}.txt').write_text(parts[i + 1].rstrip('\n') + '\n', encoding='utf-8')
    n += 1
(out / 'all.txt').write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
print(f'{n} pages -> {out}')
