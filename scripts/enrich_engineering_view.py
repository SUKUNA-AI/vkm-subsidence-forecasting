"""Add mapped context and source-support boundaries to an engineering delivery.

Source plans are draped only for display; they never become a seam or an
excavation volume. Every output is pinned to the unchanged geometry receipt.
"""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import re
import shutil
import sys

import numpy as np
from shapely.geometry import MultiPoint, Point, shape
from shapely.prepared import prep

from build_engineering_geology import REPO, decode, encode, js, read, save, sha
from vkm_world.geometry_build.column_display import TINSurface


def load_js(path):
    return json.loads(path.read_text(encoding="utf-8").split("=",1)[1].strip().removesuffix(";"))


def replace_exact(text,old,new,expected=1):
    if text.count(old)!=expected:
        raise ValueError("viewer contract changed: "+old[:80])
    return text.replace(old,new)


def run(args):
    out=args.output.resolve()
    if not out.is_relative_to((REPO/"work").resolve()) or out.exists():
        raise ValueError("new output directory under ignored work required")
    base_receipt=read(args.model/"receipt.json")
    for name,digest in base_receipt["outputs_sha256"].items():
        if sha(args.model/name)!=digest:raise ValueError("changed model input: "+name)
    inputs={"model_receipt":args.model/"receipt.json","context":args.context,"builder":Path(__file__),
            "helpers":REPO/"scripts/build_engineering_geology.py","TIN_operator":REPO/"src/vkm_world/geometry_build/column_display.py"}
    pins={key:sha(path) for key,path in inputs.items()}
    shutil.copytree(args.model,out)
    for name,digest in base_receipt["outputs_sha256"].items():
        if sha(out/name)!=digest:raise ValueError("copied model failed integrity: "+name)
    shutil.move(out/"receipt.json",out/"geometry_receipt.json")
    scene_dir=out/"FULL_CONTEXT"
    scene=load_js(scene_dir/"data/scene.js")
    objects=load_js(scene_dir/"data/spatial_objects.js")
    original=load_js(args.context)
    rows=read(out/"mapped_boreholes.json")
    txy=decode(scene["terrain_xy"]).reshape(-1,2);tz=decode(scene["terrain_raw_z"]).ravel()
    ttri=decode(scene["terrain_triangles"]).reshape(-1,3)
    terrain=TINSurface(np.column_stack([txy,tz]),ttri)
    fullrows=[r for r in rows if all(v is not None for v in r["depths_m"])]
    hull=MultiPoint([[r["x"],r["y"]] for r in fullrows]).convex_hull
    hull_prepared=prep(hull)
    scene["terrain_support"]=encode([int(hull_prepared.covers(Point(p))) for p in txy],"i4")
    scene["meta"]["frame_label"]="X / Y: SH1, м · Z: глубины + DSM EGM2008 (2011–2015), инженерная гипотеза"
    counts={"mapped_depth_columns":len(rows),"complete_columns":len(fullrows),"context_plans":0,"plan_paths_outside_DSM":0,"map_only_symbols":0}
    objects["boreholes"]=[]
    objects["lines"]=[]
    for row in rows:
        well={"id":row["physical_id"],"label":row["physical_id"].removeprefix("BH-SOL-"),"x":row["x"],"y":row["y"],"display_z":row["collar_scenario_m"],
              "status":"ENGINEERING_ASSUMPTION","source_id":"VKM-SRC-012 p64–65","display_anchor_status":"Устье по DSM, глубины из источника; XY по регистрации схемы","scope":row["scope"]}
        objects["boreholes"].append(well)
        depths=[v for v in row["depths_m"] if v is not None]
        if depths:
            pts=[[row["x"],row["y"],row["collar_scenario_m"]],[row["x"],row["y"],row["collar_scenario_m"]-max(depths)]]
            objects["lines"].append({**well,"group":"borehole","paths":[{"points":encode(pts),"delta":encode([0,0])}]})
    mapped_ids={r["physical_id"] for r in rows}
    for old in original.get("boreholes",[]):
        if old.get("catalogue_id") in mapped_ids:continue
        z=terrain.sample([[old["x"],old["y"]]])[0]
        if not np.isfinite(z):continue
        row={**old,"display_z":float(z),"display_anchor_status":"Знак плана на исходной DSM; глубина и отметка устья UNKNOWN"}
        objects["boreholes"].append(row);counts["map_only_symbols"]+=1
    plan_geometry=[]
    for old in original.get("lines",[]):
        if not old.get("geometry_xy"):continue
        geometry=shape(old["geometry_xy"])
        parts=list(geometry.geoms) if geometry.geom_type.startswith("Multi") else [geometry]
        paths=[]
        for part in parts:
            rings=[part.exterior,*part.interiors] if part.geom_type=="Polygon" else [part]
            for ring in rings:
                xy=np.array(ring.coords)[:,:2]
                z=terrain.sample(xy)
                if not np.isfinite(z).all():
                    counts["plan_paths_outside_DSM"]+=1;continue
                paths.append({"points":encode(np.column_stack([xy,z])),"delta":encode(np.zeros(len(xy)))})
                plan_geometry.append(xy)
        if paths:
            objects["lines"].append({**old,"paths":paths,"z_status":"DISPLAY_ONLY_PLAN_ON_DSM; seam elevations unknown",
                                    "metadata":{**old.get("metadata",{}),"geometry_role":"XY_PLAN_CONTEXT_NOT_EXCAVATION","display_delta_applied":False}})
            counts["context_plans"]+=1
    support_xy=np.array(hull.exterior.coords)
    # Dense segments follow the actual DSM instead of bridging relief linearly.
    dense=[]
    for a,b in zip(support_xy[:-1],support_xy[1:]):
        dense.extend(a+(b-a)*t for t in np.linspace(0,1,max(2,int(np.linalg.norm(b-a)/30)),endpoint=False))
    dense=np.vstack([dense,support_xy[0]])
    objects["lines"].append({"id":"source_support_boundary","group":"support_boundary","status":"DERIVATION","name":"Граница геометрического охвата 14 полных колонок",
                            "z_status":"DISPLAY_ONLY_MASK","paths":[{"points":encode(np.column_stack([dense,terrain.sample(dense)])),"delta":encode(np.zeros(len(dense)))}]})
    objects["events"]=original.get("events",[])
    objects["context_counts"]=counts
    js(scene_dir/"data/scene.js","VKM_SCENE",scene)
    js(scene_dir/"data/spatial_objects.js","VKM_SPATIAL_OBJECTS",objects)
    viewer=(scene_dir/"unified_world_viewer.js").read_text(encoding="utf-8")
    viewer=replace_exact(viewer,"let target = new T.Vector3(), radius = 31", "function fitRadius(){return Math.max(31,14/(Math.tan(21*Math.PI/180)*Math.min(host.clientWidth/host.clientHeight,1)));}\n    let target = new T.Vector3(), radius = fitRadius()")
    viewer=replace_exact(viewer,"radius=31;", "radius=fitRadius();",2)
    viewer=replace_exact(viewer,"const meshes=new Map(),objectMaterials=[];", "const meshes=new Map(),objectMaterials=[],planMaterials=[];")
    viewer=viewer.replace("const a=new T.Color('#557c58')", "const support=D.terrain_support?decode(D.terrain_support):null;const a=new T.Color('#557c58')")
    viewer=viewer.replace("c.toArray(colors,i*3);", "if(support&&!support[i])c.lerp(new T.Color('#c5c2b3'),.48);c.toArray(colors,i*3);")
    viewer=viewer.replace("color:data.group==='plan_outline'?'#273e4c':'#315c75'", "color:data.group==='support_boundary'?'#b15219':data.group==='plan_outline'?'#273e4c':'#315c75',depthTest:data.group==='borehole'")
    viewer=viewer.replace("line.userData=data;groups.plans.add(line);", "line.userData=data;line.renderOrder=data.group==='support_boundary'?18:12;(data.group==='borehole'?groups.wells:groups.plans).add(line);")
    viewer=viewer.replace("line.userData=data;", "planMaterials.push(line.material);line.userData=data;")
    viewer=replace_exact(viewer,"clipMaterials(shellMaterials,clipping,mode==='quarter');", "clipMaterials(shellMaterials,clipping,mode==='quarter');clipMaterials(planMaterials,clipping,mode==='quarter');")
    viewer=viewer.replace("markers(O.boreholes||[],groups.wells,'#352066',7);", "markers((O.boreholes||[]).filter(r=>r.object_kind==='GRAPHICAL_PLAN_SYMBOL'),groups.wells,'#667b87',5);markers((O.boreholes||[]).filter(r=>r.object_kind!=='GRAPHICAL_PLAN_SYMBOL'),groups.wells,'#352066',8);")
    (scene_dir/"unified_world_viewer.js").write_text(viewer,encoding="utf-8")
    page=(scene_dir/"index.html").read_text(encoding="utf-8")
    page=page.replace('<option value="all">FULL_CONTEXT</option>','<option value="all">Вся область модели</option>')
    page=page.replace('<option value="mine">Выработки внутри массива</option>','')
    page=page.replace('value="1" selected','value="1"').replace('value="3">','value="3" selected>')
    page=page.replace('Полная область исходной модели: контакты по скважинам, исходная DSM и инженерная высотная привязка.', 'Полный охват 127,75 км² · геология, рельеф, планы и скважины · вертикальный масштаб можно переключить на 1 : 1.')
    for id_ in ("chambers","seams","recent","localstrata"):
        page=re.sub(r'<label><input id="'+id_+r'".*?</label>',lambda m:'<span hidden>'+m.group(0).replace(' checked','')+'</span>',page)
    page=re.sub(r'<small>Камеры показывают.*?</small>', '<small>Планы показаны на поверхности для ориентирования. Их положение по глубине и фактические объёмы выемки этим не задаются.</small>',page)
    page=re.sub(r'<label>Спутниковые высоты.*?</label>', lambda m:'<span hidden>'+m.group(0)+'</span>',page)
    page=page.replace('<fieldset><legend>Стратиграфия</legend>', '<div class="notice"><b>Покрытие:</b> коричневая линия — охват полных колонок; приглушённая поверхность снаружи — инженерное продолжение. Фиолетовые знаки — колонки с глубинами; серые — знаки исходных планов.</div><fieldset><legend>Стратиграфия</legend>')
    page=page.replace('<div class="links">','<div class="links"><a href="../map.html">Карта привязки и скважин</a><a href="../section_full_ru.png">Общий геологический разрез</a><a href="../mapped_boreholes.csv">Таблица привязки скважин</a>')
    (scene_dir/"index.html").write_text(page,encoding="utf-8")
    (out/"index.html").write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=FULL_CONTEXT/index.html"><a href="FULL_CONTEXT/index.html">Открыть полную модель</a>',encoding="utf-8")
    render_map(out,txy,tz,ttri,rows,objects["boreholes"],plan_geometry,hull)
    render_section(out,read(scene_dir/"sections.json"))
    (out/"map.html").write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Привязка скважин</title><style>body{font:16px Arial;margin:24px;background:#f4f6f7;color:#223746}img{max-width:100%;height:auto}a{color:#28617c}</style><a href="FULL_CONTEXT/index.html">← Полная 3D-модель</a><h1>Карта привязки скважин и планов</h1><p>Координаты SH1 получены регистрацией схем. Отметки устьев по DSM — инженерное допущение. Источник и неопределённость каждой колонки сохранены в таблице.</p><a href="mapped_boreholes.csv">Скачать таблицу</a><br><img src="map.png"></html>',encoding="utf-8")
    fields=["physical_id","scope","x","y","collar_scenario_m","terrain_min_m","terrain_max_m","xy_status"]
    with (out/"mapped_boreholes.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields+["depths_m","role"]);writer.writeheader()
        for r in rows:writer.writerow({**{k:r.get(k) for k in fields},"depths_m":json.dumps(r["depths_m"]),"role":"ENGINEERING_XY_DSM_COLLAR; source depths unchanged"})
    save(out/"context_summary.json",{**counts,"full_area_m2":read(scene_dir/"qa.json")["area_m2"],"source_hull_area_m2":hull.area,
                                    "plan_height_role":"DISPLAY_ONLY_ON_DSM","geology_geometry_changed":False})
    if pins!={key:sha(path) for key,path in inputs.items()}:raise RuntimeError("input changed during enrichment")
    save(out/"receipt.json",{"schema":"vkm.engineering_context/1","inputs":{k:{"path":p.resolve().relative_to(REPO).as_posix(),"sha256":pins[k]} for k,p in inputs.items()},
                             "outputs_sha256":{p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()},"command":sys.argv})
    print(json.dumps(counts))


def render_map(out,xy,z,tri,rows,markers,plans,hull):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.patches import Polygon as Patch
    fig,ax=plt.subplots(figsize=(12,10))
    fig.subplots_adjust(bottom=.14,top=.91,left=.10,right=.90)
    image=ax.tripcolor(xy[:,0],xy[:,1],tri,z,cmap="terrain",rasterized=True,alpha=.72)
    ax.add_collection(LineCollection(plans,colors="#263e4c",linewidths=.35,alpha=.75))
    ax.add_patch(Patch(np.array(hull.exterior.coords),facecolor="#ffffff",alpha=.22,edgecolor="#a3481e",linewidth=2,hatch="//",label="Охват 14 полных колонок (геометрический)"))
    other=[r for r in markers if r.get("object_kind")=="GRAPHICAL_PLAN_SYMBOL"]
    ax.scatter([r["x"] for r in other],[r["y"] for r in other],s=12,c="#4f6c7c",marker="+",label="Знаки планов; глубины неизвестны")
    colors={"SKRU1":"#512180","SKRU1_SKRU2_PILLAR":"#a13e28","SKRU2":"#163d87"}
    scope_names={"SKRU1":"СКРУ-1","SKRU1_SKRU2_PILLAR":"Целик между СКРУ-1 и СКРУ-2","SKRU2":"СКРУ-2"}
    for scope,color in colors.items():
        selected=[r for r in rows if r["scope"]==scope]
        ax.scatter([r["x"] for r in selected],[r["y"] for r in selected],s=34,c=color,edgecolors="white",linewidths=.5,zorder=5,label=scope_names[scope])
        for r in selected:ax.annotate(r["physical_id"].removeprefix("BH-SOL-"),(r["x"],r["y"]),xytext=(4,4),textcoords="offset points",fontsize=7,color=color)
    ax.set_aspect("equal");ax.set_xlabel("X (восток), м; локальная система SH1");ax.set_ylabel("Y (север), м; начало у ствола № 1")
    ax.set_title("ВКМ: полная область модели, планы и зарегистрированные скважины\nЗа охватом колонок — инженерное продолжение геологии",fontsize=13)
    ax.grid(alpha=.2);ax.legend(loc="upper left",fontsize=8,framealpha=.94)
    fig.colorbar(image,ax=ax,shrink=.72,label="DSM GLO-30, м EGM2008; съёмка 2011–2015")
    fig.text(.10,.025,"Источники: VKM-SRC-012, табл. 3.1; VKM-SRC-014, рис. 3.1; Copernicus GLO-30.\nXY — регистрация схем; устья по DSM — инженерное допущение. Сборка 17.10.2026.",fontsize=8)
    fig.savefig(out/"map.png",dpi=180,bbox_inches="tight");plt.close(fig)


def render_section(out,data):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    distance=np.array(data["distance_m"]);z=np.array(data["new_contact_z_m"],float)
    names=["Кровля ТКТ","Кровля СМТ","Кровля ПКС","Кровля СКЗ","Кровля СЗ","Кровля ПдКС"]
    colors=["#b9b382","#c2d0b2","#d6a7bd","#b5a6ce","#e7cda6"]
    fig,ax=plt.subplots(figsize=(12,5),layout="constrained")
    for k in range(5):ax.fill_between(distance,z[:,k+1],z[:,k],color=colors[k],alpha=.8)
    for k,name in enumerate(names):ax.plot(distance,z[:,k],lw=1.2,label=name)
    ax.set_xlabel("Расстояние вдоль ломаной через опорные скважины, м")
    ax.set_ylabel("Z, м; инженерная привязка к DSM EGM2008")
    ax.set_title("Общий разрез по 14 полным колонкам\nГлубины источника сохранены; DSM-отметки устьев приняты как инженерная гипотеза")
    ax.grid(alpha=.25);ax.legend(ncol=3,fontsize=8)
    fig.savefig(out/"section_full_ru.png",dpi=180);plt.close(fig)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for name in ("model","context","output"):parser.add_argument("--"+name,type=Path,required=True)
    run(parser.parse_args())
