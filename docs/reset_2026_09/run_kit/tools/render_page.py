"""Render one page of a corpus source to PNG for visual checking.
Usage: python render_page.py <SOURCE_ID> <page> [dpi] [--clip x0,y0,x1,y1 (fractions)]
Writes $VKM_WORK/run/img/<SOURCE_ID>_p<page>_<dpi>.png and prints the path.
Roots come from VKM_RESOURCES_ROOT (register + registered files) and VKM_WORK (see _roots.py).
For VKM-SRC-037 (djvu) page = djvu page (2-up spread). For VKM-SRC-023 page = page of the LibreOffice-rendered
$VKM_WORK/corpus/VKM-SRC-023/filatova.pdf."""
import sys, subprocess, pathlib, csv
import pymupdf
from _roots import root
RES, WORK = root('VKM_RESOURCES_ROOT'), root('VKM_WORK')
reg = {r['resource_id']: r['canonical_path'] for r in csv.DictReader(open(RES/'00_registry/SOURCE_REGISTER.csv', encoding='utf-8'))}
sid, page = sys.argv[1], int(sys.argv[2])
dpi = int(sys.argv[3]) if len(sys.argv) > 3 and not sys.argv[3].startswith('--') else 150
clip = None
if '--clip' in sys.argv:
    clip = [float(v) for v in sys.argv[sys.argv.index('--clip')+1].split(',')]
out = WORK / 'run' / 'img' / f'{sid}_p{page}_{dpi}{"_clip" if clip else ""}.png'
out.parent.mkdir(parents=True, exist_ok=True)
if sid == 'VKM-SRC-023':
    src = WORK / 'corpus' / 'VKM-SRC-023' / 'filatova.pdf'
else:
    src = RES / reg[sid]
if src.suffix == '.djvu':
    tmp = out.with_suffix('.pgm')
    subprocess.run(['ddjvu', '-format=pgm', f'-page={page}', f'-scale={dpi}', str(src), str(tmp)], check=True)
    from PIL import Image
    im = Image.open(tmp)
    if clip:
        w, h = im.size; im = im.crop((int(clip[0]*w), int(clip[1]*h), int(clip[2]*w), int(clip[3]*h)))
    im.save(out); tmp.unlink()
else:
    doc = pymupdf.open(src); pg = doc[page-1]
    r = pg.rect
    kw = {}
    if clip:
        kw['clip'] = pymupdf.Rect(r.x0 + clip[0]*r.width, r.y0 + clip[1]*r.height, r.x0 + clip[2]*r.width, r.y0 + clip[3]*r.height)
    pg.get_pixmap(dpi=dpi, **kw).save(out)
print(out)
