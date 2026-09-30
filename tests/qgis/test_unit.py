"""All fixtures are synthetic; no corpus source or closed geometry is embedded."""
import json
from pathlib import Path
import numpy as np
import pytest
from vkm_qgis.errors import ToolFailure
from vkm_qgis.fitting import apply_points, fit_transform
from vkm_qgis.policy import Paths, layer_name
from vkm_qgis.service import QgisService

POINTS=[[0,0],[10,0],[0,10],[10,10],[3,7]]

@pytest.mark.parametrize('model,matrix',[
    ('similarity',[[1.5,-.5,123],[.5,1.5,-37],[0,0,1]]),
    ('affine',[[2,.3,123],[-.2,1.7,-37],[0,0,1]])])
def test_known_transform_and_loo(model,matrix):
    out=fit_transform(POINTS,apply_points(POINTS,matrix),model)
    np.testing.assert_allclose(out['matrix'],matrix,atol=1e-11)
    assert out['rms']<1e-11 and out['max_residual']<1e-11
    assert out['leave_one_out']['status']=='PASS' and out['leave_one_out']['rms']<1e-10
    assert out['epsg'] is None and out['epistemic_status']=='DERIVATION' and out['crs_status']=='UNKNOWN_CRS'

def test_signed_residuals_and_holdout_are_different():
    target=np.asarray(POINTS,dtype=float); target[-1]+=[.2,-.4]
    out=fit_transform(POINTS,target.tolist(),'affine')
    predicted=np.asarray(apply_points(POINTS,out['matrix']))
    np.testing.assert_allclose([r['residual'] for r in out['residuals']],predicted-target)
    assert out['rms']>0 and out['leave_one_out']['rms']>out['rms']

@pytest.mark.parametrize('source,target,model,code',[
    ([[1,1],[1,1]],[[0,0],[1,1]],'similarity','DEGENERATE_GCP'),
    ([[0,0],[1,1],[2,2]],[[0,0],[1,1],[2,2]],'affine','DEGENERATE_GCP'),
    ([[0,0]],[[0,0]],'similarity','INSUFFICIENT_GCP'),
    ([[0,float('nan')],[1,1]],[[0,0],[1,1]],'similarity','INVALID_GCP')])
def test_bad_gcps(source,target,model,code):
    with pytest.raises(ToolFailure) as error: fit_transform(source,target,model)
    assert error.value.code==code

def test_minimal_gcps_dont_invent_loo():
    out=fit_transform([[0,0],[1,0]],[[3,4],[5,4]])
    assert out['leave_one_out']['status']=='NOT_RUN' and out['leave_one_out']['rms'] is None

@pytest.mark.parametrize('target,model',[
    ([[4,4]]*5,'similarity'),([[4,4]]*5,'affine'),([[x[0],0] for x in POINTS],'affine')])
def test_target_collapse_is_rejected_even_with_low_rms(target,model):
    with pytest.raises(ToolFailure) as error: fit_transform(POINTS,target,model)
    assert error.value.code=='DEGENERATE_TRANSFORM'

def test_metric_crs_requires_confirmed_units():
    assert fit_transform(POINTS,POINTS,target_unit='px',target_unit_verified=True)['crs_status']=='UNKNOWN_CRS'
    assert fit_transform(POINTS,POINTS,target_unit='m')['crs_status']=='UNKNOWN_CRS'
    assert fit_transform(POINTS,POINTS,target_unit='m',target_unit_verified=True)['crs_status']=='LOCAL_ENGINEERING_CRS / AUTHORITY_UNKNOWN'

@pytest.mark.parametrize('ref',[
    '../escape.gpkg','work:../escape.gpkg','work:/escape.gpkg','work:C:/escape.gpkg',
    'work:a\\b.gpkg','https:example.gpkg','work:x.exe','public:x.gpkg'])
def test_path_escapes_and_public_writes(tmp_path,ref):
    paths=Paths(tmp_path,tmp_path/'work')
    with pytest.raises(ToolFailure): paths.resolve(ref,write=True)

def test_symlink_escape_if_supported(tmp_path):
    root=tmp_path/'work'; root.mkdir(); other=tmp_path/'outside'; other.mkdir()
    try: (root/'link').symlink_to(other,target_is_directory=True)
    except OSError: pytest.skip('Symlink privilege unavailable')
    with pytest.raises(ToolFailure): Paths(tmp_path,root).resolve('work:link/x.gpkg',write=True)

def test_output_does_not_overwrite(tmp_path):
    paths=Paths(tmp_path,tmp_path/'work'); output=paths.new_output('a.json'); output.write_text('original')
    with pytest.raises(ToolFailure): paths.new_output('a.json')
    assert output.read_text()=='original'

@pytest.mark.parametrize('name',['gpkg_contents','sqlite_schema','_private','bad|name','a/b'])
def test_identifier_policy(name):
    with pytest.raises(ToolFailure): layer_name(name)

def test_unavailable_runtime_and_json_errors(tmp_path,monkeypatch):
    monkeypatch.setenv('VKM_QGIS_PYTHON',str(tmp_path/'missing.exe'))
    service=QgisService(Paths(tmp_path,tmp_path/'work'))
    status=service.call('qgis_status')
    assert status['ok'] and not status['result']['available']
    assert service.call('qgis_list_layers',{'path':'a.gpkg'})['error']['code']=='QGIS_RUNTIME_UNAVAILABLE'
    assert service.call('qgis_create_geopackage',{'path':'public:bad.gpkg'})['error']['code']=='PATH_POLICY'
    bad=service.call('qgis_register_control_points',{'points':[{'source':[float('nan'),0]}]})
    assert not bad['ok'] and bad['error']['code']=='INVALID_ARGUMENT'
    json.dumps(bad,allow_nan=False)

def test_public_tree_does_not_receive_receipt(tmp_path,monkeypatch):
    monkeypatch.setenv('VKM_QGIS_PYTHON',str(tmp_path/'missing.exe'))
    service=QgisService(Paths(tmp_path,tmp_path/'work'))
    status=service.call('qgis_status')
    ref=status['receipt']; receipt=json.loads(service.paths.resolve(ref,exists=True).read_text())
    assert receipt['schema']=='vkm.qgis_operation_receipt/1' and receipt['outputs']=={}
    assert list(tmp_path.iterdir())==[tmp_path/'work']
