"""Build bounded engineering contact models from explicit mapped borehole depths.

The DSM-collar hypothesis is a new model choice, not a datum transformation of
the earlier model. Original depths and coordinates are retained in the package.
No source contact is changed to close an already built geological top to DSM.
"""
from __future__ import annotations
import argparse
import base64
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter

import numpy as np
from scipy.spatial import Delaunay
from shapely.geometry import MultiPoint, Point, Polygon, mapping

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from vkm_world.geometry_build.constrained_geology import joint_predict, grouped_contact_cv, interval_envelope
from vkm_world.geometry_build.linked_geology import write_vtu, write_vtm, plan_triangles
from vkm_world.geometry_build.mesh_audit import audit_plan_triangles, audit_vertical_wedges, audit_layer_interface
from vkm_world.geometry_build.column_display import TINSurface
from finalize_unified_world_view import PAGE


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    def finite(obj):
        if isinstance(obj,dict):
            return {k:finite(v) for k,v in obj.items()}
        if isinstance(obj,(list,tuple)):
            return [finite(v) for v in obj]
        if isinstance(obj,(float,np.floating)) and not np.isfinite(obj):
            return None
        return obj
    Path(path).write_text(json.dumps(finite(value), ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def encode(array, dtype="f4"):
    a = np.asarray(array, dtype="<"+dtype)
    return {"dtype": dtype, "shape": list(a.shape), "data": base64.b64encode(a.tobytes()).decode("ascii")}


def decode(spec):
    return np.frombuffer(base64.b64decode(spec["data"]), dtype="<"+spec["dtype"]).reshape(spec["shape"])


def js(path, variable, value):
    path.write_text("window."+variable+"="+json.dumps(value, ensure_ascii=False, separators=(",",":"), allow_nan=False)+";\n", encoding="utf-8")


def boundary_polygon(xy, edges):
    adjacency={}
    for a,b in np.asarray(edges).reshape(-1,2):
        adjacency.setdefault(int(a),[]).append(int(b));adjacency.setdefault(int(b),[]).append(int(a))
    if any(len(v)!=2 for v in adjacency.values()):
        raise ValueError("full-context boundary must be a single simple ring")
    start=min(adjacency);ring=[start];previous=None;current=start
    while True:
        following=next(v for v in adjacency[current] if v!=previous)
        if following==start:break
        ring.append(following);previous,current=current,following
        if len(ring)>len(adjacency):raise ValueError("invalid boundary cycle")
    if len(ring)!=len(adjacency):raise ValueError("multiple boundary rings require separate components")
    return Polygon(xy[ring])


def insert_support_vertices(raw_triangles, supports):
    """Split every incident triangle, so edge insertions cannot leave T nodes."""
    triangles=np.asarray(raw_triangles).reshape(-1,3,2)
    for point in np.asarray(supports):
        if np.min(np.linalg.norm(triangles.reshape(-1,2)-point,axis=1))<1e-8:
            continue
        offset=point-triangles
        edge=np.roll(triangles,-1,axis=1)-triangles
        cross=edge[:,:,0]*offset[:,:,1]-edge[:,:,1]*offset[:,:,0]
        lengths=np.linalg.norm(edge,axis=2)
        incident=np.all(cross>=-1e-8*lengths,axis=1)
        if not incident.any():
            raise ValueError("support lies outside full mesh footprint")
        new=[]
        for triangle in triangles[incident]:
            for a,b in zip(triangle,np.roll(triangle,-1,axis=0)):
                area=(b[0]-a[0])*(point[1]-a[1])-(b[1]-a[1])*(point[0]-a[0])
                if area>1e-8:
                    new.append([a,b,point])
        triangles=np.concatenate([triangles[~incident],np.asarray(new)])
    return triangles


def tagged_vtu(path, points, cells, material, note):
    write_vtu(path, {"points": points, "wedges": cells}, material_id=material)
    tree = ET.parse(path)
    field = ET.SubElement(tree.getroot().find("UnstructuredGrid"), "FieldData")
    for name, text in note.items():
        # VTK XML strings are zero-terminated byte codes in ASCII serialization.
        data = ET.SubElement(field, "Array", type="String", Name=name, NumberOfTuples="1", format="ascii")
        data.text = " ".join(map(str, text.encode("utf-8")+b"\0"))
    tree.write(path, encoding="utf-8", xml_declaration=True)


def run(args):
    config, source = read(args.config), read(args.contacts)
    placement = read(args.placements)
    out = args.output.resolve()
    if not out.is_relative_to((REPO/"work").resolve()):
        raise ValueError("real spatial outputs belong in ignored work/")
    if out.exists():
        raise ValueError("use a new versioned output directory")
    for key in ("methods", "spacing_m", "site_offset_m", "site_offset_sensitivity_m", "scopes"):
        if key not in config:
            raise ValueError("explicit configuration required: "+key)
    if config["methods"] != ["nearest", "idw2", "linear"]:
        raise ValueError("comparison methods must be declared before scoring")
    paths = {"builder": Path(__file__), "contacts": args.contacts, "placements": args.placements,
             "config": args.config, "previous_scene": args.previous_scene, "three_runtime": args.three,
             "viewer": REPO/"scripts/unified_world_viewer.js", "template": REPO/"scripts/finalize_unified_world_view.py"}
    for name in ("constrained_geology", "linked_geology", "mesh_audit", "column_display"):
        paths[name] = REPO/("src/vkm_world/geometry_build/"+name+".py")
    for n, filename in enumerate(config.get("evidence_inputs", [])):
        paths["evidence_"+str(n)] = REPO/filename
    pins = {k: sha(p) for k,p in paths.items()}
    out.mkdir(parents=True)
    (out/"inputs").mkdir()
    for label in ("contacts", "placements", "config"):
        shutil.copyfile(paths[label], out/"inputs"/(label+".json"))
    previous_text = args.previous_scene.read_text(encoding="utf-8")
    previous = json.loads(previous_text.split("=",1)[1].strip().removesuffix(";"))
    del previous_text
    terrain_xy = decode(previous["terrain_xy"]).reshape(-1,2)
    terrain_z = decode(previous["terrain_raw_z"]).ravel()
    terrain_tri = decode(previous["terrain_triangles"]).reshape(-1,3)
    surface = TINSurface(np.column_stack([terrain_xy,terrain_z]), terrain_tri)
    oldxy = decode(previous["xy"]).reshape(-1,2)
    oldlevels = decode(previous["levels"])
    oldtri = decode(previous["triangles"]).reshape(-1,3)
    old_top = TINSurface(np.column_stack([oldxy, oldlevels[-1]]),oldtri)
    old_contacts = {u["id"]: oldlevels[i+1] for i,u in enumerate(previous["units"])}
    placed = {r["physical_id"]: r for r in placement["rows"]}
    joined, excluded = [], []
    for row in source["rows"]:
        p = placed.get(row["physical_id"], {})
        if p.get("x") is None or p.get("y") is None or p.get("terrain_height_m") is None:
            excluded.append({"physical_id": row["physical_id"], "reason": "NO_FINITE_PLACEMENT_OR_DSM"})
            continue
        joined.append({**row, **p, "collar_scenario_m": p["terrain_height_m"]+config["site_offset_m"]})
    save(out/"mapped_boreholes.json", joined)
    features=[]
    for row in joined:
        features.append({"type":"Feature", "geometry":{"type":"Point","coordinates":[row["x"],row["y"],row["collar_scenario_m"]]},
                         "properties":{k:row.get(k) for k in ("physical_id","scope","xy_status","terrain_status","depths_m","input_ids")}})
    save(out/"mapped_boreholes.geojson", {"type":"FeatureCollection","name":"SH1_LOCAL_engineering_placements","features":features})
    reports, compare_rows, links, coverage = [], [], [], []
    colors=["#b9b382","#c2d0b2","#d6a7bd","#b5a6ce","#e7cda6"]
    names=source["ordered_contacts"]
    for scope in config["scopes"]:
        full_context=scope=="FULL_CONTEXT"
        rows=[r for r in joined if (full_context or r["scope"]==scope) and all(v is not None for v in r["depths_m"])]
        if len(rows)<3:
            reports.append({"scope":scope,"status":"INSUFFICIENT_COMPLETE_COLUMNS","complete_columns":len(rows)})
            continue
        xy=np.array([[r["x"],r["y"]] for r in rows])
        depths=np.array([r["depths_m"] for r in rows])
        collars=np.array([r["collar_scenario_m"] for r in rows])
        levels=collars[:,None]-depths
        support_hull=MultiPoint(xy).convex_hull
        polygon=boundary_polygon(oldxy,decode(previous["edges"])) if full_context else support_hull
        if polygon.area<=0:
            continue
        # Source supports are explicit vertices. Interior discretization adds no evidence.
        x0,y0,x1,y1=polygon.bounds; step=config["spacing_m"]
        if not np.isfinite(step) or step<=0:
            raise ValueError("positive finite spacing required")
        if full_context:
            raw_triangles=insert_support_vertices(plan_triangles(polygon,step),xy)
            nodes,inverse=np.unique(np.asarray(raw_triangles).reshape(-1,2),axis=0,return_inverse=True)
            triangles=inverse.reshape(-1,3)
        else:
            regular=np.array([[x,y] for x in np.arange(x0,x1+step*.01,step) for y in np.arange(y0,y1+step*.01,step)
                              if polygon.covers(Point(x,y))]).reshape(-1,2)
            nodes=np.unique(np.vstack([xy,regular]),axis=0)
            triangles=Delaunay(nodes).simplices.copy()
        ab=nodes[triangles[:,1]]-nodes[triangles[:,0]]; ac=nodes[triangles[:,2]]-nodes[triangles[:,0]]
        cross=ab[:,0]*ac[:,1]-ab[:,1]*ac[:,0]
        triangles[cross<0]=triangles[cross<0][:,[0,2,1]]
        edge_counts=Counter(tuple(sorted((int(a),int(b)))) for t in triangles for a,b in zip(t,np.roll(t,-1)))
        edges=np.array([edge for edge,count in sorted(edge_counts.items()) if count==1])
        plan_qa=audit_plan_triangles(nodes[triangles],polygon)
        spatial_groups=["west" if x<=np.median(xy[:,0]) else "east" for x in xy[:,0]]
        cv=grouped_contact_cv(xy,levels,[r["physical_id"] for r in rows],spatial_groups)
        relative_cv=grouped_contact_cv(xy,-(depths-depths[:,[0]]),[r["physical_id"] for r in rows],spatial_groups)
        coverage.append({"type":"Feature","geometry":mapping(polygon),"properties":{"scope":scope,"support":"ENGINEERING_EXTENSION_WITH_SEPARATE_SUPPORT_MASK" if full_context else "INTERPOLATION_WITH_ENGINEERING_XY_AND_DSM_COLLAR","physical_groups":len(rows),"area_m2":polygon.area}})
        model_dir=out/scope;model_dir.mkdir()
        save(model_dir/"cross_validation.json",{"absolute_engineering_scenario":cv,"collar_invariant_intervals":relative_cv,
                                               "purpose":"source consistency, not surveyed elevation accuracy"})
        source_fit=[]; all_method_levels={}
        terrain_here=surface.sample(nodes)
        support_surface=surface.sample(xy)
        depth_operator=-depths+(collars-support_surface-config["site_offset_m"])[:,None]
        for method in config["methods"]:
            z,masks,weights=joint_predict(xy,depth_operator if full_context else levels,nodes,method,polygon,extrapolate=full_context and method!="linear")
            if full_context:
                z+=terrain_here[:,None]+config["site_offset_m"]
            if not np.isfinite(z).all() and not (full_context and method=="linear"):
                raise ValueError("mesh node outside declared support")
            all_method_levels[method]=z
            fit,_,_=joint_predict(xy,depth_operator if full_context else levels,xy,method,polygon)
            if full_context:fit+=support_surface[:,None]+config["site_offset_m"]
            source_fit.append({"method":method,"max_used_source_residual_m":float(np.max(abs(fit-levels)))})
        selected_method="idw2" if full_context else "linear"
        z=all_method_levels[selected_method]
        # Source evaluation alone can conceal grid discretization errors.
        exported_fit=np.column_stack([TINSurface(np.column_stack([nodes,z[:,k]]),triangles).sample(xy) for k in range(len(names))])
        exported_residual=float(np.max(abs(exported_fit-levels)))
        if not np.isfinite(exported_residual) or exported_residual>config["acceptance"]["source_fit_tolerance_m"]:
            raise ValueError("exported mesh fails source-contact reproduction")
        _,masks,_=joint_predict(xy,levels,nodes,selected_method,polygon,extrapolate=full_context)
        low_collar=np.array([r.get("terrain_min_m",r["terrain_height_m"]) for r in rows])+min(config["site_offset_sensitivity_m"])
        high_collar=np.array([r.get("terrain_max_m",r["terrain_height_m"]) for r in rows])+max(config["site_offset_sensitivity_m"])
        low,_,_=joint_predict(xy,low_collar[:,None]-depths,nodes,selected_method,polygon,extrapolate=full_context)
        high,_,_=joint_predict(xy,high_collar[:,None]-depths,nodes,selected_method,polygon,extrapolate=full_context)
        if full_context:
            interpolated,_,_=joint_predict(xy,levels,nodes,selected_method,polygon,extrapolate=True)
            low+=z-interpolated;high+=z-interpolated
        np.savez_compressed(model_dir/"contact_fields.npz",xy=nodes,triangles=triangles,contacts=z,lower_sensitivity=low,upper_sensitivity=high,
                            support_mask=masks,**{"contacts_"+k:v for k,v in all_method_levels.items()})
        terrain_checks={"known_samples":int(np.isfinite(terrain_here).sum()),
                        "TKT_above_DSM_nodes":int(np.sum(z[:,0]>terrain_here)),"correction":"none; discrepancy retained"}
        native_entries=[]; mesh_checks=[]; meshes=[]
        for k in range(len(names)-1):
            # Wedges in mathematical CCW winding. Writer converts explicitly to OGS convention.
            n=len(nodes); points=np.vstack([np.column_stack([nodes,z[:,k+1]]),np.column_stack([nodes,z[:,k]])])
            cells=np.column_stack([triangles,triangles+n])
            check=audit_vertical_wedges(points,cells,polygon,winding="ccw")
            if not check["conforming"]:
                raise ValueError("nonconforming engineering volume")
            filename=f"{names[k]}_to_{names[k+1]}.vtu"
            tagged_vtu(model_dir/filename,points,cells,k,{"GeometryRole":"ENGINEERING_SCENARIO","HeightFrame":"DSM_EGM2008_ANCHORED_DEPTH_MODEL",
                       "HorizontalFrame":"SH1_LOCAL","Source":"VKM-SRC-012 p64-65","VerticalTie":"ASSUMED_COLLAR_EQUALS_DSM_PLUS_SITE_OFFSET",
                       "Convention":"SOURCE012_ROOF_PRIMARY_WITH_RETAINED_UPPER_CONFLICT"})
            native_entries.append((names[k]+" to "+names[k+1],filename))
            mesh_checks.append(check);meshes.append((points,cells))
        interface_checks=[audit_layer_interface(meshes[k+1][0],meshes[k+1][1],meshes[k][0],meshes[k][1]) for k in range(len(meshes)-1)]
        write_vtm(model_dir/"unified_world.vtm",native_entries)
        # Real section line passes through every support in increasing X order.
        ordered=np.argsort(xy[:,0]); route=xy[ordered]
        query=[]; distance=[]; chainage=0.
        for a,b in zip(route[:-1],route[1:]):
            length=float(np.linalg.norm(b-a))
            for t in np.linspace(0,1,max(3,int(length/step)+1),endpoint=False):
                query.append(a+(b-a)*t);distance.append(chainage+t*length)
            chainage+=length
        query.append(route[-1]);distance.append(chainage);query=np.array(query)
        section,_,_=joint_predict(xy,depth_operator if full_context else levels,query,selected_method,polygon,extrapolate=full_context)
        if full_context:section+=surface.sample(query)[:,None]+config["site_offset_m"]
        old_smooth=decode(previous["terrain_smooth_z"]).ravel()
        display_surface=TINSurface(np.column_stack([terrain_xy,old_smooth]),terrain_tri)
        old_delta=display_surface.sample(query)-old_top.sample(query)
        comparisons={}
        for k,name in enumerate(names):
            if name not in old_contacts:
                continue
            old_native=old_top.sample(query,z_values=old_contacts[name])
            old_display=old_native+old_delta
            valid=np.isfinite(old_native)&np.isfinite(section[:,k])
            error=section[valid,k]-old_native[valid]
            comparisons[name]={"common_points":int(valid.sum()),"mean_change_from_old_native_m":float(error.mean()) if len(error) else None,
                               "max_abs_change_m":float(np.max(abs(error))) if len(error) else None}
            for i in np.where(valid)[0]:
                compare_rows.append({"scope":scope,"contact":name,"distance_m":distance[i],"x":query[i,0],"y":query[i,1],
                                     "old_native_z_m":old_native[i],"old_display_z_m":old_display[i],"new_scenario_z_m":section[i,k]})
        save(model_dir/"sections.json",{"route_physical_ids":[rows[i]["physical_id"] for i in ordered],"distance_m":distance,
                                      "xy":query.tolist(),"new_contact_z_m":section.tolist(),"unknown_points":int(np.sum(~np.isfinite(section).all(axis=1))),"comparison":comparisons})
        render_sections(model_dir,scope,query,distance,section,names,old_top,old_contacts,old_delta,surface,rows)
        export_view(model_dir,scope,nodes,triangles,edges,z,names,colors,terrain_xy,terrain_z,terrain_tri,rows,args.three,full_context,support_hull)
        # Rank is tested on thickness-only observations, before the declared DSM hypothesis.
        rank_constraints=[]
        for k,value in enumerate(np.diff(depths[0])):
            coefficients=np.zeros(len(names));coefficients[k]=1;coefficients[k+1]=-1
            rank_constraints.append({"id":rows[0]["physical_id"]+":"+names[k],"coefficients":coefficients.tolist(),"lower":value,"upper":value})
        report={"scope":scope,"status":"ENGINEERING_MODEL_BUILT","physical_groups":[r["physical_id"] for r in rows],
                "area_m2":polygon.area,"vertices_per_contact":len(nodes),"wedges_per_interval":len(triangles),
                "source_fit":source_fit,"exported_mesh_source_residual_m":exported_residual,"plan_qa":plan_qa,"mesh_qa":mesh_checks,"interface_qa":interface_checks,
                "thickness_only_identifiability":interval_envelope(len(names),rank_constraints),"terrain_checks":terrain_checks,
                "absolute_height_status":"ENGINEERING_ASSUMPTION; independent collar heights unknown",
                "method_choice":selected_method+" selected before field build; CV does not establish field accuracy",
                "extension":full_context,"support_mask_counts":dict(Counter(map(str,masks))),
                "vertical_operator":"local DSM minus joint source-depth field; bilinear-versus-TIN sampling residual reconciled at source wells" if full_context else "interpolated DSM collars minus source depths",
                "DSM_sampling_operator_max_difference_m":float(np.max(abs(collars-support_surface-config["site_offset_m"]))),
                "source_support_hull_area_m2":support_hull.area,
                "comparison":comparisons}
        save(model_dir/"qa.json",report);reports.append(report);links.append(scope)
    save(out/"coverage.geojson",{"type":"FeatureCollection","name":"SH1_LOCAL_support_masks","features":coverage})
    if compare_rows:
        with (out/"before_after_same_sections.csv").open("w",newline="",encoding="utf-8") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(compare_rows[0]));writer.writeheader();writer.writerows(compare_rows)
    save(out/"summary.json",{"models":reports,"mapped_boreholes":len(joined),"excluded":excluded,"field_validation":False,
                              "available_branches":source["branches"],"site_offset_m":config["site_offset_m"]})
    (out/"index.html").write_text('<!doctype html><meta charset="utf-8"><title>Инженерная геология</title><h1>Инженерная геология по скважинам</h1><p>Привязка схем к SH1; устья по DSM GLO-30 (2011–2015, EGM2008) — инженерное допущение. Глубины сохранены.</p>'+''.join(f'<p><a href="{s}/index.html">{s}: 3D и разрезы</a> · <a href="{s}/sections.png">До / после на общих сечениях</a></p>' for s in links)+'<p><a href="summary.json">Проверки и охват</a> · <a href="mapped_boreholes.geojson">Скважины в SH1</a></p>',encoding="utf-8")
    if pins!={k:sha(p) for k,p in paths.items()}:
        raise RuntimeError("input changed during build")
    save(out/"receipt.json",{"schema":"vkm.engineering_geology/1","command":sys.argv,"inputs":{k:{"path":str(p.resolve().relative_to(REPO)).replace('\\','/'),"sha256":pins[k]} for k,p in paths.items()},
                              "outputs_sha256":{p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()},
                              "interpretation":"ENGINEERING_ASSUMPTION_DSM_COLLAR","source_data_changed":False,"solver":"NOT_RUN"})
    print(json.dumps({"models":links,"mapped_boreholes":len(joined),"output":str(out)},ensure_ascii=False))


def render_sections(out,scope,query,distance,section,names,old_top,old_contacts,old_delta,surface,rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(2,1,figsize=(13,8),sharex=True,layout="constrained")
    for k,name in enumerate(names):
        line,=axes[0].plot(distance,section[:,k],label=name)
        if name in old_contacts:
            native=old_top.sample(query,z_values=old_contacts[name])
            axes[0].plot(distance,native,"--",color=line.get_color(),alpha=.55)
            axes[1].plot(distance,section[:,k]-native,label=name)
    axes[0].plot(distance,surface.sample(query),color="black",label="DSM 2011–2015")
    axes[0].set_title(scope+": solid = engineering contacts; dashed = previous native; source012 p64–65")
    axes[0].set_ylabel("Z, m; DSM EGM2008 anchor assumed")
    axes[1].set_ylabel("New − previous native, m");axes[1].set_xlabel("Distance along support route, m")
    axes[1].axhline(0,color="black",lw=.7)
    for ax in axes:ax.grid(alpha=.25);ax.legend(ncol=4,fontsize=8)
    fig.savefig(out/"sections.png",dpi=160);plt.close(fig)


def export_view(out,scope,nodes,triangles,edges,z,names,colors,txy,tz,ttri,rows,three,full_context=False,support_hull=None):
    (out/"data").mkdir();(out/"vendor").mkdir()
    bounds=MultiPoint(nodes).bounds
    mid=txy[ttri].mean(axis=1)
    select=(mid[:,0]>=bounds[0]-300)&(mid[:,0]<=bounds[2]+300)&(mid[:,1]>=bounds[1]-300)&(mid[:,1]<=bounds[3]+300)
    chosen=ttri[select];used=np.unique(chosen);index=np.full(len(txy),-1);index[used]=np.arange(len(used))
    units=[{"id":names[k]+"_"+names[k+1],"name":names[k]+" → "+names[k+1]+" (интервал)","color":colors[k]} for k in range(len(names)-1)]
    scene={"xy":encode(nodes),"triangles":encode(triangles,"i4"),"edges":encode(edges,"i4"),"levels":encode(z[:,::-1].T),"units":units[::-1],
           "terrain_xy":encode(txy[used]),"terrain_raw_z":encode(tz[used]),"terrain_smooth_z":encode(tz[used]),"terrain_triangles":encode(index[chosen],"i4"),
           "recent_points":[],"meta":{"bounds":bounds,"frame_label":"XY SH1, м; Z DSM EGM2008 + глубины, инженерная привязка"}}
    objects={"meshes":[],"lines":[],"boreholes":[{"id":r["physical_id"],"label":r["physical_id"],"x":r["x"],"y":r["y"],"display_z":r["collar_scenario_m"],
              "status":"ENGINEERING_ASSUMPTION","source_id":"VKM-SRC-012","display_anchor_status":"DSM_COLLAR_ASSUMPTION"} for r in rows]}
    for r in rows:
        pts=np.array([[r["x"],r["y"],r["collar_scenario_m"]],[r["x"],r["y"],r["collar_scenario_m"]-max(r["depths_m"])]])
        objects["lines"].append({"id":r["physical_id"],"group":"borehole","paths":[{"points":encode(pts),"delta":encode([0,0])}],"z_status":"SOURCE_DEPTHS_AT_ENGINEERING_COLLAR"})
    js(out/"data/scene.js","VKM_SCENE",scene)
    js(out/"data/unified.js","VKM_UNIFIED",{"delta":encode(np.zeros(len(nodes))),"meta":{"height_mode":"ENGINEERING_DSM_COLLAR_DEPTHS; NO_POSTHOC_WARP","height_label":"Глубины скважин + DSM; инженерное продолжение" if full_context else "Контрольный участок по скважинам"}})
    js(out/"data/spatial_objects.js","VKM_SPATIAL_OBJECTS",objects)
    (out/"data/supplement.js").write_text("// No satellite quality promotion.\n",encoding="utf-8")
    shutil.copyfile(three,out/"vendor/three.min.js");shutil.copyfile(REPO/"scripts/unified_world_viewer.js",out/"unified_world_viewer.js")
    import re
    page=PAGE.replace("Рельеф, девять геологических толщ, план горных работ, скважины и размерные сценарии камер в одной системе просмотра.",scope+": контакты по скважинам, исходная DSM и инженерная высотная привязка.")
    page=re.sub(r'<div class="notice">.*?</div>','<div class="notice">Устья = DSM + явная поправка; контакты = устья − исходные глубины. Это инженерная модель. XY привязаны по общим скважинам; глобальный высотный datum источников не установлен. Верхние контакты сохраняют конфликт интерпретации.</div>',page,count=1)
    page=page.replace('value="1">1 : 1','value="1" selected>1 : 1').replace('value="3" selected','value="3"')
    page=page.replace('<option value="smooth">Сглаженная DSM</option>','').replace('Все 419 точек','Не подключены')
    page=re.sub(r'<select id="region">.*?</select>','<select id="region"><option value="all">'+scope+'</option></select>',page,count=1)
    page=page.replace('data/joint_manifest.json','qa.json').replace('evidence/HEIGHT_FRAME_REVIEW_RU.md','cross_validation.json').replace('RESULT_RU.md','sections.png')
    page=page.replace('Камеры</label>','Камеры (нет)</label>').replace('КрII</label>','Отдельный КрII (нет)</label>').replace('ICESat-2</label>','ICESat-2 (нет)</label>')
    page=page.replace('Вращение — левая кнопка мыши','X / Y: SH1, м; Z: EGM2008 DSM, инженерная привязка · Вращение — левая кнопка мыши')
    if full_context:
        page=page.replace(scope+": контакты", "Полная область исходной модели: контакты")
        page=page.replace('Устья = DSM + явная поправка;', 'Полная инженерная модель. За границей опор — продолжение IDW2, без новых измерений. Устья = DSM + явная поправка;')
    (out/"index.html").write_text(page,encoding="utf-8")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for name in ("config","contacts","placements","previous-scene","three","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    run(parser.parse_args())
