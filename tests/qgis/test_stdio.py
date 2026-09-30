"""Actual MCP initialize/list_tools/call_tool; run with an existing MCP host Python."""
import json
import os
import sys
import uuid
from pathlib import Path
import anyio
try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters,stdio_client
except ImportError:
    ClientSession=None

async def smoke():
    root=Path(__file__).resolve().parents[2]
    prefix='work:qgis_2026-09-30/stdio/'+uuid.uuid4().hex
    env=dict(os.environ, PYTHONPATH=str(root/'src'), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
             VKM_PUBLIC_ROOT=str(root),VKM_WORK=str(root/'work'))
    params=StdioServerParameters(command=sys.executable,args=['-m','vkm_qgis.mcp_server'],env=env,cwd=root)
    results=[]
    async with stdio_client(params) as streams:
        async with ClientSession(*streams,read_timeout_seconds=120) as session:
            initialize=await session.initialize()
            listing=await session.list_tools()
            names=[t.name for t in listing.tools]
            assert len(names)==15 and 'qgis_fit_transform' in names
            assert 'preserve_invalid_source' in next(t for t in listing.tools if t.name=='qgis_import_vector').input_schema['properties']
            async def call(name,args=None):
                result=await session.call_tool(name,args or {})
                payload=result.structured_content
                assert isinstance(payload,dict),result
                assert payload['ok'],payload
                results.append(payload)
                return payload['result']
            status=await call('qgis_status'); assert status['available']
            path=prefix+'/stdio.gpkg'
            await call('qgis_create_geopackage',{'path':path})
            listed=await call('qgis_list_layers',{'path':path}); assert len(listed['layers'])==15
            await call('qgis_feature',{'path':path,'layer':'shafts','action':'add','stable_id':'SYNTHETIC-SHAFT',
                'geometry':{'type':'Point','coordinates':[10,20]},'properties':{'source_id':'SYNTHETIC'}})
            shaft=await call('qgis_feature',{'path':path,'layer':'shafts','stable_id':'SYNTHETIC-SHAFT'})
            assert shaft['feature']['geometry']['coordinates']==[10,20]
            await call('qgis_register_control_points',{'path':path,'source_frame_id':'SYNTHETIC_PIXELS',
                'target_frame_id':'SKRU1_LOCAL_ENGINEERING','source_unit':'px','target_unit':'m','target_unit_verified':True,
                'points':[{'id':'MCP-G0','source':[0,0],'target':[10,20]},
                          {'id':'MCP-G1','source':[1,0],'target':[12,21]},
                          {'id':'MCP-G2','source':[0,1],'target':[9,22]}]})
            fit=await call('qgis_fit_transform',{'path':path,'output_path':prefix+'/transform.json','model':'similarity'})
            assert fit['rms']<1e-10 and fit['crs_status']=='LOCAL_ENGINEERING_CRS / AUTHORITY_UNKNOWN'
            assert fit['leave_one_out']['status']=='PASS'
            await call('qgis_residuals',{'transform_path':prefix+'/transform.json'})
            source=prefix+'/invalid.geojson'
            source_path=root/'work'/source.removeprefix('work:'); source_path.parent.mkdir(parents=True,exist_ok=True)
            source_path.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature',
                'properties':{'stable_id':'MCP-BOWTIE'},'geometry':{'type':'Polygon','coordinates':[[[0,0],[10,10],[0,10],[10,0],[0,0]]]}}]}),encoding='utf-8')
            invalid=await call('qgis_import_vector',{'source_path':source,'path':path,'name':'invalid_source',
                'frame_id':'SYNTHETIC_PIXEL','unit':'px','preserve_invalid_source':True})
            assert invalid['invalid_geometry_count']==1 and invalid['topology_status']=='FAIL'
            blocked=await session.call_tool('qgis_render',{'path':path,'layers':['invalid_source'],'output_path':prefix+'/blocked.png'})
            assert blocked.is_error and blocked.structured_content['error']['code']=='INVALID_SOURCE_GEOMETRY'
            results.append(blocked.structured_content)
            bad=await session.call_tool('qgis_create_geopackage',{'path':'public:forbidden.gpkg'})
            assert bad.is_error and bad.structured_content['error']['code']=='PATH_POLICY'
            results.append(bad.structured_content)
    # Missing runtime must preserve protocol/tools availability and report unavailable honestly.
    missing=StdioServerParameters(command=sys.executable,args=['-m','vkm_qgis.mcp_server'],
                env={**env,'VKM_QGIS_PYTHON':str(root/'work/qgis_2026-09-30/no_runtime.exe')},cwd=root)
    async with stdio_client(missing) as streams:
        async with ClientSession(*streams,read_timeout_seconds=30) as session:
            await session.initialize(); assert len((await session.list_tools()).tools)==15
            status=(await session.call_tool('qgis_status')).structured_content
            assert status['ok'] and status['result']['status']=='UNAVAILABLE'
            failed=(await session.call_tool('qgis_list_layers',{'path':'work:unavailable.gpkg'})).structured_content
            assert not failed['ok'] and failed['error']['code']=='QGIS_RUNTIME_UNAVAILABLE'
            results.extend([status,failed])
    out={'schema':'vkm.qgis_stdio_acceptance/1','status':'PASS','server_info':initialize.server_info.model_dump(),
         'protocol_version':initialize.protocol_version,'tools':names,'calls':results}
    directory=root/'work'/prefix.removeprefix('work:'); directory.mkdir(parents=True,exist_ok=True)
    (directory/'acceptance.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'PASS','tools':len(names),'calls':len(results),'artifact':prefix+'/acceptance.json'}))

def test_stdio_protocol():
    import pytest
    if ClientSession is None: pytest.skip('Run stdio proof with an existing MCP host Python')
    if not (os.environ.get('VKM_QGIS_ROOT') or os.environ.get('VKM_QGIS_PYTHON')): pytest.skip('Existing QGIS runtime not configured')
    anyio.run(smoke)

if __name__=='__main__':
    if ClientSession is None: raise RuntimeError('An existing MCP host Python is required')
    anyio.run(smoke)
