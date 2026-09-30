"""Narrow stdio MCP bridge; the host needs MCP, the isolated worker needs existing PyQGIS."""
from __future__ import annotations
import json
from typing import Annotated, Literal
import anyio
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from vkm_qgis import __version__
from vkm_qgis.service import QgisService

READ=ToolAnnotations(read_only_hint=True,destructive_hint=False,idempotent_hint=True,open_world_hint=False)
WRITE=ToolAnnotations(read_only_hint=False,destructive_hint=False,idempotent_hint=False,open_world_hint=False)
Ref=Annotated[str,Field(min_length=1,max_length=600,description='Logical relative path: work:... / private:... / public:... (read only)')]
Name=Annotated[str,Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,62}$')]
Frame=Annotated[str,Field(min_length=1,max_length=200)]
Crstype=Literal['SOURCE_COORDINATES','LOCAL_ENGINEERING']
server=MCPServer('vkm-qgis',title='VKM local QGIS bridge',version=__version__,instructions=(
    'Local isolated headless QGIS operations only. Writes remain WORK/PRIVATE; PUBLIC is read only. '
    'Source coordinates retain their frame and unit; no EPSG or CRS is inferred. '
    'Similarity and affine fits report RMS, maximum residual and leave-one-out. '
    'GIS outputs are DERIVATION/AUTO_EXTRACTED_UNREVIEWED, never evidence or observed data. '
    'No arbitrary code, providers, surface interpolation, solver, GPU, package installation or service administration. '
    'Use qgis_status first. A missing existing runtime returns UNAVAILABLE. Raster import copies a companion GeoTIFF.'
))

async def call(tool,args):
    payload=await anyio.to_thread.run_sync(lambda: QgisService().call(tool,args))
    return CallToolResult(content=[TextContent(type='text',text=json.dumps(payload,ensure_ascii=False,sort_keys=True))],
                          structured_content=payload,is_error=not payload['ok'])

@server.tool(annotations=READ)
async def qgis_status()->CallToolResult:
    """Probe the existing isolated QGIS/GDAL runtime; never install or start a GUI."""
    return await call('qgis_status',{})

@server.tool(annotations=WRITE)
async def qgis_create_geopackage(path:Ref,minimal_layers:bool=True,frame_id:Frame='SKRU1_LOCAL_ENGINEERING')->CallToolResult:
    """Create a NEW GeoPackage with the 15 minimal provenance-bearing vector/aspatial layers."""
    return await call('qgis_create_geopackage',locals())

@server.tool(annotations=READ)
async def qgis_list_layers(path:Ref)->CallToolResult:
    """List GeoPackage layers, fields, feature counts and explicit coordinate frames."""
    return await call('qgis_list_layers',locals())

@server.tool(annotations=WRITE)
async def qgis_create_layer(path:Ref,name:Name,geometry_type:Literal['None','Point','MultiPoint','LineString','MultiLineString','Polygon','MultiPolygon'],
                            crs_kind:Crstype='SOURCE_COORDINATES',frame_id:Frame|None=None,unit:str='unknown',fields:dict[str,str]|None=None)->CallToolResult:
    """Add a NEW layer with an explicit source or local metric frame; no overwrite or EPSG inference."""
    return await call('qgis_create_layer',locals())

@server.tool(annotations=WRITE)
async def qgis_feature(path:Ref,layer:Name,action:Literal['get','list','add','update']='get',stable_id:str='',
                       geometry:dict|None=None,properties:dict|None=None,expected_sha256:str|None=None,
                       limit:Annotated[int,Field(ge=1,le=1000)]=100,offset:Annotated[int,Field(ge=0)]=0)->CallToolResult:
    """Read/list or transactionally add/update one stable feature. FACT promotion and duplicate IDs are rejected."""
    args=locals().copy()
    if geometry is None: args.pop('geometry')
    return await call('qgis_feature',args)

@server.tool(annotations=WRITE)
async def qgis_import_vector(source_path:Ref,path:Ref,name:Name,crs_kind:Crstype='SOURCE_COORDINATES',
                             frame_id:Frame|None=None,unit:str='unknown',geometry_type:str|None=None,append:bool=False,
                             preserve_invalid_source:bool=False)->CallToolResult:
    """Import FeatureCollection; explicit invalid source archive requires SOURCE_COORDINATES + AUTO/ASTRA, preserves raw coords and records topology FAIL."""
    return await call('qgis_import_vector',locals())

@server.tool(annotations=WRITE)
async def qgis_register_control_points(path:Ref,source_frame_id:Frame,target_frame_id:Frame,
                                       points:Annotated[list[dict],Field(min_length=2,max_length=2000)],
                                       source_unit:str='unknown',target_unit:str='unknown',target_unit_verified:bool=False)->CallToolResult:
    """Register paired GCPs with provenance/units; target_unit must match layer. Unverified units keep fit CRS UNKNOWN."""
    return await call('qgis_register_control_points',locals())

@server.tool(annotations=WRITE)
async def qgis_fit_transform(path:Ref,output_path:Ref,model:Literal['similarity','affine']='similarity',
                             control_point_ids:list[str]|None=None)->CallToolResult:
    """Fit planar similarity/affine from registered GCPs; rank checks, signed residuals, RMS, max and LOO are saved."""
    return await call('qgis_fit_transform',locals())

@server.tool(annotations=READ)
async def qgis_residuals(transform_path:Ref)->CallToolResult:
    """Read a saved transform's residuals and leave-one-out diagnostics with its SHA-256."""
    return await call('qgis_residuals',locals())

@server.tool(annotations=WRITE)
async def qgis_apply_transform(path:Ref,layer:Name,transform_path:Ref,output_path:Ref,output_layer:Name)->CallToolResult:
    """Transform a layer into a NEW GeoPackage, preserving parent IDs and transform hash as DERIVATION."""
    return await call('qgis_apply_transform',locals())

@server.tool(annotations=WRITE)
async def qgis_overlay(left_path:Ref,left_layer:Name,right_path:Ref,right_layer:Name,output_path:Ref,output_layer:Name,
                       operation:Literal['intersection','difference','union']='intersection')->CallToolResult:
    """Bounded planar overlay in one identical explicit frame; output is a new GeoPackage and derived geometry."""
    return await call('qgis_overlay',locals())

@server.tool(annotations=WRITE)
async def qgis_export_layer(path:Ref,layer:Name,output_path:Ref)->CallToolResult:
    """Export source/local-coordinate FeatureCollection with explicit VKM frame metadata; no WGS84 conversion."""
    return await call('qgis_export_layer',locals())

@server.tool(annotations=WRITE)
async def qgis_import_raster(source_path:Ref,output_path:Ref)->CallToolResult:
    """Copy an existing local raster to a NEW companion GeoTIFF with GDAL; preserve supplied georeferencing."""
    return await call('qgis_import_raster',locals())

@server.tool(annotations=WRITE)
async def qgis_project(path:Ref,action:Literal['create','open']='open',layers:list[dict]|None=None)->CallToolResult:
    """Create a NEW relative-path QGIS project or safely inspect one with only rooted local OGR/GDAL inputs."""
    args=locals().copy(); args['layers']=layers or []
    return await call('qgis_project',args)

@server.tool(annotations=WRITE)
async def qgis_render(path:Ref,layers:Annotated[list[Name],Field(min_length=1,max_length=30)],output_path:Ref,
                      width:Annotated[int,Field(ge=32,le=4096)]=1024,height:Annotated[int,Field(ge=32,le=4096)]=768)->CallToolResult:
    """Render rooted GeoPackage layers in one explicit frame into a NEW PNG using QGIS CPU rendering."""
    return await call('qgis_render',locals())

def main():
    server.run('stdio')

if __name__=='__main__': main()
