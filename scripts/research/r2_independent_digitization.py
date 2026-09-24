# R2 independent digitization v2: category centres from tick marks; value = median y of series colour
# in a +/-2 px column band at the category centre, converted via a least-squares gridline fit.
import numpy as np, json
from PIL import Image
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "work/r2_adversarial_audit/digitization/r2_independent_digitization_v2.json"
import hashlib
INPUT_HASHES = {'data/published_figure_digitization_v1/source_profile_1_native.png': '254b82c1eb8aecb1dbacd071257cbade8d69bfca628367af6f067bafc114ea97', 'data/published_figure_digitization_v1/source_profile_5_native.png': '7ccbcd0bc9300472354f1e53cdc54851e682998be9d97b3463a8de6567936fb2', 'data/published_figure_digitization_v1/source_profile_17_native.png': 'bfef815b174e50db7add90a0debca2d9f4c06e2c6fdc0fded600a5e5b558eef0', 'data/published_figure_digitization_v1/source_profile_6_native.png': '3d68aef28ce7c2398a2bed91cb6894bdcd95d7d0f894ede10b57e2ae2bcdf325'}
for relative, expected in INPUT_HASHES.items():
    with (ROOT / relative).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"R2 input hash mismatch: {relative}")
OUT.parent.mkdir(parents=True, exist_ok=True)

charts={157:("1",[0,-50,-100,-150,-200,-250,-300,-350,-400],[91,133,175,216,258,300,342,384,426],14),
        159:("5",[0,-50,-100,-150,-200,-250,-300,-350],[91,139,187,234,282,330,378,426],24),
        161:("17",list(range(0,-201,-20)),[91+33.5*i for i in range(11)],11),
        163:("6",[10,0,-10,-20,-30,-40,-50,-60],[91,153,214,276,338,400,462,524],31)}
cols={"blue":((55,105,165),(100,145,205)),"red":((165,55,50),(210,100,95)),"green":((135,165,65),(170,200,105)),"purple":((105,80,140),(145,115,180))}
out={}
for o,(line,vals,rows,n) in charts.items():
    a=np.array(Image.open(ROOT / f"data/published_figure_digitization_v1/source_profile_{line}_native.png").convert("RGB")).astype(int)
    p=np.polyfit(rows,vals,1)
    x0,x1=100,875; step=(x1-x0)/n
    centers=[x0+step*(i+0.5) for i in range(n)]
    res={}
    for cn,(lo,hi) in cols.items():
        m=np.all((a>=lo)&(a<=hi),axis=2)
        ser=[]
        for c in centers:
            band=m[60:int(rows[-1])+12, int(round(c))-2:int(round(c))+3]
            ys=np.where(band.any(1))[0]+60
            if len(ys)<3: ser.append(None); continue
            # take largest contiguous cluster of ys
            groups=np.split(ys,np.where(np.diff(ys)>2)[0]+1)
            gbest=max(groups,key=len)
            ser.append(round(float(np.polyval(p,np.median(gbest))),1) if len(gbest)>=4 else None)
        res[cn]=ser
    out[line]={"mm_per_px":float(p[0]),"series":res}
OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
for l,r in out.items():
    print("LINE",l,"mm/px",round(r["mm_per_px"],3))
    for cn,s in r["series"].items(): print("  ",cn,s)
