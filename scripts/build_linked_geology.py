"""Build an explicit, local geological interpretation and dimensioned mining scenarios.

All real data are supplied as paths. Outputs must remain in ignored work/ or PRIVATE.
The package is a hypothesis family with provenance, not a field reconstruction claim.
Run in the QGIS Python runtime; validate volume files independently with OGS checkMesh.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import shutil
from pathlib import Path
import sys

import numpy as np
from shapely.geometry import shape, mapping, Polygon
from shapely.ops import unary_union

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from vkm_world.geometry_build.linked_geology import (
    folded_roof,idw_values,layer_mesh,room_footprints,write_vtu,write_vtm,polygon_parts)

UNITS=[('PDKS','Подстилающая каменная соль','#8b9ba4'),('SZ','Сильвинитовая зона','#b36590'),
       ('SKZ','Сильвинито-карналлитовая зона','#de815e'),('PKS','Покровная каменная соль','#e4adba'),
       ('PP','Переходная пачка','#b5a7c9'),('SMT','Соляно-мергельная толща','#80aec2'),
       ('TKT','Терригенно-карбонатная толща','#c69d72'),('PCT','Пестроцветная толща','#abc58a'),
       ('Q','Четвертичные отложения','#e6d07d')]


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as file:return list(csv.DictReader(file))
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,data):Path(path).write_text(json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def build(args):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import plotly.graph_objects as go
    from plotly.offline import get_plotlyjs

    out=args.output
    repo=Path(__file__).resolve().parents[1]
    target=out.resolve()
    allowed=[(repo/'work').resolve(),args.resources.resolve()]
    if not any(target.is_relative_to(root) for root in allowed):
        raise ValueError('real geometry output must remain under ignored work/ or PRIVATE')
    out.mkdir(parents=True,exist_ok=False);(out/'data').mkdir()
    canonical=args.resources/'11_evidence_vnext/canonical'
    inputs={'mining':args.constraints,'heights':args.heights,
      'intervals':args.package/'geology/interval_observations_local.jsonl',
      'symbols':canonical/'WORLD_PARAMETERS/curated/c4_map/skru1/skru1_boreholes_014_unique.csv',
      'plans':canonical/'WORLD_PARAMETERS/curated/c4_map/skru1/skru1_workings.geojson',
      'fence':args.fence/'source011_fence.json','code':Path(__file__),
      'geometry_code':Path(__file__).resolve().parents[1]/'src/vkm_world/geometry_build/linked_geology.py'}
    if args.temporal:inputs['temporal']=args.temporal
    if args.expansion:inputs['expansion']=args.expansion
    if args.interval_supports:inputs['interval_supports']=args.interval_supports
    if args.section_volume:
        for path in sorted(args.section_volume.iterdir()):
            if path.is_file() and path.suffix in ('.vtp','.json','.html'):
                inputs['section_volume:'+path.name]=path
    pins={k:sha(p) for k,p in inputs.items()}
    mining=read(args.constraints);heights=read(args.heights)['constraints'];symbols=rows(inputs['symbols'])
    plans=read(inputs['plans'])['features'];fence=read(inputs['fence'])
    intervals=[json.loads(s) for s in inputs['intervals'].read_text(encoding='utf-8').splitlines()]
    if args.interval_supports:
        extra_intervals=read(args.interval_supports)
        intervals.extend(extra_intervals['supports'])
        save(out/'data/additional_interval_supports.json',extra_intervals)
    regions=mining['regions']
    regional_seam_thickness=max(r['geological_thickness']['value_m'] for r in regions)
    # Convex hull is an explicit research envelope, not a mine boundary or salt limit.
    domain=unary_union([shape(f['geometry']) for f in plans if f['properties'].get('kind') in ('BLOCK','PANEL')]).convex_hull
    save(out/'data/model_domain.geojson',{'type':'FeatureCollection','features':[{'type':'Feature','geometry':mapping(domain),
       'properties':{'status':'MODEL_CHOICE','role':'convex hull of mapped block/panel domains, not mine boundary','frame':'SKRU1_LOCAL_SH1'}}]})
    brackets=[c for c in heights if c['kind']=='VERTICAL_CONTACT_GRAPHICAL_BRACKET']
    # Explicit ordered quantile realization of source040 brackets. Source brackets are never overwritten.
    depth=[]
    for c in brackets:
        fraction=1/3 if c['lower_unit']=='PP' else 2/3 if c['upper_unit']=='PP' else .5
        depth.append(c['depth_min_m']+fraction*(c['depth_max_m']-c['depth_min_m']))
    if any(a>=b for a,b in zip(depth,depth[1:])):raise ValueError('shaft realization violates stratigraphic order')
    shaft_xy=np.array(brackets[0]['c4_coordinate_candidates'][0]['xy'])
    # Source012 roof branch: same-number interval differences imply first-unit thickness only in this branch.
    grouped={name:np.array([[r['x'],r['y'],r['value_m']] for r in intervals if r['interval_id']==name])
       for name in ('TKT_to_SMT','SMT_to_PKS','SKZ_to_SZ','SZ_to_PDKS')}
    shaft_intervals={'TKT_to_SMT':depth[2]-depth[1], 'SMT_to_PKS':depth[4]-depth[2],
                     'SKZ_to_SZ':depth[6]-depth[5], 'SZ_to_PDKS':depth[7]-depth[6]}
    for key,value in shaft_intervals.items():grouped[key]=np.vstack([grouped[key], [*shaft_xy,value]])
    variants=[{'id':'low','roof_quantile':.15,'kr2_fraction_in_SZ':.5,'phase':0.},
              {'id':'central','roof_quantile':.5,'kr2_fraction_in_SZ':.8,'phase':1.1},
              {'id':'high','roof_quantile':.85,'kr2_fraction_in_SZ':.95,'phase':2.2}]
    metadata=[];all_meshes={};model_functions={};entries=[]

    def make_figure(title,ztitle='Z сценария в отсчёте SRC252, м'):
        fig=go.Figure();fig.update_layout(title={'text':title,'x':.025},height=790,
            margin=dict(l=0,r=0,t=75,b=0),paper_bgcolor='#f6f6f1',font=dict(family='Arial',size=13),
            legend=dict(x=1.01,y=.98),scene=dict(xaxis_title='X LOCAL SH1, м · восток',yaxis_title='Y LOCAL SH1, м · север',
              zaxis_title=ztitle,aspectmode='manual',aspectratio=dict(x=1,y=1.1,z=.65),camera=dict(eye=dict(x=1.2,y=-1.6,z=.9))))
        return fig

    def add_mesh(fig,mesh,name,color,opacity=1.,visible=True,legend=True):
        p=mesh['points'];f=mesh['faces']
        fig.add_trace(go.Mesh3d(x=p[:,0],y=p[:,1],z=p[:,2],i=f[:,0],j=f[:,1],k=f[:,2],name=name,
            color=color,opacity=opacity,visible=visible,showlegend=legend,flatshading=False,
            lighting=dict(ambient=.55,diffuse=.8,specular=.15,roughness=.8),
            hovertemplate=html.escape(name)+'<br>X=%{x:.1f}; Y=%{y:.1f}; Z=%{z:.1f} м<extra></extra>'))

    world=make_figure('СКРУ-1 · геологическая интерпретация поля<br><sup>Три варианта; верхние толщи скрываются щелчком в легенде. Геометрия между опорами — интерполяция.</sup>')
    for variant in variants:
        q=variant['roof_quantile'];roof_support=[]
        for r in regions:
            pt=shape(r['plan_support']['geometry']).representative_point();zr=r['roof']
            roof_support.append([pt.x,pt.y,zr['z_min_m']+q*(zr['z_max_m']-zr['z_min_m'])])
        roof_support=np.array(roof_support)
        def interfaces(xy,roof_support=roof_support,frac=variant['kr2_fraction_in_SZ'],kr2_override=None,seam_thickness=None):
            if seam_thickness is None:seam_thickness=regional_seam_thickness
            kr2=idw_values(xy,roof_support) if kr2_override is None else np.asarray(kr2_override)
            thickness={k:idw_values(xy,v) for k,v in grouped.items()}
            sz=thickness['SZ_to_PDKS'];skz=thickness['SKZ_to_SZ']
            if np.any(sz<seam_thickness):raise ValueError('SZ cannot contain the interpreted KrII seam')
            top_sz=kr2+frac*(sz-seam_thickness);bottom_sz=top_sz-sz
            top_skz=top_sz+skz
            top_pks=top_skz+(depth[5]-depth[4])
            top_pp=top_pks+(depth[4]-depth[3])
            top_smt=top_pks+thickness['SMT_to_PKS']
            top_tkt=top_smt+thickness['TKT_to_SMT']
            top_pct=top_tkt+(depth[1]-depth[0]);top_q=top_pct+depth[0]
            bounds=[bottom_sz-50,bottom_sz,top_sz,top_skz,top_pks,top_pp,top_smt,top_tkt,top_pct,top_q]
            if any(np.any(a>=b) for a,b in zip(bounds,bounds[1:])):raise ValueError('crossed interpreted strata')
            return bounds
        model_functions[variant['id']]=interfaces
        variant_meshes=[]
        # Variable-thickness interface pair uses common triangulation, preserving conforming boundaries.
        reference=layer_mesh(domain,lambda xy:np.zeros(len(xy)),1,spacing=args.field_spacing)
        n=len(reference['points'])//2;xy=reference['points'][:n,:2];bounds=interfaces(xy)
        for index,(unit,name,color) in enumerate(UNITS):
            points=np.vstack([np.column_stack([xy,bounds[index]]),np.column_stack([xy,bounds[index+1]])])
            mesh={**reference,'points':points}
            tri=mesh['wedges'][:,:3];a=xy[tri[:,1]]-xy[tri[:,0]];b=xy[tri[:,2]]-xy[tri[:,0]];areas=np.abs(a[:,0]*b[:,1]-a[:,1]*b[:,0])/2
            thickness=bounds[index+1]-bounds[index];mesh['volume_m3']=float(np.sum(areas*np.mean(thickness[tri],axis=1)))
            stem=f'field_{variant["id"]}_{unit}';write_vtu(out/'data'/f'{stem}.vtu',mesh,index)
            entries.append((stem,f'data/{stem}.vtu'));variant_meshes.append(mesh)
            visible=True if variant['id']=='central' else False
            add_mesh(world,mesh,name,color,opacity=1.,visible=visible)
            metadata.append({'id':stem,'kind':'INTERPRETED_REGIONAL_LAYER','unit':unit,'variant':variant,
                'status':['INTERPOLATION','MODEL_CHOICE'],'volume_m3':mesh['volume_m3'],'file':f'data/{stem}.vtu',
                'datum':'SCENARIO_IN_SOURCE252_FRAME; source040 brackets used as thickness, not common absolute Z',
                'source_inputs':['source012 interval differences ROOF branch','source040 ordered bracket realization','source252 areal roof quantiles'],
                'validity':'hypothesis; entire regional domain extrapolates sparse supports; no accepted geological surface'})
        all_meshes[variant['id']]=variant_meshes
    # Scenario selector preserves geological unit visibility and makes unsupported choices explicit.
    buttons=[]
    for vi,variant in enumerate(variants):
        visible=[False]*len(world.data)
        for j,unit in enumerate(UNITS):visible[vi*len(UNITS)+j]=True
        buttons.append(dict(label=variant['id'],method='update',args=[{'visible':visible}]))
    world.update_layout(updatemenus=[dict(type='buttons',direction='left',x=.02,y=1.03,buttons=buttons)])

    mining_fig=make_figure('КрII · складчатые объёмы и камерная структура<br><sup>Камеры: размерный сценарий по ПРГР-2013, азимут и фаза условные; это не фактический выработанный контур.</sup>')
    local_fig=make_figure('Согласованная локальная геология · девять толщ и КрII<br><sup>Каждая область имеет общую сетку интерфейсов. Укрупнённое отображение; точные сетки — в VTU.</sup>')
    region_meshes=[];room_meshes=[];room_records=[]
    local_entries=[];containment=[];local_visuals=[]
    for index,r in enumerate(regions):
        polygon=shape(r['plan_support']['geometry']);zr=r['roof'];fold=r['folds'];mp=r['mining_2013']
        wings=fold.get('wing_span_m_bounds');amplitudes=fold.get('amplitude_m_bounds')
        if not wings or not amplitudes:raise ValueError('source fold dimensions required; no silent geological defaults')
        wing=max(wings);amplitude=sum(amplitudes)/2
        direction=0. if zr['dip_direction_as_printed'].startswith('west') else 180.
        def roof(xy,p=polygon,z=zr,w=wing,a=amplitude,d=direction):
            return folded_roof(xy,p.bounds,z['z_min_m'],z['z_max_m'],w,a,1.1,d)
        thick=r['geological_thickness']['value_m'];spacing=min(args.seam_spacing,wing/4)
        mesh=layer_mesh(polygon,roof,thick,spacing=spacing)
        stem=f'KR2_{index}';write_vtu(out/'data'/f'{stem}.vtu',mesh,20+index);entries.append((r['label'],f'data/{stem}.vtu'))
        add_mesh(mining_fig,mesh,f'КрII · {r["label"]}',UNITS[1][2],.42)
        region_meshes.append((r,mesh));metadata.append({'id':stem,'kind':'INTERPRETED_SEAM_SOLID','status':['INTERPOLATION','MODEL_CHOICE'],
           'source_constraint':r,'file':f'data/{stem}.vtu','volume_m3':mesh['volume_m3'],
           'model_choices':{'fold_wing_half_period_m':wing,'fold_peak_to_trough_m':amplitude,'fold_phase_rad':1.1,
            'fold_direction_deg':direction,'whole_named_polygon_as_work_subset':True,'thickness_orientation':'vertical',
            'mesh_spacing_m':spacing,'fold_wing_selected_from_source_range':'upper endpoint; computational MODEL_CHOICE'}})
        # The local stack and KrII use exactly the same XY connectivity and roof realization.
        n=len(mesh['points'])//2;xy=mesh['points'][:n,:2];kr2=roof(xy)
        local_bounds=model_functions['central'](xy,kr2_override=kr2,seam_thickness=thick)
        margin_top=float(np.min(local_bounds[2]-kr2));margin_bottom=float(np.min(kr2-thick-local_bounds[1]))
        if min(margin_top,margin_bottom)<-1e-8:raise ValueError('local KrII leaves its containing SZ')
        containment.append({'region':r['region_id'],'min_margin_to_SZ_roof_m':margin_top,
            'min_margin_to_SZ_floor_m':margin_bottom,'shared_XY_connectivity':True,'no_stratigraphic_crossing':True})
        # Smaller display mesh is explicitly LOD only; exported meshes retain fold resolution.
        lod=layer_mesh(polygon,roof,thick,spacing=max(20.,spacing));ln=len(lod['points'])//2;lxy=lod['points'][:ln,:2]
        lbounds=model_functions['central'](lxy,kr2_override=roof(lxy),seam_thickness=thick)
        for ui,(unit,name,color) in enumerate(UNITS):
            local={**mesh,'points':np.vstack([np.column_stack([xy,local_bounds[ui]]),np.column_stack([xy,local_bounds[ui+1]])])}
            stem=f'local_{index}_{unit}';write_vtu(out/'data'/f'{stem}.vtu',local,ui)
            local_entries.append((r['label']+' '+unit,f'data/{stem}.vtu'))
            metadata.append({'id':stem,'kind':'CONFORMING_LOCAL_LAYER','status':['INTERPOLATION','MODEL_CHOICE'],
                 'unit':unit,'region':r['region_id'],'file':f'data/{stem}.vtu','same_XY_as_KrII':True,
                 'container_check':containment[-1],'field_base_geometry':'SEPARATE_COARSE_HYPOTHESIS_NOT_MERGED',
                 'source040_thickness_prior':'spatial extrapolation, not local observation'})
            visual={**lod,'points':np.vstack([np.column_stack([lxy,lbounds[ui]]),np.column_stack([lxy,lbounds[ui+1]])])}
            add_mesh(local_fig,visual,name,color,1.,visible=True if index==0 else False)
        local_visuals.append(r['label'])
        # Dimensioned strips: exact chamber count, azimuth, mouth locations and dates remain unknown.
        azimuth=90.;phase=.25
        footprints=room_footprints(polygon,mp['chamber_width_m'],mp['pillar_width_m'],azimuth,phase)
        chamber_height=mp['extracted_height_m']
        if chamber_height is None:continue
        for j,footprint in enumerate(footprints):
            room=layer_mesh(footprint,roof,chamber_height,spacing=min(wing/4,5.))
            stem=f'rooms_{index}_{j:03d}';write_vtu(out/'data'/f'{stem}.vtu',room,100+index)
            entries.append((stem,f'data/{stem}.vtu'));room_meshes.append((r,room))
            add_mesh(mining_fig,room,f'Камеры-сценарий · {r["label"]}', '#253a46',1.,legend=j==0)
            room_records.append({'id':stem,'region':r['region_id'],'file':f'data/{stem}.vtu','volume_m3':room['volume_m3'],
               'geometry_role':'POTENTIAL_CHAMBER_SCENARIO_NOT_ACTUAL_EXCAVATION','plan_identity':None,
               'azimuth_deg_MODEL_CHOICE':azimuth,'phase_fraction_MODEL_CHOICE':phase,
               'width_m':mp['chamber_width_m'],'height_m':chamber_height,'pillar_m':mp['pillar_width_m'],
               'height_semantics':mp['height_semantics'],'calendar_type':mp['planned_or_actual'],
               'korzh_side_MODEL_CHOICE':'entire extra height placed BELOW geological floor; source does not establish side',
               'roof_offset_MODEL_CHOICE_m':0.,'alternate_pillar_width_as_source':mp.get('alternate_pillar_width_m'),
               'active_clean_mining_months_2013':mp['active_months_2013'],'source_calendar':mp['calendar_row_id'],
               'monthly_work_source':mp.get('source_monthly_work'),
               'individual_chamber_sequence':'UNKNOWN; no fictional cumulative excavation animation'})
    local_buttons=[]
    for ri,label in enumerate(local_visuals):
        visible=[False]*len(local_fig.data)
        for ui in range(len(UNITS)):visible[ri*len(UNITS)+ui]=True
        local_buttons.append(dict(label=label,method='update',args=[{'visible':visible}]))
    local_fig.update_layout(updatemenus=[dict(type='dropdown',x=.02,y=1.03,buttons=local_buttons)])
    # The source section remains independent in Z. No hard registration is inferred by display.
    section_fig=make_figure('Разрез 011 на плане · исходная геометрия и объёмная интерпретация', 'Z источника 011, м · datum UNKNOWN')
    for u in fence['units']:
        p=np.asarray(u['triangle_vertices_xyz_m']);f=np.asarray(u['triangles']);mesh={'points':p,'faces':f}
        add_mesh(section_fig,mesh,u['unit_as_printed'],'rgb('+','.join(map(str,u['color_rgb']))+')',1.)
    for line in fence['support_lines']:
        if not isinstance(line,dict):continue
        points=line.get('mapped_xyz_m') or line.get('xyz_m')
        if points:
            a=np.asarray(points);section_fig.add_trace(go.Scatter3d(x=a[:,0],y=a[:,1],z=a[:,2],mode='lines',name=str(line.get('id','Опора'))))

    # Every reviewed plan symbol is shown in 2D, avoiding fictitious collar elevations.
    plan=go.Figure()
    for r in regions:
        for part in polygon_parts(shape(r['plan_support']['geometry'])):
            a=np.asarray(part.exterior.coords);plan.add_trace(go.Scatter(x=a[:,0],y=a[:,1],mode='lines',fill='toself',
              fillcolor='rgba(179,101,144,.15)',line=dict(width=1,color='#90577b'),name=r['label']))
    for f in plans:
        if f['properties'].get('kind')!='BLOCK':continue
        for part in polygon_parts(shape(f['geometry'])):
            a=np.asarray(part.exterior.coords);plan.add_trace(go.Scatter(x=a[:,0],y=a[:,1],mode='lines',line=dict(width=.7,color='#c3c7c5'),showlegend=False,hoverinfo='skip'))
    plan.add_trace(go.Scatter(x=[float(s['x_local_m']) for s in symbols],y=[float(s['y_local_m']) for s in symbols],
         mode='markers+text',text=[s['label_checked'] for s in symbols],textposition='top center',textfont=dict(size=9),
         marker=dict(size=6,color='#2d6877'),name=f'Проверенные знаки скважин: {len(symbols)}',
         hovertext=[f"{s['label_checked']} · {s['borehole_id_catalogue'] or 'identity unresolved'}<br>{s['position_source']}<br>Устье Z UNKNOWN; знак не равен колонке" for s in symbols],hoverinfo='text'))
    plan.update_layout(title='План: все проверенные знаки скважин и области геологической сборки',height=800,
       xaxis_title='X LOCAL SH1, м',yaxis=dict(title='Y LOCAL SH1, м',scaleanchor='x',scaleratio=1),font=dict(family='Arial',size=13))

    # Geological sections derived from the same 3D interpolation, not redrawn independently.
    bounds=domain.bounds;yc=(bounds[1]+bounds[3])/2;x=np.linspace(bounds[0],bounds[2],250)
    xy=np.column_stack([x,np.full(len(x),yc)]);levels=model_functions['central'](xy)
    fig,axes=plt.subplots(1,2,figsize=(16,7),gridspec_kw={'width_ratios':[1.4,1]})
    for i,(unit,name,color) in enumerate(UNITS):axes[0].fill_between(x,levels[i],levels[i+1],color=color,label=name)
    axes[0].set(xlabel='X LOCAL SH1, м',ylabel='Z сценария SRC252, м',title=f'Производный разрез при Y={yc:.0f} м · центральный вариант')
    axes[0].grid(alpha=.2);axes[0].legend(fontsize=8,loc='lower left',ncol=2)
    for r,mesh in region_meshes:
        poly=shape(r['plan_support']['geometry'])
        for part in polygon_parts(poly):
            a=np.asarray(part.exterior.coords);axes[1].fill(a[:,0],a[:,1],color='#bb7e9f',alpha=.3)
        center=poly.representative_point();axes[1].text(center.x,center.y,r['label'],ha='center',fontsize=9)
    for r,mesh in room_meshes:
        p=mesh['points'];axes[1].plot(p[:,0],p[:,1],'.',ms=.25,color='#263e4c',alpha=.5)
    axes[1].set(aspect='equal',xlabel='X LOCAL SH1, м',ylabel='Y LOCAL SH1, м',title='Размерный сценарий камер · не фактическая отработка')
    fig.suptitle('СКРУ-1 · интерпретация геологии и выработок по корпусу',fontsize=17)
    fig.text(.02,.02,'Между опорами: INTERPOLATION. Корреляция, отсчёты, фазы и границы сценария: MODEL_CHOICE. Полевое подтверждение не выполнено.',fontsize=9)
    fig.tight_layout(rect=(0,.05,1,.95));fig.savefig(out/'geology_and_rooms.png',dpi=180);plt.close(fig)

    save(out/'data/geometry_manifest.json',{'schema':'vkm.linked_geological_interpretation/1','features':metadata,'chamber_scenarios':room_records,
       'variants':variants,'source040_ordered_depth_realization_m':depth,'source040_original_brackets':brackets,
       'source012_interval_inputs':intervals,'source012_branch':'ROOFS; floor branch changes unit attribution and remains competing',
       'source012_observed_intervals_preserved_separately':True,
       'scenario_interpolator_supports_include_source040_thickness_prior':True,
       'source040_prior_support_values_m':shaft_intervals,'local_KrII_SZ_containment':containment,
       'global_model_choices':{'IDW_power':2,'extrapolation_outside_support_hulls':True,'regional_domain':'convex hull of source plan BLOCK/PANEL',
           'KrII_relative_position_in_SZ':'unknown; fraction of remaining SZ thickness above seam, not measured tie','PDKS_bottom_cap_m_below_SZ':50,
           'regional_vs_local':'separate geological hypotheses; do not merge their overlapping coarse/refined meshes',
           'heights':'in SRC252 scenario frame; no common datum established with DSM, SRC011, SRC040',
           'source040_bracket_realization':'.5 quantile except sequential PP boundaries at 1/3 and2/3; explicit interpretation',
           'shaft_constraints_used_as_spatial_thickness_prior':'MODEL_CHOICE, spatial representativeness not established'},
       'backfill':mining['backfill_events'],'source_mining_constraints':mining,
       'mesh_readiness':'DIAGNOSTIC_MULTIBLOCK_NOT_SOLVER_MESH; SZ and nested KrII overlap, rooms use a separate discretization',
       'field_validation':False,'solver_execution':'NOT_RUN'})
    write_vtm(out/'linked_geology.vtm',entries)
    write_vtm(out/'local_geology.vtm',local_entries+[(r['label'],f'data/KR2_{i}.vtu') for i,(r,_) in enumerate(region_meshes)])
    plotlyjs=get_plotlyjs();sections=[]
    for key,title,figure,caption in [
      ('world','Геология всего поля',world,'Рабочая интерпретация девяти толщ. Центральный вариант — один из трёх сценариев. Между опорами IDW; за их охватом экстраполяция. Щелчком в легенде скрывайте верхние толщи.'),
      ('local','Согласованная локальная геология',local_fig,'Девять толщ над каждой из семи площадей. КрII находится внутри СЗ; порядок и общие интерфейсы проверяются на точных сетках. Региональная гипотеза и локальные вставки показываются отдельно.'),
      ('mining','КрII и камеры',mining_fig,'Диапазоны отметок, мощности, складчатость и размеры системы разработки связаны с семью плановыми площадями. Размещение отдельных камер и фазы складок условные.'),
      ('section','Разрез 011',section_fig,'Разрез привязан к локальному плану. Отсчёт высот источника 011 сохраняется отдельно; его совмещение с источником252 не установлено.'),
      ('plan','Скважины и план',plan,'Показан полный проверенный набор знаков, включая скважины без численных отбивок. Количество знаков не считается количеством полноценных колонок.')]:
        sections.append(f'<section id="{key}" class="view"><p>{caption}</p>'+figure.to_html(full_html=False,include_plotlyjs=False,div_id=f'plot-{key}')+'</section>')
    context_html=''
    if args.temporal:
        temporal=read(args.temporal);save(out/'data/temporal_evidence.json',temporal)
        items=[]
        for r in temporal['records']:
            values={k:v for k,v in r.items() if k in ('source_id','page_id','data_as_of','publication_year','data_kind','usable_now','geometry_allocation','interpretation_guard')}
            items.append('<li>'+html.escape(json.dumps(values,ensure_ascii=False))+'</li>')
        context_html+='<details class="note"><summary>Поздние сведения и даты наблюдений · 2022–2026</summary><p>Поздние ограничения хранятся отдельно от пространственного плана 2013 года. Дата публикации не заменяет дату измерения.</p><ul>'+''.join(items)+'</ul><a href="data/temporal_evidence.json">Все даты, значения, источники и ограничения</a></details>'
    if args.expansion:
        expansion=read(args.expansion);save(out/'data/additional_supports.json',expansion)
        context_html+='<details class="note"><summary>Дополнительные скважинные и геологические опоры</summary><p>Новые численные контакты, повторно проверенные интервалы, дополнительные знаки и группы подземных скважин разделены по типу доказательства.</p><a href="data/additional_supports.json">Результат расширенного поиска и отсеянные кандидаты</a></details>'
    if args.interval_supports:
        context_html+='<p class="note">Интервальные опоры: '+str(len(intervals))+' интервалов по '+str(len(set(r['borehole_id'] for r in intervals)))+' скважинам. Две дополнительные позиции выбраны внутри исходных зон как MODEL_CHOICE; альтернативы и исходные контакты: <a href="data/additional_interval_supports.json">данные привязки</a>.</p>'
    if args.section_volume:
        (out/'source011_volume').mkdir()
        for key,path in inputs.items():
            if key.startswith('section_volume:'):shutil.copyfile(path,out/'source011_volume'/path.name)
        context_html+='<p class="note"><a href="source011_volume/index.html">Объёмная геология по привязанному разрезу011: семь тел, три варианта боковой выдержанности</a>. Исходный разрез сохранён точно; боковое продолжение является MODEL_CHOICE.</p>'
    table=''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [r['label'],r['roof']['z_min_m'],r['roof']['z_max_m'],r['geological_thickness']['value_m'],r['mining_2013']['extracted_height_m_as_printed'],r['mining_2013']['chamber_width_m'],r['mining_2013']['pillar_width_m'],r['mining_2013']['source_start_as_printed'],r['mining_2013']['planned_or_actual']])+'</tr>' for r in regions)
    document='<!doctype html><html lang="ru"><meta charset="utf-8"><title>СКРУ-1 · геологическая модель</title><style>body{margin:0;background:#f6f6f1;color:#1e343c;font:15px Arial}header{padding:24px 32px 12px}h1{margin:0 0 12px}nav{display:flex;gap:8px;padding:0 32px}button{padding:10px 18px;border:1px solid #7b9b9b;background:white;cursor:pointer;border-radius:5px}.view{padding:0 24px}p{max-width:1300px;line-height:1.5}table{border-collapse:collapse;margin:20px 32px}td,th{padding:8px;border:1px solid #bbb}.note{margin:24px 32px}a{color:#176a80}</style>'
    document+='<script>'+plotlyjs+'</script><header><h1>СКРУ-1 · геология, разрезы и камерная система</h1><p>Геологические тела собраны из источников корпуса. Формы между опорами являются интерпретацией; три варианта не задают статистический доверительный интервал.</p></header><nav>'
    document+=''.join(f'<button onclick="showView(\'{key}\')">{title}</button>' for key,title in [('world','Геология поля'),('local','Геология участков'),('mining','КрII и камеры'),('section','Разрез на плане'),('plan','Все скважины')])+'</nav>'+''.join(sections)
    document+='<div class="note"><a href="linked_geology.vtm">Региональные варианты и камеры · ParaView</a> · <a href="local_geology.vtm">Согласованные локальные толщи · ParaView</a> · <a href="data/geometry_manifest.json">Источники и допущения</a> · <a href="geology_and_rooms.png">Разрез и план камер</a><p>Шкалы: XY — локальные метры; Z — отсчёт выбранного источника или сценария. Масштаб Z на интерактивном обзоре увеличен для различимости толщ. Диагностические наборы включают вложенные перекрывающиеся объекты; это не готовая непересекающаяся сетка решателя. План 2013 остаётся планом 2013; подтверждение исполнения и состояние 2026 UNKNOWN.</p></div>'
    document+=context_html+'<table><tr><th>Площадь</th><th>Кровля min, м</th><th>max, м</th><th>Геол.мощность</th><th>Вынимаемая</th><th>Камера</th><th>Целик</th><th>Период</th><th>Статус</th></tr>'+table+'</table>'
    document+="<script>function showView(id){document.querySelectorAll('.view').forEach(s=>{s.style.display=s.id===id?'block':'none'});Plotly.Plots.resize(document.getElementById('plot-'+id));}showView('world');</script></html>"
    (out/'index.html').write_text(document,encoding='utf-8')
    if pins!={k:sha(p) for k,p in inputs.items()}:raise RuntimeError('input changed during build')
    outputs={str(p.relative_to(out)).replace('\\','/'):sha(p) for p in sorted(out.rglob('*')) if p.is_file()}
    receipt={'schema':'vkm.linked_geology.build_receipt/1','inputs_sha256':pins,'outputs_sha256':outputs,
       'counts':{'regional_layers':len(UNITS),'variants':len(variants),'regional_volume_meshes':len(UNITS)*len(variants),'conforming_local_layers':len(local_entries),
                 'seam_regions':len(region_meshes),'chamber_scenario_pieces':len(room_records),'reviewed_plan_symbols':len(symbols),
                 'numeric_interval_wells':len(set(r['borehole_id'] for r in intervals))},
       'checks':{'input_unchanged':True,'positive_ordered_layers':True,'triangulation_area_preserved':True},
       'scientific_status':'INTERPRETATION_HYPOTHESIS_FAMILY; no field validation or inference of2026 state',
       'runtime':{'python':sys.version,'numpy':np.__version__},'full_solvers':'NOT_RUN'}
    save(out/'receipt.json',receipt);print(json.dumps(receipt['counts'],ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources',type=Path,default=os.environ.get('VKM_RESOURCES_ROOT'))
    parser.add_argument('--constraints',required=True,type=Path)
    parser.add_argument('--heights',required=True,type=Path)
    parser.add_argument('--package',required=True,type=Path)
    parser.add_argument('--fence',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--temporal',type=Path)
    parser.add_argument('--expansion',type=Path)
    parser.add_argument('--interval-supports',type=Path)
    parser.add_argument('--section-volume',type=Path)
    parser.add_argument('--field-spacing',type=float,default=200.)
    parser.add_argument('--seam-spacing',type=float,default=8.)
    args=parser.parse_args()
    if args.resources is None:parser.error('--resources or VKM_RESOURCES_ROOT required')
    build(args)
