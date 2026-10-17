"""Export a separately labelled local absence branch from a registered profile.

The zero interval comes from the source interpretation. Its footprint width and
transition width are engineering choices, not surveyed boundaries. These are
contact TINs; no collapsed wedge is advertised as a finite-element volume.
"""
import argparse
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial import Delaunay
from shapely.geometry import LineString, Point, mapping

from build_engineering_geology import REPO, read, save, sha
from vkm_world.geometry_build.column_display import TINSurface


def write_vtp(path,xy,z,triangles):
    root=ET.Element("VTKFile",type="PolyData",version="1.0",byte_order="LittleEndian")
    poly=ET.SubElement(root,"PolyData")
    field=ET.SubElement(poly,"FieldData")
    for name,value in {"GeometryRole":"ENGINEERING_PINCHOUT_BRANCH_CONTACT_ONLY","Source":"VKM-SRC-048 p85 fig2.58; SOURCE012 depth field","VerticalTie":"DSM_COLLAR_ASSUMPTION","HorizontalFrame":"SH1_ENGINEERING_REGISTRATION","LengthUnit":"m","HeightFrame":"DSM_EGM2008_ANCHORED_DEPTH_MODEL","ContactName":path.stem}.items():
        data=ET.SubElement(field,"Array",type="String",Name=name,NumberOfTuples="1",format="ascii")
        data.text=" ".join(map(str,value.encode()+b"\0"))
    piece=ET.SubElement(poly,"Piece",NumberOfPoints=str(len(xy)),NumberOfPolys=str(len(triangles)),NumberOfVerts="0",NumberOfLines="0",NumberOfStrips="0")
    points=ET.SubElement(ET.SubElement(piece,"Points"),"DataArray",type="Float64",NumberOfComponents="3",format="ascii")
    points.text=" ".join(format(v,".15g") for v in np.column_stack([xy,z]).ravel())
    polys=ET.SubElement(piece,"Polys")
    cells=ET.SubElement(polys,"DataArray",type="Int64",Name="connectivity",format="ascii");cells.text=" ".join(map(str,triangles.ravel()))
    offsets=ET.SubElement(polys,"DataArray",type="Int64",Name="offsets",format="ascii");offsets.text=" ".join(map(str,np.arange(1,len(triangles)+1)*3))
    ET.ElementTree(root).write(path,encoding="utf-8",xml_declaration=True)


def run(args):
    out=args.output.resolve()
    if out.exists() or not out.is_relative_to((REPO/"work").resolve()):raise ValueError("new ignored output directory required")
    inputs={"field":args.field,"registration":args.registration,"builder":Path(__file__),"helpers":REPO/"scripts/build_engineering_geology.py","TIN_operator":REPO/"src/vkm_world/geometry_build/column_display.py"}
    pins={k:sha(p) for k,p in inputs.items()}
    registration=read(args.registration);data=np.load(args.field)
    endpoints=np.array(registration["pinchout_segment"]["sh1_endpoints"])
    axis=endpoints[1]-endpoints[0];length=np.linalg.norm(axis);unit=axis/length;normal=np.array([-unit[1],unit[0]])
    # Fine local grid brackets the registered interpreted interval exactly.
    s=np.unique(np.r_[np.arange(-200,length+205,5),0,length]);t=np.arange(-200,205,5)
    along,across=np.meshgrid(s,t,indexing="ij")
    xy=endpoints[0]+along.reshape(-1,1)*unit+across.reshape(-1,1)*normal
    tri=Delaunay(np.column_stack([along.ravel(),across.ravel()])).simplices
    base=np.column_stack([TINSurface(np.column_stack([data["xy"],data["contacts"][:,k]]),data["triangles"]).sample(xy) for k in range(6)])
    if not np.isfinite(base).all():raise ValueError("local branch outside base support")
    out.mkdir(parents=True);records=[];features=[]
    for width in (50.,100.):
        core=LineString(endpoints).buffer(width,cap_style=2)
        distance=np.array([core.distance(Point(p)) for p in xy])
        factor=np.minimum(distance/50.,1.)
        branch=base.copy();branch[:,2]=base[:,3]+(base[:,2]-base[:,3])*factor
        zero=factor<1e-10
        if not zero.any() or np.any(np.diff(branch,axis=1)>1e-9):raise ValueError("invalid pinchout contacts")
        name=f"halfwidth_{int(width)}m";folder=out/name;folder.mkdir()
        np.savez_compressed(folder/"contact_fields.npz",xy=xy,triangles=tri,contacts=branch,base_contacts=base,absence_mask=zero)
        names=["TKT","SMT","PKS","SKZ","SZ","PDKS"]
        for k,contact in enumerate(names):write_vtp(folder/(contact+".vtp"),xy,branch[:,k],tri)
        root=ET.Element("VTKFile",type="vtkMultiBlockDataSet",version="1.0",byte_order="LittleEndian");blocks=ET.SubElement(root,"vtkMultiBlockDataSet")
        for k,contact in enumerate(names):ET.SubElement(blocks,"DataSet",index=str(k),name=contact,file=contact+".vtp")
        ET.ElementTree(root).write(folder/"contacts.vtm",encoding="utf-8",xml_declaration=True)
        record={"halfwidth_m":width,"transition_width_m":50,"width_status":"ENGINEERING_ASSUMPTION_SENSITIVITY","zero_thickness_nodes":int(zero.sum()),
                "negative_thickness_nodes":int(np.sum(np.any(np.diff(branch,axis=1)>0,axis=1))),"source_zero_residual_m":float(np.max(abs(branch[zero,2]-branch[zero,3]))),
                "volume_mesh":"NOT_BUILT; contact surfaces include exact zero thickness","semantic_branch":"ABSENT_THROUGHOUT_296_392; transition-zone semantic alternative retained in evidence"}
        records.append(record);features.append({"type":"Feature","geometry":mapping(core),"properties":record})
    save(out/"absence_scenarios.geojson",{"type":"FeatureCollection","name":"SH1_LOCAL_ENGINEERING_BRANCH","features":features})
    save(out/"qa.json",{"branches":records,"base_model_changed":False,"interpretation":"Source-local pinchout honored as separate scenario; cannot be inferred as a measured 3D absence boundary"})
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    center=np.isclose(across.ravel(),0);order=np.argsort(along.ravel()[center]);p=along.ravel()[center][order]
    fig,ax=plt.subplots(figsize=(11,5),layout="constrained")
    ax.plot(p,base[center,2][order],"--",color="#a77c9f",label="ПКС: продолжение общего поля")
    ax.plot(p,branch[center,2][order],color="#77346c",label="ПКС: ветка выклинивания")
    ax.plot(p,branch[center,3][order],color="#2f637f",label="Кровля СКЗ")
    ax.axvspan(0,length,color="#e6c982",alpha=.3,label="Зарегистрированный интервал ПК 296–392")
    ax.set_xlabel("Расстояние вдоль зарегистрированной линии от ПК 296, м")
    ax.set_ylabel("Z, м; инженерная привязка общего поля к DSM")
    ax.set_title("Локальная альтернатива: нулевая мощность покровной каменной соли\nИсточник: VKM-SRC-048, с. 85, рис. 2.58; ширина зоны 50/100 м — сценарий")
    ax.grid(alpha=.25);ax.legend(fontsize=9);fig.savefig(out/"pinchout_section.png",dpi=180);plt.close(fig)
    if pins!={k:sha(p) for k,p in inputs.items()}:raise RuntimeError("input changed")
    save(out/"receipt.json",{"schema":"vkm.pinchout_contact_branch/1","command":sys.argv,"inputs":{k:{"path":p.resolve().relative_to(REPO).as_posix(),"sha256":pins[k]} for k,p in inputs.items()},
                             "outputs_sha256":{p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()}})
    print(records)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for name in ("field","registration","output"):parser.add_argument("--"+name,type=Path,required=True)
    run(parser.parse_args())
