"""Inspectable SKRU-1 evidence geometry, with separate vertical reference frames.

Run with the QGIS Python runtime (numpy/scipy/shapely/matplotlib/plotly).
Outputs belong in ignored work/. No source or previously built package is edited.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import Delaunay
from shapely.geometry import shape
from shapely.ops import triangulate, unary_union

COLORS = {"Q":"#e6cb55", "ПЦТ":"#b3ca83", "ТКТ":"#d4a36e", "СМТ":"#8cb7ce",
          "ПП":"#b8adc8", "ПКС":"#d395ae", "СКЗ":"#da814f", "СЗ":"#9b6ea9",
          "ПдКС":"#a7b6bf", "МГ":"#5f7885", "ПдКС_под_МГ":"#839398"}
NAMES = {"Q":"Четвертичные", "ПЦТ":"Пестроцветная толща", "ТКТ":"Терригенно-карбонатная толща",
         "СМТ":"Соляно-мергельная толща", "ПП":"Переходная пачка", "ПКС":"Покровная каменная соль",
         "СКЗ":"Сильвинито-карналлитовая зона", "СЗ":"Сильвинитовая зона", "ПдКС":"Подстилающая каменная соль"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def csv_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def number(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [p for p in geometry.geoms if p.geom_type == "Polygon"]


def polygon_triangles(geometry):
    """No repair, convex-hull fill or triangles across holes/concave boundaries."""
    if not geometry.is_valid:
        raise ValueError("invalid source polygon: cannot create display envelope")
    triangles=[t for p in polygon_parts(geometry) for t in triangulate(p) if p.covers(t)]
    # An unconstrained Delaunay filtered by containment can omit concave pockets.
    # Refuse such a result rather than silently delivering an incomplete surface.
    covered=unary_union(triangles)
    if covered.symmetric_difference(geometry).area>max(1e-8,geometry.area*1e-9):
        raise ValueError("triangulation does not cover the complete source polygon")
    return [np.asarray(t.exterior.coords)[:3, :2] for t in triangles]


def mesh_at_z(geometry, z):
    triangles = polygon_triangles(geometry)
    points = np.concatenate([np.column_stack([t, np.full(3, z)]) for t in triangles])
    faces = np.arange(len(points)).reshape(-1, 3)
    return points, faces


def vtp(path, points, faces):
    """Portable ASCII PolyData; accompanying feature JSON carries all provenance."""
    pts = " ".join(format(float(x), ".12g") for x in np.asarray(points).flat)
    conn = " ".join(str(int(x)) for x in np.asarray(faces).flat)
    offsets = " ".join(str(3*(i+1)) for i in range(len(faces)))
    Path(path).write_text(f'<?xml version="1.0"?>\n<VTKFile type="PolyData" version="0.1" byte_order="LittleEndian">'
        f'<PolyData><Piece NumberOfPoints="{len(points)}" NumberOfPolys="{len(faces)}">'
        f'<Points><DataArray type="Float64" NumberOfComponents="3" format="ascii">{pts}</DataArray></Points>'
        f'<Polys><DataArray type="Int64" Name="connectivity" format="ascii">{conn}</DataArray>'
        f'<DataArray type="Int64" Name="offsets" format="ascii">{offsets}</DataArray></Polys>'
        '</Piece></PolyData></VTKFile>\n', encoding="utf-8")


def build(resources, old_package, constraints_path, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import plotly
    import plotly.graph_objects as go
    from plotly.offline import get_plotlyjs

    base = resources / "11_evidence_vnext/canonical"
    inputs = {"constraints": constraints_path, "picks": base/"BOREHOLES/borehole_picks.csv",
              "symbols": base/"WORLD_PARAMETERS/curated/c4_map/skru1/skru1_boreholes_014_unique.csv",
              "plans": base/"WORLD_PARAMETERS/curated/c4_map/skru1/skru1_workings.geojson",
              "links": old_package/"geology/qualified_symbol_pick_links.jsonl", "code": Path(__file__)}
    pins = {k:sha(v) for k,v in inputs.items()}
    out.mkdir(parents=True, exist_ok=False)
    (out/"data").mkdir()
    source = read_json(constraints_path)
    constraints = source["constraints"]
    plans = read_json(inputs["plans"])["features"]
    by_id = {f["properties"]["id"]:f for f in plans}
    picks = csv_rows(inputs["picks"])
    links = [json.loads(line) for line in inputs["links"].read_text(encoding="utf-8").splitlines()]
    positioned = {r["borehole_id"]:r for r in links}
    labels = {r["borehole_id"]:r["symbol_source"]["label_checked"] for r in links}
    geometries, pages = [], []

    def figure(title, ztitle):
        fig = go.Figure()
        fig.update_layout(title={"text":title,"x":0.03}, height=750, font={"family":"Arial","size":14},
            margin={"l":20,"r":20,"t":95,"b":20}, legend={"x":1.01,"y":1},
            scene={"xaxis":{"title":"X — восток, м (LOCAL SH1)","tickformat":".0f","nticks":4},
                   "yaxis":{"title":"Y — север, м (LOCAL SH1)","tickformat":".0f","nticks":3},
                   "zaxis":{"title":ztitle,"tickformat":".0f","nticks":4}, "aspectmode":"manual",
                   "aspectratio":{"x":1,"y":1,"z":.38}, "camera":{"eye":{"x":.9,"y":-1.15,"z":.8}}},
            updatemenus=[{"type":"buttons","direction":"left","x":.05,"y":1.10,"buttons":[
                {"label":"Масштаб ×1","method":"relayout","args":[{"scene.aspectmode":"data"}]},
                {"label":"Увеличить Z (схема)","method":"relayout","args":[{"scene.aspectmode":"manual", "scene.aspectratio":{"x":1,"y":1,"z":.38}}]}]}])
        return fig

    def line(fig, xy, za, zb, name, color, text="", width=9, legend=True):
        fig.add_trace(go.Scatter3d(x=[xy[0],xy[0]],y=[xy[1],xy[1]],z=[za,zb],mode="lines+markers",
            line={"color":color,"width":width},marker={"size":3,"color":color},name=name,showlegend=legend,
            hovertemplate=html.escape(text or name)+"<br>X=%{x:.1f} м; Y=%{y:.1f} м<br>Z=%{z:.2f} м<extra></extra>"))

    def ring(fig, poly, z, color, name, showlegend=False):
        for p in polygon_parts(poly):
            a=np.asarray(p.exterior.coords)
            fig.add_trace(go.Scatter3d(x=a[:,0],y=a[:,1],z=np.full(len(a),z),mode="lines",
                line={"color":color,"width":3},name=name,showlegend=showlegend,
                hovertemplate=html.escape(name)+"<br>Z=%{z:.1f} м<extra></extra>"))

    # Each frame stays in its own figure. There is no hidden vertical registration.
    panel_fig = figure("СКРУ-1 · диапазоны кровли КрII · источник 252, 2013<br><sup>Контуры — прокси названной области; площадь работ 2013 внутри них UNKNOWN</sup>", "Z источника 252, м; datum UNKNOWN")
    areal = [c for c in constraints if c["kind"]=="AREAL_SOURCE_HEIGHT_BOUNDS"]
    panel_geoms=[]
    for k,c in enumerate(areal):
        color = ["#0072B2","#D55E00","#009E73","#CC79A7","#8c734d","#56B4E9","#777700"][k%7]
        for match in c["c4_matches"]:
            f=by_id[match["id"]]; poly=shape(f["geometry"])
            panel_geoms.append((c,poly,color))
            for edge,z in [("lower",c["z_min_m"]),("upper",c["z_max_m"])]:
                points,faces=mesh_at_z(poly,z)
                fid=f'{c["constraint_id"]}:{edge}:DISPLAY_PROXY'
                stem=f'panel_{k}_{edge}'
                vtp(out/"data"/(stem+".vtp"),points,faces)
                metadata={"id":fid,"source_constraint":c,"frame":"SOURCE252_ABSOLUTE_UNSPECIFIED",
                    "geometry_role":"DISPLAY_AREAL_RANGE_BOUND_NOT_A_SEAM_SURFACE_OR_SOLID",
                    "xy_support":"whole named polygon used as display proxy; 2013 subset UNKNOWN",
                    "z_semantics":"spatial source range, not point reading or confidence interval",
                    "file":"data/"+stem+".vtp","status":"DERIVATION; MODEL_CHOICE display support"}
                geometries.append(metadata)
                panel_fig.add_trace(go.Mesh3d(x=points[:,0],y=points[:,1],z=points[:,2],i=faces[:,0],j=faces[:,1],k=faces[:,2],
                    color=color,opacity=.18,name=f'{c["object_label"]}: {c["z_min_m"]}…{c["z_max_m"]} м',showlegend=edge=="upper",
                    hovertemplate=html.escape(fid)+"<br>252:r7; datum UNKNOWN<br>Граница диапазона, не геологическая поверхность<extra></extra>"))
                ring(panel_fig,poly,z,color,f'{c["object_label"]} · {z} м')
            anchor=poly.representative_point()
            panel_fig.add_trace(go.Scatter3d(x=[anchor.x],y=[anchor.y],z=[c["z_max_m"]],mode="text",
                text=[c["object_label"]],textposition="top center",showlegend=False,hoverinfo="skip"))
    pages.append(("КрII: пространственные диапазоны",panel_fig,"252:r7. Полупрозрачные границы показывают диапазон отметок на площади работ, а не поверхность кровли. Неопределённость подмножества 2013 года сохранена. Плоские границы диапазона не воспроизводят падение или складки."))

    brackets=[c for c in constraints if c["kind"]=="VERTICAL_CONTACT_GRAPHICAL_BRACKET"]
    collar_caption=str(brackets[0]["collar_source_m"]).replace(".",",") if brackets else "UNKNOWN"
    shaft_fig=figure(f"СКРУ-1 · ствол 2-бис · графические интервалы контактов<br><sup>Источник 040:19, рис.1.4; Z = +{collar_caption} − глубина; система высот не установлена</sup>","Z источника 040, м; datum UNKNOWN")
    shaft_labels=[]
    for k,c in enumerate(brackets):
        xy=c["c4_coordinate_candidates"][0]["xy"]
        canonical={"PCT":"ПЦТ","TKT":"ТКТ","SMT":"СМТ","PP":"ПП","PKS":"ПКС","SKZ":"СКЗ","SZ":"СЗ","PDKS":"ПдКС"}
        upper=canonical.get(c["upper_unit"],c["upper_unit"]); lower=canonical.get(c["lower_unit"],c["lower_unit"])
        lab=upper+" / "+lower
        line(shaft_fig,xy,c["source_height_min_m"],c["source_height_max_m"],lab,COLORS.get(upper,"#0072B2"),c["constraint_id"]+"; "+c["reading_basis"],width=12)
        shaft_labels.append({"contact":lab,"depth_bounds_m":[c["depth_min_m"],c["depth_max_m"]],
                             "source_height_bounds_m":[c["source_height_min_m"],c["source_height_max_m"]],"id":c["constraint_id"]})
    pages.append(("Ствол 2-бис",shaft_fig,"Это ствол, а не обычная скважина. Толщина линии — экранный знак. Интервалы показывают графические скобки по отметкам крепи, не измерительную точность. Оба варианта положения 014/064 сохранены во входных записях; здесь показан 014."))

    abs_fig=figure("СКРУ-1 · скважина 128 · колонка исходного разреза 011<br><sup>Отдельная система высот; не совмещена с 040, 048, 252 или EGM2008</sup>","Z источника 011, м; datum UNKNOWN")
    abs_records=[]
    for row in picks:
        if row["borehole_id"]!="BH-SOL-128" or row["reference"]!="absolute_elevation":
            continue
        za,zb=number(row["top_abs_m"]),number(row["bottom_abs_m"])
        if za is None:
            continue
        s=positioned[row["borehole_id"]]["symbol_source"]
        xy=[float(s["x_local_m"]),float(s["y_local_m"])]
        unit=row["unit_canonical_guess"]
        line(abs_fig,xy,za,za if zb is None else zb,row["unit_as_printed"],COLORS.get(unit,"#666666"),row["pick_id"]+"; "+row["locator"],width=16)
        abs_records.append({"pick":row,"xy":xy,"identity_status":positioned[row["borehole_id"]]["identity_status"],"vertical_frame":"SOURCE011_ABSOLUTE_UNSPECIFIED"})
    pages.append(("Скважина 128",abs_fig,"Высоты прочитаны с графика; прежняя оценка чтения ±3–5 м не является полной ошибкой скважины. Соляное зеркало и граница СМТ/ПКС расходятся: обе записи сохранены. Совпадение положения со знаком плана — интерпретация, не независимая геодезическая привязка."))

    reflector_fig=figure("СКРУ-1 · скважины 29 и 50с · источник 048, профиль 294С1<br><sup>Отражатели сохраняют исходные названия; геологическое тождество не домысливается</sup>","Z источника 048, м; datum UNKNOWN")
    refl=[]
    for row in picks:
        if row["borehole_id"] not in {"BH-SOL-29","BH-SOL-50s"} or row["reference"]!="absolute_elevation":
            continue
        a,b=number(row["top_abs_m"]),number(row["bottom_abs_m"])
        if a is None and b is None:
            continue
        v=a if a is not None else b
        s=positioned[row["borehole_id"]]["symbol_source"]; xy=[float(s["x_local_m"]),float(s["y_local_m"])]
        name=labels[row["borehole_id"]]+": "+row["unit_as_printed"]
        line(reflector_fig,xy,v,v,name,"#009E73",row["pick_id"]+"; "+row["locator"],width=5)
        refl.append({"pick":row,"xy":xy,"vertical_frame":"SOURCE048_ABSOLUTE_UNSPECIFIED"})
    pages.append(("Профиль 294С1: опоры",reflector_fig,"Устья, забой и отражающие горизонты показаны с исходными именами. Трасса профиля и соответствие отражателей стратиграфическим единицам требуют проверки; соединяющий геологический разрез здесь не придуман."))

    # An optional, explicitly conditional representation: all unknown collars set to zero.
    # This is useful for seeing variability of recorded depths, never accepted height geometry.
    depth_fig=figure("СКРУ-1 и межрудничный целик · глубинные отбивки 012<br><sup>MODEL_CHOICE: устья 0 м; Z = −глубина; † — SKRU1_SKRU2_PILLAR</sup>","−глубина от своего устья, м")
    groups=defaultdict(list); seen={}
    for row in picks:
        bid=row["borehole_id"]
        if bid not in positioned or row["source_id"]!="VKM-SRC-012" or row["reference"]!="depth_below_collar" or row["units"]!="m":
            continue
        value=number(row["top_depth_m"])
        if value is not None:
            seen.setdefault((bid,row["unit_canonical_guess"]),[]).append((value,row))
    for (bid,unit),values in sorted(seen.items()):
        if len({v for v,_ in values})!=1:
            continue  # conflict remains in the source file, never averaged
        value,row=values[0]; s=positioned[bid]["symbol_source"]
        groups[unit].append({"borehole_id":bid,"x":float(s["x_local_m"]),"y":float(s["y_local_m"]),
            "z":-value,"depth_m":value,"pick_ids":[r["pick_id"] for _,r in values],
            "frame":"OWN_COLLAR_ZERO_DISPLAY_SCENARIO","collar_height":"UNKNOWN","interpretation":"ROOF_OR_FLOOR_CONFLICT",
            "source_scope":row["scope"],"source_id":row["source_id"],"locator":row["locator"],
            "original_pick_status":row["status"],"source_scale":row["scale"],
            "status":"MODEL_CHOICE visualization; original depth pick unchanged"})
    all_depth=[]
    for unit,rows in groups.items():
        color=COLORS.get(unit,"#777777"); all_depth.extend(rows)
        depth_fig.add_trace(go.Scatter3d(x=[r["x"] for r in rows],y=[r["y"] for r in rows],z=[r["z"] for r in rows],
            mode="markers",text=[labels[r["borehole_id"]]+("†" if r["source_scope"]!="SKRU1" else "") for r in rows],textposition="top center",
            name=unit+" · глубины",marker={"size":4,"color":color},
            customdata=[";".join(r["pick_ids"])+"; scope="+r["source_scope"] for r in rows],
            hovertemplate=html.escape(unit)+"<br>%{text}; %{customdata}<br>−depth=%{z:.2f} м<br>Смысл кровля/подошва не разрешён<extra></extra>"))
        if len(rows)<3:
            continue
        xy=np.asarray([[r["x"],r["y"]] for r in rows]); zv=np.asarray([r["z"] for r in rows])
        if np.linalg.matrix_rank(xy-xy.mean(axis=0))<2:
            continue
        tri=Delaunay(xy).simplices
        pts=np.column_stack([xy,zv]); name="common_collar_"+str(len(geometries))
        vtp(out/"data"/(name+".vtp"),pts,tri)
        geometries.append({"id":name,"file":"data/"+name+".vtp","unit":unit,"inputs":rows,
            "frame":"OWN_COLLAR_ZERO_DISPLAY_SCENARIO","status":"INTERPOLATION",
            "source_scopes":sorted({r["source_scope"] for r in rows}),
            "mine_transfer":"NOT_ADMITTED; includes distinct intermine-pillar supports as labelled",
            "model_choices":["every unknown collar set to 0 for depth display only","linear Delaunay within per-contact convex hull", "source roof/floor conflict unresolved"],
            "science_admission":"NOT_ADMITTED_AS_ABSOLUTE_GEOLOGICAL_SURFACE"})
        depth_fig.add_trace(go.Mesh3d(x=pts[:,0],y=pts[:,1],z=pts[:,2],i=tri[:,0],j=tri[:,1],k=tri[:,2],
            color=color,opacity=.35,name=unit+" · условная поверхность",visible="legendonly",
            hovertemplate=html.escape(unit)+"<br>INTERPOLATION при устьях0<br>Не абсолютная геология<extra></extra>"))
    shallow={}
    for row in all_depth:
        if row["borehole_id"] not in shallow or row["depth_m"]<shallow[row["borehole_id"]]["depth_m"]:
            shallow[row["borehole_id"]]=row
    depth_fig.add_trace(go.Scatter3d(x=[r["x"] for r in shallow.values()],y=[r["y"] for r in shallow.values()],
        z=[r["z"]+8 for r in shallow.values()],mode="text",text=[labels[r["borehole_id"]]+("†" if r["source_scope"]!="SKRU1" else "") for r in shallow.values()],
        textposition="top center",textfont={"size":13,"color":"#222222"},showlegend=False,hoverinfo="skip"))
    pages.append(("Глубины: СКРУ-1 и целик",depth_fig,"† — опоры межрудничного целика СКРУ-1/СКРУ-2; их scope не повышен до поля СКРУ-1. Нажмите в легенде на условную поверхность, чтобы включить её. Линейная интерполяция только внутри оболочки опор — отдельный диагностический выбор; разрешение геологических доменов отсутствует. Совмещение неизвестных устьев на нуле не даёт абсолютных высот или реальных наклонов. Это диагностическое представление будущего WorldSpec, не готовый PhysicalWorld."))

    # Readable static overview: a map and source range bars, with no implicit datum merge.
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":11})
    fig,axes=plt.subplots(1,2,figsize=(17,9),gridspec_kw={"width_ratios":[1.15,1]})
    ax=axes[0]
    for f in plans:
        if f["properties"].get("layer")!="PANEL_BLOCK_LAYOUT_014":
            continue
        poly=shape(f["geometry"])
        for p in polygon_parts(poly):
            a=np.asarray(p.exterior.coords); ax.plot(a[:,0],a[:,1],color="#cccccc",lw=.45)
    for c,poly,color in panel_geoms:
        for p in polygon_parts(poly):
            a=np.asarray(p.exterior.coords); ax.fill(a[:,0],a[:,1],color=color,alpha=.3)
        p=poly.representative_point(); ax.text(p.x,p.y,c["object_label"],fontsize=11,ha="center",bbox={"facecolor":"white","alpha":.8,"edgecolor":"none","pad":1})
    for bid,r in positioned.items():
        if not r["numeric_pick_rows"]:
            continue
        s=r["symbol_source"]; x,y=float(s["x_local_m"]),float(s["y_local_m"])
        scopes={p["scope"] for p in picks if p["borehole_id"]==bid and number(p["top_depth_m"]) is not None}
        pillar="SKRU1_SKRU2_PILLAR" in scopes
        ax.plot(x,y,"k+" if pillar else "ko",ms=5 if pillar else 3)
        ax.annotate(labels[bid]+("†" if pillar else ""),(x,y),xytext=(3,4),textcoords="offset points",fontsize=9)
    if brackets:
        shaftxy=brackets[0]["c4_coordinate_candidates"][0]["xy"]
        ax.plot(*shaftxy,"r^",ms=7); ax.annotate("Ствол 2-бис",shaftxy,xytext=(7,7),textcoords="offset points",color="#ad251b")
    ax.set_aspect("equal"); ax.set_xlabel("X — восток, м"); ax.set_ylabel("Y — север, м")
    ax.set_title("План опор · SKRU1_LOCAL_SH1\nНачало: ствол №1; геодезический CRS UNKNOWN",fontsize=12)
    ax.grid(alpha=.2)
    ax.annotate("С",xy=(.06,.95),xytext=(.06,.84),xycoords="axes fraction",ha="center",arrowprops={"arrowstyle":"->","lw":2})
    ax2=axes[1]
    for k,(c,_,color) in enumerate(panel_geoms):
        ax2.plot([c["z_min_m"],c["z_max_m"]],[k,k],color=color,lw=9,solid_capstyle="butt")
        ax2.text(c["z_min_m"]-1.5,k,f'{c["z_min_m"]}',va="center",ha="right",fontsize=10)
        ax2.text(c["z_max_m"]+1.5,k,f'{c["z_max_m"]}',va="center",ha="left",fontsize=10)
    ax2.set_yticks(range(len(panel_geoms)),[x[0]["object_label"] for x in panel_geoms]); ax2.invert_yaxis()
    ax2.set_xlim(-190,-115); ax2.set_xlabel("Отметка кровли КрII источника 252, м\nСистема высот и связь с EGM2008 UNKNOWN")
    ax2.set_title("Диапазоны отметок на площадях работ 2013\nЭто пространственная изменчивость, не погрешность",fontsize=12)
    ax2.grid(axis="x",alpha=.25)
    fig.suptitle("СКРУ-1 · проверяемые пространственные геологические ограничения",fontsize=17)
    fig.text(.05,.025,"План: источники 014/064; подписи скважин — существующие проверенные знаки. Диапазоны КрII: 252:r7.\n"
        "† — межрудничный целик СКРУ-1/СКРУ-2. Именованные области — прокси: контуры работ 2013 UNKNOWN; высотные системы не совмещены.",fontsize=10)
    fig.subplots_adjust(top=.86,bottom=.17,left=.07,right=.97,wspace=.3)
    fig.savefig(out/"skru1_constraints_overview.png",dpi=200)
    plt.close(fig)

    # Offline HTML: embedded library, separate tabs and inline provenance summary.
    content=[]
    for n,(title,plot,explanation) in enumerate(pages):
        # Exact aspect ratio from explicit axis ranges: five screen units per vertical metre.
        coords=[]
        for axis in ("x","y","z"):
            vals=[float(v) for trace in plot.data
                  for v in (getattr(trace,axis) if getattr(trace,axis,None) is not None else [])
                  if number(v) is not None]
            lo,hi=min(vals),max(vals)
            width=max(hi-lo,1000 if axis!="z" else 1)
            coords.append((lo-width*.03,hi+width*.03) if hi>lo else (lo-width/2,hi+width/2))
        spans=[hi-lo for lo,hi in coords]; longest=max(spans[:2])
        ratio={"x":spans[0]/longest,"y":spans[1]/longest,"z":5*spans[2]/longest}
        factor=max(ratio.values()); ratio={k:v/factor for k,v in ratio.items()}
        plot.update_layout(scene={"xaxis":{"range":coords[0]},"yaxis":{"range":coords[1]},"zaxis":{"range":coords[2]},"aspectratio":ratio},
            updatemenus=[])
        heading=plot.layout.title.text
        plot.update_layout(title={"text":""},margin={"l":20,"r":20,"t":20,"b":20})
        plot.layout.updatemenus=()
        controls=f'<button onclick=\'Plotly.relayout("plot{n}",{{"scene.aspectmode":"data"}})\'>Масштаб ×1</button>'
        controls+=f'<button onclick=\'Plotly.relayout("plot{n}",{{"scene.aspectmode":"manual","scene.aspectratio":{json.dumps(ratio)}}})\'>Вертикаль ×5</button>'
        content.append(f'<section id="p{n}" class="panel" style="display:{"block" if n==0 else "none"}"><h2>{heading}</h2><p>{html.escape(explanation)}</p>'+controls
            +plot.to_html(full_html=False,include_plotlyjs=False,div_id=f"plot{n}",config={"responsive":True,"displaylogo":False})+"</section>")
    buttons="".join(f'<button onclick="show({n})">{html.escape(p[0])}</button>' for n,p in enumerate(pages))
    doc='<!doctype html><html lang="ru"><meta charset="utf-8"><title>СКРУ-1 — геология из evidence</title>'
    doc+='<style>body{font:16px Arial;margin:24px;color:#172b3d;background:#fafafa}h1{font-size:26px}button{padding:12px;margin:4px;border:1px solid #8798a5;background:white;cursor:pointer}p{max-width:1400px;line-height:1.5}small{color:#4c6070}.panel{background:white;padding:12px}</style>'
    doc+='<script>'+get_plotlyjs()+'</script><h1>СКРУ-1 · геология из источников</h1><p>3D + время WorldSpec → допустимые PhysicalWorld → представления → ObservationWorld → прогноз. '
    doc+='Здесь собраны пространственные опоры и диагностические представления. Сплошная геология рудника ещё не установлена: неизвестные устья, трассы и связи высот сохранены как UNKNOWN.</p>'
    doc+='<p>Оси X/Y в метрах локального плана. Z подписана отдельно для каждого источника. Начальный вертикальный масштаб ×5; кнопка ×1 возвращает одинаковый масштаб метров.</p>'+buttons+"".join(content)
    doc+='<script>function show(n){document.querySelectorAll(".panel").forEach((p,i)=>p.style.display=i==n?"block":"none");window.dispatchEvent(new Event("resize"));}</script>'
    doc+='<p><a href="scene_features.json">Провенанс объектов и выборы модели</a> · <a href="receipt.json">Receipt</a> · <a href="skru1_constraints_overview.png">Подписанный план</a></p>'
    doc+='<small>Данные источников AUTO_EXTRACTED_UNREVIEWED / AGENT_VISUAL_CHECK; научный допуск NOT_CHECKED. Решатели и ML не запускались. Offline Plotly '+plotly.__version__+'.</small></html>'
    (out/"index.html").write_text(doc,encoding="utf-8")
    records={"schema":"vkm.skru1_evidence_scene/1","features":geometries,"shaft_contacts":shaft_labels,
        "source011_columns":abs_records,"source048_points":refl,"depth_display_points":all_depth,
        "frame_policy":"Separate source datums. No zero-offset datum merge, no DSM subtraction.",
        "full_geology_status":"INCOMPLETE_CONSTRAINTS; NOT_A_COMPLETED_PHYSICAL_WORLD",
        "solvers":"NOT_RUN","ml":"NOT_RUN"}
    (out/"scene_features.json").write_text(json.dumps(records,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    if {k:sha(v) for k,v in inputs.items()}!=pins:
        raise RuntimeError("an input changed during build; outputs not verified")
    receipt={"schema":"vkm.skru1_evidence_views/1","inputs_sha256":pins,"source_height_frames":4,
        "areal_constraints":len(areal),"shaft_contact_brackets":len(brackets),"depth_display_picks":len(all_depth),
        "connected_geological_world":"NOT_BUILT; missing common datum/collars/trace/contact identity",
        "output_sha256":{str(p.relative_to(out)).replace("\\","/"):sha(p) for p in sorted(out.rglob("*")) if p.is_file()},
        "input_integrity":"PASS","scientific_admission":"NOT_CHECKED","plotly_version":plotly.__version__}
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return receipt


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--resources-root",type=Path,required=True)
    parser.add_argument("--old-package",type=Path,required=True)
    parser.add_argument("--constraints",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.resources_root,args.old_package,args.constraints,args.out),ensure_ascii=False,indent=2))
