"""Detailed local geological display from pinned evidence and scenario inputs.

Full mesh topology is retained in native exports and browser views. Terrain and
mine Z frames remain separate; joint display does not establish a vertical tie.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial import cKDTree
from shapely import contains_xy
from shapely.geometry import shape, mapping, box
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from vkm_world.geometry_build.linked_geology import folded_roof, idw_values, layer_mesh, polygon_parts, write_vtu, write_vtm

UNITS=[('PDKS','Подстилающая каменная соль','#82939d'),('SZ','Сильвинитовая зона','#b45e8e'),
 ('SKZ','Сильвинито-карналлитовая зона','#dd7856'),('PKS','Покровная каменная соль','#e6a8b4'),
 ('PP','Переходная пачка','#ae9bc2'),('SMT','Соляно-мергельная толща','#70a6bc'),
 ('TKT','Терригенно-карбонатная толща','#c09360'),('PCT','Пестроцветная толща','#9abd78'),
 ('Q','Четвертичные отложения','#e4ca67')]

def read(p): return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,v): Path(p).write_text(json.dumps(v,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')

def read_volume(path):
    root=ET.parse(path).getroot(); piece=root.find('.//Piece')
    p=np.fromstring(piece.find('./Points/DataArray').text,sep=' ').reshape(-1,3)
    a={x.attrib['Name']:x for x in piece.findall('./Cells/DataArray')}
    cells=np.fromstring(a['connectivity'].text,sep=' ',dtype=np.int64).reshape(-1,6)
    # Stored OGS clockwise prisms -> mathematical counterclockwise base.
    t=cells[:,[0,2,1]]; n=len(p)//2
    edges=np.sort(np.vstack([t[:,[0,1]],t[:,[1,2]],t[:,[2,0]]]),axis=1)
    unique,counts=np.unique(edges,axis=0,return_counts=True); edge=unique[counts==1]
    side=np.vstack([np.column_stack([edge[:,0],edge[:,1],edge[:,1]+n]),np.column_stack([edge[:,0],edge[:,1]+n,edge[:,0]+n])])
    return {'points':p,'wedges':np.column_stack([t,t+n]),'faces':np.vstack([t[:,::-1],t+n,side])}

class Model:
    def __init__(self,m):
        self.m=m;self.regions=m['source_mining_constraints']['regions']; self.depth=m['source040_ordered_depth_realization_m']
        self.groups={name:np.array([[r['x'],r['y'],r['value_m']] for r in m['source012_interval_inputs'] if r['interval_id']==name])
          for name in ('TKT_to_SMT','SMT_to_PKS','SKZ_to_SZ','SZ_to_PDKS')}
        xy=m['source040_original_brackets'][0]['c4_coordinate_candidates'][0]['xy']
        for name,value in m['source040_prior_support_values_m'].items():self.groups[name]=np.vstack([self.groups[name],[*xy,value]])
        support=[]
        for r in self.regions:
            p=shape(r['plan_support']['geometry']).representative_point(); z=r['roof']
            support.append([p.x,p.y,(z['z_min_m']+z['z_max_m'])/2])
        self.roof_support=np.array(support); self.thickness=max(r['geological_thickness']['value_m'] for r in self.regions)

    def roof(self,xy): return idw_values(xy,self.roof_support)

    def local_roof(self,xy,r):
        p=shape(r['plan_support']['geometry']);z=r['roof'];f=r['folds']
        return folded_roof(xy,p.bounds,z['z_min_m'],z['z_max_m'],max(f['wing_span_m_bounds']),
          sum(f['amplitude_m_bounds'])/2,1.1,0. if z['dip_direction_as_printed'].startswith('west') else 180.)

    def interfaces(self,xy,roof=None,seam_thickness=None):
        kr2=self.roof(xy) if roof is None else np.asarray(roof)
        thickness={k:idw_values(xy,v) for k,v in self.groups.items()};d=self.depth
        sz=thickness['SZ_to_PDKS'];skz=thickness['SKZ_to_SZ']
        h=self.thickness if seam_thickness is None else seam_thickness
        top_sz=kr2+.8*(sz-h); bottom_sz=top_sz-sz;top_skz=top_sz+skz
        top_pks=top_skz+d[5]-d[4];top_pp=top_pks+d[4]-d[3]
        top_smt=top_pks+thickness['SMT_to_PKS'];top_tkt=top_smt+thickness['TKT_to_SMT']
        top_pct=top_tkt+d[1]-d[0];top_q=top_pct+d[0]
        bounds=np.array([bottom_sz-50,bottom_sz,top_sz,top_skz,top_pks,top_pp,top_smt,top_tkt,top_pct,top_q])
        if not np.isfinite(bounds).all() or not np.all(np.diff(bounds,axis=0)>0):raise ValueError('invalid strata')
        return bounds

def figure(title,ztitle='Z источника / сценария, м · вертикальное совмещение UNKNOWN'):
    import plotly.graph_objects as go
    f=go.Figure();f.update_layout(title={'text':title,'x':.02},height=920,margin=dict(l=0,r=0,t=90,b=0),
      paper_bgcolor='#f5f5f0',font=dict(family='Arial',size=13),legend=dict(x=.84,y=.98,bgcolor='rgba(255,255,255,.8)'),
      scene=dict(xaxis_title='X LOCAL SH1, м',yaxis_title='Y LOCAL SH1, м',zaxis_title=ztitle,
        aspectmode='manual',aspectratio=dict(x=1.15,y=1,z=.7),camera=dict(eye=dict(x=1.35,y=-1.7,z=1.15))))
    return f

def add_mesh(f,mesh,name,color=None,opacity=1.,visible=True,intensity=False):
    import plotly.graph_objects as go
    p=mesh['points'].astype(np.float32);t=mesh['faces'].astype(np.int32)
    props={'intensity':p[:,2],'colorscale':'Earth','showscale':False} if intensity else {'color':color}
    f.add_trace(go.Mesh3d(x=p[:,0],y=p[:,1],z=p[:,2],i=t[:,0],j=t[:,1],k=t[:,2],name=name,
      opacity=opacity,visible=visible,showlegend=True,flatshading=False,
      lighting=dict(ambient=.5,diffuse=.85,specular=.15,roughness=.9),**props,
      hovertemplate=html.escape(name)+'<br>X=%{x:.2f};Y=%{y:.2f};Z=%{z:.2f}<extra></extra>'))

def add_lines(f,polygons,zfunc,color,name,width=2,visible=True,dash=None):
    import plotly.graph_objects as go
    xs=[];ys=[];zs=[]
    for polygon in polygons:
        for part in polygon_parts(polygon):
            for ring in [part.exterior,*part.interiors]:
                a=np.array(ring.segmentize(15).coords);z=zfunc(a[:,:2])
                xs.extend([*a[:,0],None]);ys.extend([*a[:,1],None]);zs.extend([*z,None])
    line={'color':color,'width':width}
    if dash:line['dash']=dash
    f.add_trace(go.Scatter3d(x=xs,y=ys,z=zs,mode='lines',line=line,name=name,visible=visible))

def volume_from_bounds(reference,xy,low,high):
    p=np.vstack([np.column_stack([xy,low]),np.column_stack([xy,high])])
    return {**reference,'points':p}

def export_page(f,path,caption):
    document='<!doctype html><html lang="ru"><meta charset="utf-8"><style>body{margin:0;background:#f5f5f0;color:#203940;font:14px Arial}p{margin:12px 28px;line-height:1.5}</style><script src="plotly.min.js"></script>'
    document+='<p>'+caption+'</p>'+f.to_html(full_html=False,include_plotlyjs=False,config={'responsive':True,'displaylogo':False})
    path.write_text(document,encoding='utf-8')

def build(args):
    import plotly.graph_objects as go
    from plotly.offline import get_plotlyjs
    out=args.output; repo=Path(__file__).resolve().parents[1]
    if not out.resolve().is_relative_to((repo/'work').resolve()):raise ValueError('real-data outputs belong in ignored work/')
    out.mkdir(parents=True,exist_ok=False);(out/'data').mkdir();(out/'plotly.min.js').write_text(get_plotlyjs(),encoding='utf-8')
    inputs={'code':Path(__file__),'geometry_code':repo/'src/vkm_world/geometry_build/linked_geology.py',
      'base_manifest':args.base/'data/geometry_manifest.json','base_receipt':args.base/'receipt.json','plans':args.plans,'terrain':args.terrain}
    if args.subsidence:
        inputs['subsidence_receipt']=args.subsidence/'RECEIPT.json'
        inputs['subsidence_inventory']=args.subsidence/'SUBSIDENCE_INVENTORY.json'
    if args.freshness:inputs['freshness_inventory']=args.freshness
    pins={k:sha(p) for k,p in inputs.items()}
    m=read(inputs['base_manifest']); model=Model(m); plans=read(args.plans)['features']; terrain=read(args.terrain)
    tp=np.array(terrain['points']);tf=np.array(terrain['triangles'],dtype=np.int64)
    terrain_mesh={'points':tp,'faces':tf};tree=cKDTree(tp[:,:2])
    def terrain_nearest(xy):return tp[tree.query(xy)[1],2]
    domains=[shape(f['geometry']) for f in plans if f['properties']['kind'] in ('BLOCK','PANEL')]
    domain=unary_union(domains)
    if not domain.is_valid:raise ValueError('invalid mapped union')
    x0,y0,x1,y1=domain.bounds
    # Cutaway is a display operation, not an inferred erosion or geological body.
    window=box(x0+(x1-x0)*.42,y0-100,x1+100,y0+(y1-y0)*.56)
    cutdomain=domain.difference(window)
    save(out/'data/display_domains.geojson',{'type':'FeatureCollection','features':[
      {'type':'Feature','properties':{'role':'mapped BLOCK/PANEL union; not surveyed mine boundary','status':'DERIVATION'},'geometry':mapping(domain)},
      {'type':'Feature','properties':{'role':'DISPLAY_CUTAWAY_ONLY','status':'MODEL_CHOICE'},'geometry':mapping(window)}]})
    print('Building full-resolution field topology',flush=True)
    reference=layer_mesh(domain,lambda xy:np.zeros(len(xy)),1,spacing=args.spacing)
    n=len(reference['points'])//2;xy=reference['points'][:n,:2];levels=model.interfaces(xy)
    cutref=layer_mesh(cutdomain,lambda xy:np.zeros(len(xy)),1,spacing=args.spacing)
    cn=len(cutref['points'])//2;cxy=cutref['points'][:cn,:2];clevels=model.interfaces(cxy)
    full=figure('Геология · полная сетка и исходная форма областей<br><sup>Сетка '+str(args.spacing)+' м; между опорами интерполяция. Верхние толщи скрываются щелчком.</sup>')
    joint=figure('Рельеф, вскрытый массив и раскройка поля<br><sup>Срез — только способ показа. DSM EGM2008 и Z рудника показаны при сценарном ΔZ=0; вертикальная связь UNKNOWN.</sup>')
    entries=[];counts={'field_spacing_m':args.spacing,'field_plan_vertices':n,'field_plan_triangles':len(reference['wedges']),
      'terrain_vertices':len(tp),'terrain_triangles':len(tf),'plan_features':len(plans),'plan_by_kind':dict(Counter(f['properties']['kind'] for f in plans)),
      'field_native_wedges':0,'local_native_wedges':0,'room_native_wedges':0,'browser_LOD_decimation':False}
    for i,(unit,name,color) in enumerate(UNITS):
        mesh=volume_from_bounds(reference,xy,levels[i],levels[i+1]);stem='detailed_'+unit
        write_vtu(out/'data'/f'{stem}.vtu',mesh,i);entries.append((name,f'data/{stem}.vtu'))
        add_mesh(full,mesh,name,color)
        cut=volume_from_bounds(cutref,cxy,clevels[i],clevels[i+1])
        add_mesh(joint,cut,name,color,visible=True if i<5 else 'legendonly')
        counts['field_native_wedges']+=len(mesh['wedges'])
    # Complete raster topology is retained; the visual aperture removes faces only.
    ctr=tp[tf].mean(axis=1); keep=~contains_xy(window,ctr[:,0],ctr[:,1])
    add_mesh(joint,{'points':tp,'faces':tf[keep]},'Поверхность DSM 2011–2015 · EGM2008',intensity=True)
    add_mesh(joint,terrain_mesh,'Полная поверхность · показать',visible='legendonly',intensity=True)
    surface=figure('Рельеф · все исходные точки DSM и контуры плана','Z EGM2008, м · DSM 2011–2015')
    add_mesh(surface,terrain_mesh,'DSM · исходная сетка 30 м',intensity=True)
    plan3d=figure('Исходные контуры и геология продуктивной зоны<br><sup>XY с планов. Высота контура — производная проекция на сценарную кровлю КрII, не измеренная 3D граница.</sup>')
    # Distinguish the map evidence classes; do not call automatic source areas actual rooms.
    colors=['#144b67','#369b9d','#d89a32','#b94153','#63797c','#af81b2','#4f78a9','#8f5d25','#52a579','#343b50','#7f8a30','#b47c7c','#7e5742']
    for ki,(kind,count) in enumerate(sorted(counts['plan_by_kind'].items())):
        features=[f for f in plans if f['properties']['kind']==kind];polys=[shape(f['geometry']) for f in features]
        label=kind+' · '+str(count)+' контуров · '+('авто, не проверено' if any(f['properties'].get('review_status')=='AUTO_EXTRACTED_UNREVIEWED' for f in features) else 'статус в manifest')
        add_lines(plan3d,polys,model.roof,colors[ki%len(colors)],label,2)
        if kind in ('BLOCK','PANEL','PROTECTIVE_PILLAR','REMAINING_RESERVES_AREA'):
            add_lines(joint,polys,model.roof,colors[ki%len(colors)],label,2)
            add_lines(surface,polys,lambda xy:terrain_nearest(xy)+.5,colors[ki%len(colors)],label,2)
    # Full local fold meshes, with no previous 20m browser display mesh.
    folds=figure('КрII · детальная складчатость и размерные камеры<br><sup>Все вершины экспорта; азимут/фаза складок и камер — MODEL_CHOICE. Масштаб Z увеличен.</sup>')
    local_pages=[];local_entries=[]
    for ri,r in enumerate(model.regions):
        print('Full local geology '+r['label'],flush=True)
        local=figure('Участок '+html.escape(r['label'])+' · полная геометрия толщ<br><sup>Общие XY и интерфейсы; верхние толщи скрываются щелчком. Камеры — сценарий ПРГР-2013.</sup>')
        for ui,(unit,name,color) in enumerate(UNITS):
            src=args.base/'data'/f'local_{ri}_{unit}.vtu';v=read_volume(src)
            add_mesh(local,v,name,color,visible=True if ui<5 else 'legendonly')
            local_entries.append((r['label']+' '+name,os.path.relpath(src,out).replace('\\','/')))
            counts['local_native_wedges']+=len(v['wedges'])
        seam=read_volume(args.base/'data'/f'KR2_{ri}.vtu')
        local_entries.append((r['label']+' KrII',os.path.relpath(args.base/'data'/f'KR2_{ri}.vtu',out).replace('\\','/')))
        add_mesh(folds,seam,'КрII · '+r['label'],UNITS[1][2],intensity=True)
        add_mesh(local,seam,'КрII · '+r['label'],'#543844')
        for room in [v for v in m['chamber_scenarios'] if v['region']==r['region_id']]:
            src=args.base/room['file'];mesh=read_volume(src)
            local_entries.append((room['id']+' scenario',os.path.relpath(src,out).replace('\\','/')))
            add_mesh(local,mesh,'Камера-сценарий '+room['id'],'#243b4a')
            # All chambers share a legend group per area but retain their individual topology.
            add_mesh(folds,mesh,'Камера-сценарий '+room['id'],'#243b4a',visible='legendonly')
            counts['room_native_wedges']+=len(mesh['wedges'])
        filename=f'local_{ri}.html';local_pages.append((r['label'],filename))
        export_page(local,out/filename,'Полная локальная сетка. Верхние толщи можно скрыть в легенде. Неподтверждённые места складок и камер не превращены в факты.')
    # Derive two intersecting sections from the same functions, avoiding independent redraw.
    sections=figure('Пересекающиеся геологические разрезы и плановые контуры')
    for orientation in ('EW','NS'):
        a=np.linspace(x0,x1,1400) if orientation=='EW' else np.linspace(y0,y1,1400)
        sxy=np.column_stack([a,np.full(len(a),(y0+y1)/2)]) if orientation=='EW' else np.column_stack([np.full(len(a),(x0+x1)/2),a])
        b=model.interfaces(sxy);tris=np.array([[j,j+1,j+len(a)] for j in range(len(a)-1)]+[[j+1,j+len(a)+1,j+len(a)] for j in range(len(a)-1)])
        for ui,(unit,name,color) in enumerate(UNITS):
            p=np.vstack([np.column_stack([sxy,b[ui]]),np.column_stack([sxy,b[ui+1]])])
            add_mesh(sections,{'points':p,'faces':tris},name+' · '+orientation,color)
    # Source011 remains independent in Z; it can be selected from the existing validated scene.
    for r in model.regions:
        add_lines(sections,[shape(r['plan_support']['geometry'])],lambda xy,r=r:model.local_roof(xy,r),'#233847',r['label'])
    for f,name in [(joint,'joint'),(full,'geology'),(surface,'surface'),(folds,'folds'),(plan3d,'polygons'),(sections,'sections')]:
        export_page(f,out/(name+'.html'),'XY: LOCAL SH1. DSM и геология имеют разные вертикальные отсчёты. Совместный вид использует явное сценарное ΔZ=0, без изменения исходных Z. Сетка детализирует представление, но не добавляет измерений.')
    # Native terrain and multiblocks; keep full geology and local hypotheses distinct.
    terrain_vtp=args.terrain.with_name('terrain_local_SH1_EGM2008_full.vtp')
    if terrain_vtp.exists():shutil.copyfile(terrain_vtp,out/'data/terrain_full.vtp');entries.append(('DSM EGM2008 · vertical tie UNKNOWN','data/terrain_full.vtp'))
    write_vtm(out/'detailed_field.vtm',entries);write_vtm(out/'detailed_local.vtm',local_entries)
    save(out/'data/plan_features.geojson',{'type':'FeatureCollection','features':plans})
    if (args.base/'data/additional_interval_supports.json').exists():
        shutil.copyfile(args.base/'data/additional_interval_supports.json',out/'data/additional_interval_supports.json')
    counts['local_regions']=len(model.regions);counts['source012_intervals']=len(m['source012_interval_inputs'])
    save(out/'data/detail_manifest.json',{'schema':'vkm.detailed_geology/1','counts':counts,'inputs_sha256':pins,
      'geology':'same explicit central hypothesis as base; actual source polygon union replaces convex envelope',
      'full_topology':True,'browser_precision':'float32 coordinates only; native float64 source precision retained',
      'source012_interval_inputs':m['source012_interval_inputs'],'source012_branch':m['source012_branch'],
      'source012_observed_intervals_preserved_separately':True,
      'source040_prior_support_values_m':m['source040_prior_support_values_m'],
      'base_manifest_reference':os.path.relpath(inputs['base_manifest'],out/'data').replace('\\','/'),
      'additional_supports_reference':'additional_interval_supports.json; two XY choices remain SCENARIO_XY/MODEL_CHOICE',
      'surface':{k:terrain[k] for k in ('coordinates','source','grid','limitations') if k in terrain},
      'geological_elements':{'regional_stratigraphy':'9 source-informed interpreted units','local_folds':'7 source-constrained, phase/azimuth MODEL_CHOICE',
        'faults':'NOT_INSERTED_WITHOUT_LOCALIZED_EVIDENCE','subsidence':'SEPARATE_RELATIVE_DISPLACEMENT_VIEW' if args.subsidence else 'NOT_CONNECTED',
        'structural_context':'SKRU1 Rudnichny dome; SKRU3 Tveritinskaya trough; no accepted shared 3D localization inferred'},
      'display_cutaway':'MODEL_CHOICE, not erosion or a mined volume','geology_to_DSM_vertical_tie':None,
      'computational_coordinate_snap_m':1e-8,
      'geological_and_mining_source_states_preserved':True,'field_validation':False,'solver_execution':'NOT_RUN'})
    links=[('Массив и поверхность','joint.html'),('Рельеф · полная сетка','surface.html'),('Полная геология','geology.html'),
      ('Складки и камеры','folds.html'),('Все контуры','polygons.html'),('Пересекающиеся разрезы','sections.html')]+[(label,url) for label,url in local_pages]
    extra=''
    if args.subsidence:
        url=os.path.relpath(args.subsidence/'index.html',out).replace('\\','/')
        links.append(('Мульда · наблюдения по эпохам',url))
        extra+='<p>Мульда: опубликованные относительные оседания линии 12 за 1992/1995/2004/2007/2010; поперечное продолжение включается отдельно как MODEL_CHOICE. Наблюдения не вычитаются из DSM.</p>'
    if args.freshness:
        url=os.path.relpath(args.freshness.with_name('MATRIX_RU.md'),out).replace('\\','/')
        extra+='<p><a href="'+html.escape(url)+'">Проверка данных 2015–2026: эпохи измерений, источники, высоты и ограничения</a>. Подтверждены относительные наблюдения СКРУ-1 за 2016, СКРУ-2 за 2020–2022 и СКРУ-3 за 2021. Абсолютная поверхность DSM относится к съёмке 2011–2015. Издание 2026 не означает съёмку 2026.</p>'
    nav=''.join('<button onclick="openView(\''+url+'\',this)">'+html.escape(title)+'</button>' for title,url in links)
    document='<!doctype html><html lang="ru"><meta charset="utf-8"><title>СКРУ-1 · детальная пространственная модель</title><style>body{margin:0;background:#f5f5f0;color:#203940;font:14px Arial}header{padding:18px 24px}h1{margin:0 0 12px}nav{display:flex;flex-wrap:wrap;gap:7px}button{padding:9px 12px;border:1px solid #9daeb0;border-radius:4px;background:white;cursor:pointer}button.active{background:#244e63;color:white}iframe{border:0;width:100%;height:1040px}p{max-width:1500px;line-height:1.5}a{color:#226781}</style>'
    document+='<header><h1>СКРУ-1 · рельеф, геология и горные работы</h1><p>'+str(counts['field_native_wedges'])+' объёмных элементов региональной геологии; '+str(counts['terrain_triangles'])+' треугольников исходного DSM; '+str(len(plans))+' плановых контуров. Локальные складки и камеры показаны с полной топологией, без прежнего упрощения.</p><nav>'+nav+'</nav><p><a href="detailed_field.vtm">ParaView: полная геология и DSM</a> · <a href="detailed_local.vtm">ParaView: детальные участки, КрII и камеры</a> · <a href="data/detail_manifest.json">Источники, отсчёты и допущения</a> · <a href="../delivery/index.html">Исходный пакет и все скважины</a></p>'+extra+'</header><iframe id="view" title="Детальная геологическая сцена" src="joint.html"></iframe><script>function openView(url,b){document.getElementById("view").src=url;document.querySelectorAll("button").forEach(x=>x.classList.remove("active"));b.classList.add("active")}</script></html>'
    (out/'index.html').write_text(document,encoding='utf-8')
    if pins!={k:sha(p) for k,p in inputs.items()}:raise RuntimeError('inputs changed during detailed build')
    outputs={p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()}
    save(out/'receipt.json',{'schema':'vkm.detailed_geology.receipt/1','inputs_sha256':pins,'outputs_sha256':outputs,'counts':counts,
      'command':sys.argv,'input_unchanged':True,'full_solvers':'NOT_RUN','field_validation':False})
    print(json.dumps(counts,ensure_ascii=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',required=True,type=Path)
    p.add_argument('--terrain',required=True,type=Path);p.add_argument('--plans',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path);p.add_argument('--spacing',type=float,default=10.)
    p.add_argument('--subsidence',type=Path);p.add_argument('--freshness',type=Path)
    build(p.parse_args())
