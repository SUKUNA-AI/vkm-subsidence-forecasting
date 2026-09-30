"""The same synthetic input yields identical PNG bytes in independent workers."""
import os,uuid
from pathlib import Path
import pytest
from vkm_qgis.policy import Paths
from vkm_qgis.service import QgisService,digest

@pytest.mark.skipif(not(os.environ.get('VKM_QGIS_ROOT') or os.environ.get('VKM_QGIS_PYTHON')),reason='Existing QGIS runtime not configured')
def test_repeat_render_png_hash():
    root=Path(__file__).resolve().parents[2]; paths=Paths(root,root/'work'); service=QgisService(paths)
    prefix='work:qgis_2026-09-30/render_determinism/'+uuid.uuid4().hex; gpkg=prefix+'/geometry.gpkg'
    assert service.call('qgis_create_geopackage',{'path':gpkg})['ok']
    assert service.call('qgis_feature',{'path':gpkg,'layer':'panels','action':'add','stable_id':'SYNTHETIC',
        'geometry':{'type':'MultiPolygon','coordinates':[[[[0,0],[10,0],[10,10],[0,10],[0,0]]]]}})['ok']
    for index in [1,2]:
        result=service.call('qgis_render',{'path':gpkg,'layers':['panels'],'output_path':prefix+f'/render{index}.png','width':320,'height':240})
        assert result['ok'],result
    assert digest(paths.resolve(prefix+'/render1.png'))==digest(paths.resolve(prefix+'/render2.png'))
