"""Real installed PyQGIS/GDAL acceptance on bounded synthetic data; never closed fixtures."""
import json
import os
import subprocess
import uuid
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from vkm_qgis.fitting import apply_points
from vkm_qgis.policy import Paths
from vkm_qgis.service import QgisService,runtime_path,digest

pytestmark=[pytest.mark.qgis_runtime, pytest.mark.skipif(not os.environ.get('VKM_QGIS_ROOT') and not os.environ.get('VKM_QGIS_PYTHON'),reason='Existing QGIS runtime not configured')]

def test_real_synthetic_roundtrip():
    root=Path(__file__).resolve().parents[2]
    paths=Paths(root,root/'work')
    service=QgisService(paths)
    prefix='work:qgis_2026-09-30/synthetic/'+uuid.uuid4().hex
    calls=[]
    def call(tool,**args):
        out=service.call(tool,args); calls.append(out)
        assert out['ok'],out
        return out['result']
    def failure(code,tool,**args):
        out=service.call(tool,args); calls.append(out)
        assert not out['ok'] and out['error']['code']==code,out
    status=call('qgis_status'); assert status['available'] and not status['gpu']
    gpkg=prefix+'/geometry.gpkg'
    create=call('qgis_create_geopackage',path=gpkg,frame_id='SYNTHETIC_LOCAL_M')
    assert len(create['layers'])==15
    layers=call('qgis_list_layers',path=gpkg)['layers']
    assert len(layers)==15 and all(x['feature_count']==0 for x in layers)
    assert all(x['authority_id'] is None and x['coordinate_reference']['epsg'] is None for x in layers)
    failure('OUTPUT_EXISTS','qgis_create_geopackage',path=gpkg)
    call('qgis_create_layer',path=gpkg,name='source_plan',geometry_type='Polygon',crs_kind='SOURCE_COORDINATES',frame_id='SYNTHETIC_PIXEL',unit='px')
    a={'type':'Polygon','coordinates':[[[0,0],[10,0],[10,10],[0,10],[0,0]]]}
    props={'source_id':'SYNTHETIC','page_id':'SYNTHETIC-P1','figure_id':'SYNTHETIC-F1','provenance':{'fixture':'synthetic'},'custom_source_key':'retain extra properties'}
    call('qgis_feature',path=gpkg,layer='source_plan',action='add',stable_id='A',geometry=a,properties=props)
    failure('DUPLICATE_STABLE_ID','qgis_feature',path=gpkg,layer='source_plan',action='add',stable_id='A',geometry=a)
    before=digest(paths.resolve(gpkg))
    failure('STATUS_POLICY','qgis_feature',path=gpkg,layer='source_plan',action='update',stable_id='A',properties={'epistemic_status':'FACT'})
    assert digest(paths.resolve(gpkg))==before
    failure('FILE_CONFLICT','qgis_feature',path=gpkg,layer='source_plan',action='update',stable_id='A',properties={'uncertainty':'x'},expected_sha256='0'*64)
    call('qgis_feature',path=gpkg,layer='source_plan',action='update',stable_id='A',properties={'uncertainty':'synthetic ±1 px'},expected_sha256=before)
    read=call('qgis_feature',path=gpkg,layer='source_plan',action='get',stable_id='A')['feature']
    assert read['properties']['uncertainty']=='synthetic ±1 px' and read['geometry']==a
    listed=call('qgis_feature',path=gpkg,layer='source_plan',action='list',limit=1)
    assert len(listed['features'])==1
    geojson=prefix+'/source.geojson'
    call('qgis_export_layer',path=gpkg,layer='source_plan',output_path=geojson)
    exported=json.loads(paths.resolve(geojson).read_text(encoding='utf-8'))
    assert exported['features'][0]['geometry']==a and exported['vkm_coordinate_reference']['epsg'] is None
    call('qgis_import_vector',source_path=geojson,path=gpkg,name='source_copy',frame_id='SYNTHETIC_PIXEL',unit='px')
    copied=call('qgis_feature',path=gpkg,layer='source_copy',action='get',stable_id='A')['feature']
    assert copied['geometry']==a
    assert json.loads(copied['properties']['properties_json'])['custom_source_key']=='retain extra properties'
    # Distinct source geometry is imported, retaining source coordinates instead of applying a CRS.
    b={'type':'FeatureCollection','features':[{'type':'Feature','properties':{'stable_id':'B',**props},'geometry':
        {'type':'Polygon','coordinates':[[[5,0],[15,0],[15,10],[5,10],[5,0]]]}}]}
    bpath=paths.new_output(prefix+'/b.geojson'); bpath.write_text(json.dumps(b),encoding='utf-8')
    call('qgis_import_vector',source_path=prefix+'/b.geojson',path=gpkg,name='source_copy',frame_id='SYNTHETIC_PIXEL',unit='px',append=True)
    assert call('qgis_feature',path=gpkg,layer='source_copy',action='list')['count']==2
    failure('DUPLICATE_STABLE_ID','qgis_import_vector',source_path=prefix+'/b.geojson',path=gpkg,name='source_copy',frame_id='SYNTHETIC_PIXEL',unit='px',append=True)
    points=[[0,0],[10,0],[0,10],[10,10],[3,7]]
    sim=[[1.5,-.5,123],[.5,1.5,-37],[0,0,1]]
    gcp=[{'id':'G'+str(i),'source':s,'target':d,'source_id':'SYNTHETIC','source_figure_id':'SYNTHETIC-F1'} for i,(s,d) in enumerate(zip(points,apply_points(points,sim)))]
    call('qgis_register_control_points',path=gpkg,source_frame_id='SYNTHETIC_PIXEL',target_frame_id='SYNTHETIC_LOCAL_M',points=gcp,source_unit='px',target_unit='m',target_unit_verified=True)
    point=call('qgis_feature',path=gpkg,layer='control_points',stable_id='G0')['feature']
    assert point['geometry']['coordinates']==gcp[0]['target']
    original=digest(paths.resolve(gpkg))
    failure('DUPLICATE_STABLE_ID','qgis_register_control_points',path=gpkg,source_frame_id='SYNTHETIC_PIXEL',target_frame_id='SYNTHETIC_LOCAL_M',points=gcp,source_unit='px',target_unit='m',target_unit_verified=True)
    assert digest(paths.resolve(gpkg))==original
    failure('GCP_ROLE_POLICY','qgis_register_control_points',path=gpkg,source_frame_id='SYNTHETIC_PIXEL',target_frame_id='SYNTHETIC_LOCAL_M',
            points=[{**p,'id':p['id']+'_BAD','role':'OBSERVED_SUBSIDENCE'} for p in gcp])
    assert digest(paths.resolve(gpkg))==original
    transform=prefix+'/similarity.json'
    fit=call('qgis_fit_transform',path=gpkg,output_path=transform,model='similarity')
    np.testing.assert_allclose(fit['matrix'],sim,atol=1e-11)
    residual=call('qgis_residuals',transform_path=transform)
    assert residual['rms']<1e-11 and residual['leave_one_out']['status']=='PASS'
    affine=[[2,.3,40],[-.2,1.7,-20],[0,0,1]]
    agcp=[{'id':'AG'+str(i),'source':s,'target':d} for i,(s,d) in enumerate(zip(points,apply_points(points,affine)))]
    call('qgis_register_control_points',path=gpkg,source_frame_id='SYNTHETIC_AFFINE_PIXEL',target_frame_id='SYNTHETIC_LOCAL_M',points=agcp,source_unit='px',target_unit='m',target_unit_verified=True)
    afit=call('qgis_fit_transform',path=gpkg,output_path=prefix+'/affine.json',model='affine',control_point_ids=[p['id'] for p in agcp])
    np.testing.assert_allclose(afit['matrix'],affine,atol=1e-11)
    metric=prefix+'/metric.gpkg'
    call('qgis_apply_transform',path=gpkg,layer='source_plan',transform_path=transform,output_path=metric,output_layer='transformed')
    transformed=call('qgis_feature',path=metric,layer='transformed',action='list')['features'][0]
    np.testing.assert_allclose(transformed['geometry']['coordinates'][0],apply_points(a['coordinates'][0],sim),atol=1e-10)
    assert transformed['properties']['epistemic_status']=='DERIVATION'
    for op,expected in [('intersection',100),('difference',0),('union',150)]:
        out=call('qgis_overlay',left_path=gpkg,left_layer='source_plan',right_path=gpkg,right_layer='source_copy',
                 operation=op,output_path=prefix+'/'+op+'.gpkg',output_layer=op)
        # source_copy contains both A and B: intersection is A∩A + A∩B = 150.
        area=150 if op=='intersection' else expected
        assert abs(out['area']-area)<1e-9
    failure('FRAME_MISMATCH','qgis_overlay',left_path=gpkg,left_layer='source_plan',right_path=metric,right_layer='transformed',output_path=prefix+'/bad.gpkg',output_layer='bad')
    image=prefix+'/render.png'; call('qgis_render',path=metric,layers=['transformed'],output_path=image,width=320,height=240)
    png=Image.open(paths.resolve(image)).convert('RGB'); assert png.size==(320,240)
    nonwhite=int(np.any(np.asarray(png)!=255,axis=2).sum())
    assert nonwhite>1000
    # Real GDAL creates a synthetic projected raster; import preserves pixels and geotransform.
    runtime=runtime_path(); helper=root/'tests/qgis/make_synthetic_raster.py'
    command=f'"{os.environ.get("COMSPEC","cmd.exe")}" /d /s /c ""{runtime}" -u "{helper}" "{prefix}/source.tif""'
    env=os.environ.copy(); env.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',VKM_PUBLIC_ROOT=str(root),VKM_WORK=str(paths.roots['work']))
    proc=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=60,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    assert proc.returncode==0,(proc.stdout,proc.stderr)
    raster=prefix+'/copy.tif'; imported=call('qgis_import_raster',source_path=prefix+'/source.tif',output_path=raster)
    assert imported['info']['has_projection'] and imported['info']['geotransform']==[12,.5,0,20,0,-.5]
    assert np.array_equal(np.asarray(Image.open(paths.resolve(raster))),np.arange(768,dtype=np.uint16).reshape(24,32))
    proj=prefix+'/project.qgz'; call('qgis_project',path=proj,action='create',layers=[{'path':metric,'layer':'transformed'},{'path':raster,'name':'synthetic_raster'}])
    assert call('qgis_project',path=proj,action='open')['layer_count']==2
    summary={'schema':'vkm.qgis_synthetic_acceptance/1','status':'PASS','prefix':prefix,'runtime':status,
             'calls':calls,'render_nonwhite_pixels':nonwhite,
             'known_similarity_rms':fit['rms'],'known_affine_rms':afit['rms'],
             'outputs':{ref:digest(paths.resolve(ref)) for ref in [gpkg,geojson,transform,metric,image,raster,proj]}}
    paths.new_output(prefix+'/acceptance.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def test_invalid_source_archive_guards():
    root=Path(__file__).resolve().parents[2]
    paths=Paths(root,root/'work'); service=QgisService(paths)
    prefix='work:qgis_2026-09-30/invalid_synthetic/'+uuid.uuid4().hex
    gpkg=prefix+'/archive.gpkg'; calls=[]
    def call(tool,**args):
        out=service.call(tool,args); calls.append(out); return out
    assert call('qgis_create_geopackage',path=gpkg)['ok']
    geometry={'type':'Polygon','coordinates':[[[0,0],[10,10],[0,10],[10,0],[0,0]]]}
    doc={'type':'FeatureCollection','features':[{'type':'Feature','geometry':geometry,
        'properties':{'stable_id':'BOWTIE','verification':'AUTO_EXTRACTED_UNREVIEWED'}}]}
    source=prefix+'/invalid.geojson'; paths.new_output(source).write_text(json.dumps(doc),encoding='utf-8')
    args={'source_path':source,'path':gpkg,'name':'invalid_candidates','frame_id':'SYNTHETIC_PIXEL','unit':'px'}
    before=digest(paths.resolve(gpkg))
    for extra,code in [({},'GEOMETRY_INVALID'),({'preserve_invalid_source':True,'crs_kind':'LOCAL_ENGINEERING','unit':'m'},'SOURCE_ARCHIVE_POLICY')]:
        result=call('qgis_import_vector',**(args|extra))
        assert not result['ok'] and result['error']['code']==code,result
        assert digest(paths.resolve(gpkg))==before
    doc['features'][0]['properties']['verification']='VERIFIED_BY_EYE'
    reviewed=prefix+'/reviewed.geojson'; paths.new_output(reviewed).write_text(json.dumps(doc),encoding='utf-8')
    result=call('qgis_import_vector',**(args|{'source_path':reviewed,'preserve_invalid_source':True}))
    assert not result['ok'] and result['error']['code']=='SOURCE_ARCHIVE_POLICY'
    assert digest(paths.resolve(gpkg))==before
    imported=call('qgis_import_vector',**(args|{'preserve_invalid_source':True}))
    assert imported['ok'] and imported['result']['topology_status']=='FAIL' and imported['result']['invalid_geometry_count']==1,imported
    assert imported['result']['invalid_geometry_examples'][0]['reason']
    before=digest(paths.resolve(gpkg))
    promoted=call('qgis_feature',path=gpkg,layer='invalid_candidates',action='update',stable_id='BOWTIE',properties={'verification':'VERIFIED_BY_EYE'})
    assert not promoted['ok'] and promoted['error']['code']=='SOURCE_ARCHIVE_POLICY'
    assert digest(paths.resolve(gpkg))==before
    exported=prefix+'/export.geojson'
    assert call('qgis_export_layer',path=gpkg,layer='invalid_candidates',output_path=exported)['ok']
    reread=json.loads(paths.resolve(exported).read_text(encoding='utf-8'))['features'][0]
    assert reread['geometry']==geometry
    assert json.loads(reread['properties']['properties_json'])['qgis_geometry_validity']['status']=='FAIL'
    from vkm_qgis.fitting import fit_transform
    transform=prefix+'/identity.json'
    paths.new_output(transform).write_text(json.dumps(fit_transform([[0,0],[10,0],[0,10]],[[0,0],[10,0],[0,10]])),encoding='utf-8')
    blocked=[('qgis_render',{'path':gpkg,'layers':['invalid_candidates'],'output_path':prefix+'/blocked.png'}),
             ('qgis_apply_transform',{'path':gpkg,'layer':'invalid_candidates','transform_path':transform,'output_path':prefix+'/blocked.gpkg','output_layer':'bad'}),
             ('qgis_overlay',{'left_path':gpkg,'left_layer':'invalid_candidates','right_path':gpkg,'right_layer':'invalid_candidates','operation':'intersection','output_path':prefix+'/blocked_overlay.gpkg','output_layer':'bad'})]
    for tool,params in blocked:
        result=call(tool,**params)
        assert not result['ok'] and result['error']['code']=='INVALID_SOURCE_GEOMETRY',result
        assert not paths.resolve(params['output_path']).exists()
    summary={'schema':'vkm.qgis_invalid_archive_acceptance/1','status':'PASS_GUARDS_RAW_COORDINATES','prefix':prefix,
             'topology_status':'FAIL','invalid_geometry_count':1,'calls':calls,'raw_coordinate_preservation':'PASS',
             'output_sha256':digest(paths.resolve(gpkg))}
    paths.new_output(prefix+'/acceptance.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')


def test_geometry_update_with_null_extra_properties():
    root=Path(__file__).resolve().parents[2]; paths=Paths(root,root/'work'); service=QgisService(paths)
    prefix='work:qgis_2026-09-30/update_regression/'+uuid.uuid4().hex; gpkg=prefix+'/update.gpkg'
    assert service.call('qgis_create_geopackage',{'path':gpkg})['ok']
    assert service.call('qgis_feature',{'path':gpkg,'layer':'shafts','action':'add','stable_id':'EMPTY-EXTRAS',
        'geometry':{'type':'Point','coordinates':[1,2]}})['ok']
    changed=service.call('qgis_feature',{'path':gpkg,'layer':'shafts','action':'update','stable_id':'EMPTY-EXTRAS',
        'geometry':{'type':'Point','coordinates':[3,4]}})
    assert changed['ok'],changed
    assert changed['result']['feature']['geometry']['coordinates']==[3,4]
    assert json.loads(changed['result']['feature']['properties']['properties_json'])['qgis_geometry_validity']['status']=='PASS'
