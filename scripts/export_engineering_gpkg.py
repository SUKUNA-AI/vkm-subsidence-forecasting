"""Package local Cartesian engineering coordinates without implying WGS84.

Run in an existing GDAL/QGIS Python runtime. No coordinate transformation is
performed; JSON geometries are treated as local coordinate arrays, not RFC7946
longitude/latitude. Actual source data and exports stay in ignored work/.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from osgeo import gdal, ogr, osr

ROOT=Path(__file__).resolve().parents[1]
LOCAL_WKT='LOCAL_CS["SH1 local engineering coordinates - global tie UNKNOWN",LOCAL_DATUM["Unknown geodetic tie",32767],UNIT["metre",1],AXIS["X East",EAST],AXIS["Y North",NORTH]]'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def run(args):
    out=args.output.resolve()
    if out.exists() or not out.is_relative_to((ROOT/'work').resolve()):
        raise ValueError('Use a new output directory under ignored work/')
    inputs={'boreholes':args.boreholes,'coverage':args.coverage,'absence':args.absence,'builder':Path(__file__)}
    pins={k:sha(p) for k,p in inputs.items()}
    sources={k:json.loads(p.read_text(encoding='utf-8-sig')) for k,p in inputs.items() if k!='builder'}
    out.mkdir(parents=True)
    gdal.UseExceptions();ogr.UseExceptions();osr.UseExceptions()
    gdal.SetConfigOption('OGR_CURRENT_DATE',args.epoch)
    srs=osr.SpatialReference();srs.ImportFromWkt(LOCAL_WKT)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    path=out/'engineering_local_sh1.gpkg'
    ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(path),options=['VERSION=1.4','METADATA_TABLES=YES'])
    common={'HORIZONTAL_FRAME':'SH1_LOCAL_ENGINEERING','LENGTH_UNIT':'m','GLOBAL_GEODETIC_TIE':'UNKNOWN',
            'VERTICAL_FRAME':'DSM_EGM2008_ANCHORED_DEPTH_MODEL','HEIGHT_ROLE':'ENGINEERING_ASSUMPTION_DSM_COLLAR',
            'SURFACE_ACQUISITION':'Copernicus GLO-30 DSM; source acquisition 2011-2015',
            'TRANSFORMATION_APPLIED':'NONE; input X/Y/Z values retained',
            'NO_EPSG_CLAIM':'Custom LOCAL_CS only; no EPSG identifier is assigned',
            'SOURCE_JSON_WARNING':'Local coordinate illustrations, not RFC7946 geographic GeoJSON',
            'FORMAT_REFERENCE':'https://gdal.org/en/stable/drivers/vector/gpkg.html'}
    ds.SetMetadata(common)
    records=[]
    for name,source in sources.items():
        features=source['features']
        kind=ogr.wkbPoint25D if name=='boreholes' else ogr.wkbPolygon
        layer=ds.CreateLayer(name,srs=srs,geom_type=kind,options=['SPATIAL_INDEX=YES','GEOMETRY_NULLABLE=NO',
             'DESCRIPTION=SH1 engineering coordinates in metres; global tie UNKNOWN; scenario heights'])
        metadata={**common,'SOURCE_SHA256':pins[name],'SOURCE_PATH':inputs[name].resolve().relative_to(ROOT).as_posix()}
        if name!='boreholes':metadata['HEIGHT_ROLE']='NO_GEOMETRY_Z; 2D engineering support/absence footprint'
        keys=sorted({key for feature in features for key in feature.get('properties',{})})
        field_kinds={}
        for key in keys:
            values=[f.get('properties',{}).get(key) for f in features]
            present=[v for v in values if v is not None]
            kind=(ogr.OFTInteger64 if present and all(isinstance(v,(int,bool)) for v in present) else
                  ogr.OFTReal if present and all(isinstance(v,(int,float)) for v in present) else ogr.OFTString)
            field_kinds[key]=kind
            layer.CreateField(ogr.FieldDefn(key,kind))
        metadata['JSON_SERIALIZED_FIELDS']=json.dumps([key for key in keys if any(isinstance(f.get('properties',{}).get(key),(dict,list)) for f in features)])
        layer.SetMetadata(metadata)
        for index,item in enumerate(features,1):
            feature=ogr.Feature(layer.GetLayerDefn());feature.SetFID(index)
            geometry=ogr.CreateGeometryFromJson(json.dumps(item['geometry']))
            if geometry is None or geometry.IsEmpty():raise ValueError('Missing geometry')
            feature.SetGeometry(geometry)
            for key,value in item.get('properties',{}).items():
                if value is None:continue
                if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False,separators=(',',':'))
                if isinstance(value,bool):value=int(value)
                feature.SetField(key,value)
            layer.CreateFeature(feature)
        records.append({'layer':name,'features':len(features),'geometry_type':ogr.GeometryTypeToName(layer.GetGeomType()),'metadata':metadata})
    ds=None
    # The metadata extension also has timestamps; fix them explicitly for a
    # deterministic package, independently of the workstation's current clock.
    with sqlite3.connect(path) as db:
        db.execute('UPDATE gpkg_metadata_reference SET timestamp=?',(args.epoch,))
        srs_rows=db.execute('SELECT srs_id,organization,organization_coordsys_id,definition FROM gpkg_spatial_ref_sys WHERE srs_id IN (SELECT srs_id FROM gpkg_contents)').fetchall()
        if any(row[1].upper()=='EPSG' for row in srs_rows):raise ValueError('Unexpected EPSG assignment')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('GeoPackage integrity failure')
    (out/'SH1_LOCAL.wkt').write_text(LOCAL_WKT+'\n',encoding='utf-8')
    save(out/'layer_manifest.json',{'layers':records,'srs_rows':srs_rows,'coordinate_transform':'NONE','field_validation':False})
    (out/'README_RU.md').write_text('# GIS-пакет локальной системы SH1\n\n'
        'Откройте engineering_local_sh1.gpkg в QGIS. Три слоя: boreholes, coverage и absence. '
        'Координаты X/Y выражены в метрах в локальной инженерной системе SH1; связь с глобальной геодезической системой UNKNOWN. '
        'Сохранён пользовательский LOCAL_CS без EPSG. Преобразования и новая привязка не выполнялись.\n\n'
        'Z скважин — инженерная отметка по DSM GLO-30/EGM2008, а не измеренное устье. '
        'Полигоны coverage и absence двумерные. Зоны отсутствия ПКС являются сценарными интерпретациями зарегистрированного профиля. '
        'Исходные .geojson содержат локальные координаты и используются как JSON-иллюстрации; для GIS используйте этот GeoPackage.\n\n'
        'Формат и метаданные: https://gdal.org/en/stable/drivers/vector/gpkg.html\n',encoding='utf-8')
    if pins!={k:sha(p) for k,p in inputs.items()}:raise RuntimeError('Input changed during GIS export')
    save(out/'receipt.json',{'schema':'vkm.engineering_local_gpkg/1','command':sys.argv,
         'inputs':{k:{'path':p.resolve().relative_to(ROOT).as_posix(),'sha256':pins[k]} for k,p in inputs.items()},
         'runtime':{'gdal':gdal.VersionInfo(),'sqlite':sqlite3.sqlite_version},'layers':records,'coordinate_transform':'NONE',
         'outputs_sha256':{p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()}})
    print(json.dumps({'status':'EXPORTED_PENDING_INDEPENDENT_READBACK','layers':[(r['layer'],r['features']) for r in records]},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for item in ('boreholes','coverage','absence','output'):parser.add_argument('--'+item,type=Path,required=True)
    parser.add_argument('--epoch',required=True,help='Fixed RFC3339 GeoPackage creation timestamp; not the observation date')
    run(parser.parse_args())
