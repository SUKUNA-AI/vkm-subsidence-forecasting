"""One isolated PyQGIS operation per process, JSON stdin/stdout; never a shell/code tool."""
from __future__ import annotations
import hashlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vkm_qgis import __version__
from vkm_qgis.errors import ToolFailure
from vkm_qgis.policy import Paths, layer_name

MINIMUM_LAYERS = {
    'source_figures':'None','control_points':'Point','control_point_matches':'None','transforms':'None',
    'mine_boundaries':'MultiPolygon','shafts':'Point','boreholes':'Point','profile_lines':'MultiLineString',
    'panels':'MultiPolygon','blocks':'MultiPolygon','mining_zones':'MultiPolygon','anomalies':'MultiPolygon',
    'geology_sections':'MultiLineString','stratigraphy':'None','conflicts':'None',
}
COMMON = {'stable_id':'string','source_id':'string','page_id':'string','figure_id':'string',
          'source_object_id':'string','extraction_method':'string','verification':'string',
          'valid_from':'string','valid_to':'string','uncertainty':'string','provenance':'string',
          'epistemic_status':'string','source_frame_id':'string','properties_json':'string'}
GCP_FIELDS = {'src_x':'double','src_y':'double','dst_x':'double','dst_y':'double','source_figure_id':'string',
              'target_frame_id':'string','role':'string','source_unit':'string','target_unit':'string','target_unit_verified':'boolean'}
LOCAL_WKT='LOCAL_CS["SKRU-1 local engineering",LOCAL_DATUM["authority unknown",0],UNIT["metre",1],AXIS["x",EAST],AXIS["y",NORTH]]'
GEOMETRY_TYPES = {'None','Point','MultiPoint','LineString','MultiLineString','Polygon','MultiPolygon','GeometryCollection'}
VERIFICATIONS = {'AUTO_EXTRACTED_UNREVIEWED','VERIFIED_BY_EYE','REVIEWED_DIGITIZATION','ASTRA_REVIEW_REQUIRED','UNREADABLE'}
STATUS_CHOICES = {'DERIVATION','INTERPOLATION','MODEL_CHOICE','ENGINEERING_ASSUMPTION','UNKNOWN'}
DLL_HANDLES=[]

def bootstrap(paths):
    # Python >=3.8 no longer resolves dependent DLLs from PATH for imported extension modules.
    # OSGeo4W's batch still supplies PATH; register those directories explicitly for PyQt/PyQGIS.
    base=os.environ.get('OSGEO4W_ROOT')
    if base and hasattr(os,'add_dll_directory'):
        for rel in ['bin','apps/qt6/bin','apps/qgis/bin','apps/Python312']:
            p=Path(base)/rel
            if p.is_dir():
                DLL_HANDLES.append(os.add_dll_directory(str(p)))
    profile=paths.roots['work']/'qgis_2026-09-30'/'runtime_profile'
    profile.mkdir(parents=True,exist_ok=True)
    os.environ['QT_QPA_PLATFORM']='offscreen'
    os.environ['QGIS_CUSTOM_CONFIG_PATH']=str(profile)
    from qgis.core import QgsApplication
    app=QgsApplication([],False,str(profile))
    app.initQgis()
    return app

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _crs(kind):
    from qgis.core import QgsCoordinateReferenceSystem
    crs=QgsCoordinateReferenceSystem()
    if kind == 'LOCAL_ENGINEERING':
        if not crs.createFromWkt(LOCAL_WKT):
            raise ToolFailure('CRS_UNAVAILABLE','Runtime does not accept a local engineering CRS')
    elif kind != 'SOURCE_COORDINATES':
        raise ToolFailure('INVALID_CRS','Only explicit local engineering or source coordinates are supported')
    return crs

def _meta(kind='LOCAL_ENGINEERING',frame_id='SKRU1_LOCAL_ENGINEERING',unit='m'):
    if kind == 'SOURCE_COORDINATES':
        return {'crs_kind':kind,'crs_status':'SOURCE_COORDINATES / AUTHORITY_UNKNOWN','frame_id':frame_id,
                'unit':unit,'authority':None,'epsg':None}
    if kind != 'LOCAL_ENGINEERING' or unit != 'm':
        raise ToolFailure('INVALID_CRS','Local engineering outputs must explicitly use metres')
    return {'crs_kind':kind,'crs_status':'LOCAL_ENGINEERING_CRS / AUTHORITY_UNKNOWN','frame_id':frame_id,
            'unit':unit,'authority':None,'epsg':None}

def _fields(extra=None):
    from qgis.core import QgsFields,QgsField
    from qgis.PyQt.QtCore import QMetaType
    choices={'string':QMetaType.Type.QString,'double':QMetaType.Type.Double,'integer':QMetaType.Type.Int,
             'boolean':QMetaType.Type.Bool}
    fields=QgsFields()
    all_fields={**COMMON,**(extra or {})}
    for name,kind in all_fields.items():
        layer_name(name)
        if kind not in choices:
            raise ToolFailure('INVALID_FIELDS','Field type must be string/double/integer/boolean')
        fields.append(QgsField(name,choices[kind]))
    return fields

def _memory(name,geometry_type,meta,extra=None):
    from qgis.core import QgsVectorLayer
    if geometry_type not in GEOMETRY_TYPES:
        raise ToolFailure('INVALID_GEOMETRY_TYPE','Unsupported geometry type')
    layer=QgsVectorLayer(geometry_type,name,'memory')
    if not layer.isValid():
        raise ToolFailure('LAYER_INVALID','Memory layer could not be created')
    layer.setCrs(_crs(meta['crs_kind']))
    layer.dataProvider().addAttributes(_fields(extra))
    layer.updateFields()
    return layer

def _write_layer(layer,path,name,meta):
    from qgis.core import QgsVectorFileWriter,QgsCoordinateTransformContext
    options=QgsVectorFileWriter.SaveVectorOptions()
    options.driverName='GPKG'
    options.layerName=layer_name(name)
    options.fileEncoding='UTF-8'
    options.actionOnExistingFile=(QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer if path.exists()
                                  else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    out=QgsVectorFileWriter.writeAsVectorFormatV3(layer,str(path),QgsCoordinateTransformContext(),options)
    if int(out[0]) != 0:
        raise ToolFailure('GPKG_WRITE_FAILED','Vector writer rejected the operation',details={'writer_error':str(out[1])[:1000]})
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS _vkm_layers (table_name TEXT PRIMARY KEY, meta_json TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO _vkm_layers VALUES (?,?)',(name,json.dumps(meta,sort_keys=True)))

def _layer(path,name):
    from qgis.core import QgsVectorLayer
    name=layer_name(name)
    layer=QgsVectorLayer(str(path)+'|layername='+name,name,'ogr')
    if not layer.isValid():
        raise ToolFailure('LAYER_NOT_FOUND','GeoPackage vector layer is unavailable',details={'layer':name})
    return layer

def _layer_meta(path,name):
    try:
        with sqlite3.connect(path) as db:
            row=db.execute('SELECT meta_json FROM _vkm_layers WHERE table_name=?',(name,)).fetchone()
        if row:
            return json.loads(row[0])
    except sqlite3.Error:
        pass
    raise ToolFailure('UNKNOWN_FRAME','Layer has no VKM coordinate-frame metadata; import it explicitly')

def _geometry(value,*,preserve_invalid_source=False):
    from qgis.core import QgsGeometry,QgsJsonUtils,QgsFields
    if value is None:
        return QgsGeometry()
    def finite(x):
        if isinstance(x,(list,tuple)):
            if len(x)==2 and all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in x):
                return True
            return bool(x) and all(isinstance(v,(list,tuple)) and finite(v) for v in x)
        return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
    if not isinstance(value,dict) or value.get('type') == 'GeometryCollection' or not finite(value.get('coordinates',[])):
        raise ToolFailure('GEOMETRY_INVALID','Expected a finite GeoJSON geometry (collections are outputs only)')
    features=QgsJsonUtils.stringToFeatureList(json.dumps({'type':'FeatureCollection','features':[
        {'type':'Feature','properties':{},'geometry':value}]}),QgsFields())
    if len(features)!=1:
        raise ToolFailure('GEOMETRY_INVALID','QGIS could not parse the source geometry')
    geometry=features[0].geometry()
    if geometry.isEmpty() or (not preserve_invalid_source and not geometry.isGeosValid()):
        raise ToolFailure('GEOMETRY_INVALID','Empty or invalid geometry is rejected')
    return geometry

def _validity(geometry):
    if geometry.isNull():
        return {'status':'NOT_APPLICABLE','reason':None}
    if geometry.isGeosValid():
        return {'status':'PASS','reason':None}
    errors=geometry.validateGeometry()
    return {'status':'FAIL','reason':'; '.join(error.what() for error in errors)[:1600] or 'GEOS invalid geometry'}

def _require_valid_features(layer):
    for feature in layer.getFeatures():
        if not feature.geometry().isNull() and not feature.geometry().isGeosValid():
            raise ToolFailure('INVALID_SOURCE_GEOMETRY','Downstream operations reject invalid source candidates; no MakeValid is applied',
                details={'stable_id':str(feature['stable_id']),'validity':_validity(feature.geometry())})

def _same_frame(left,right):
    # Validation statistics describe an import, not a coordinate frame.
    keys=['crs_kind','crs_status','frame_id','unit','authority','epsg']
    return all(left.get(key)==right.get(key) for key in keys)

def _props(properties):
    properties=dict(properties or {})
    # Source exporters may use the documentary names page/figure/method. Preserve
    # those original fields as extras and also populate the standard provenance columns.
    for field,alias in [('page_id','page'),('figure_id','figure'),('extraction_method','method')]:
        if field not in properties and alias in properties:
            properties[field]=properties[alias]
    if not isinstance(properties.get('stable_id'),str) or not properties['stable_id']:
        raise ToolFailure('INVALID_FEATURE','stable_id is required')
    if properties.get('verification','AUTO_EXTRACTED_UNREVIEWED') not in VERIFICATIONS:
        raise ToolFailure('STATUS_POLICY','Verification cannot silently become evidence')
    if properties.get('epistemic_status','DERIVATION') not in STATUS_CHOICES:
        raise ToolFailure('STATUS_POLICY','GIS geometry cannot be promoted to FACT or observation')
    return {'source_id':'UNKNOWN','page_id':'UNKNOWN','figure_id':'UNKNOWN','source_object_id':'UNKNOWN',
            'valid_from':'UNKNOWN','valid_to':'UNKNOWN','source_frame_id':'UNKNOWN',
            'extraction_method':'VKM_QGIS','verification':'AUTO_EXTRACTED_UNREVIEWED',
            'epistemic_status':'DERIVATION','uncertainty':'UNKNOWN','provenance':'{}',**properties}

def _add_memory(layer,geometry,properties):
    from qgis.core import QgsFeature
    properties=_props(properties)
    feature=QgsFeature(layer.fields())
    if geometry and not geometry.isNull():
        feature.setGeometry(geometry)
    extras={k:v for k,v in properties.items() if layer.fields().indexFromName(k) < 0}
    if extras:
        original=properties.get('properties_json')
        if original:
            try: base=json.loads(original) if isinstance(original,str) else original
            except (TypeError,ValueError) as error:
                raise ToolFailure('INVALID_FEATURE','properties_json must contain a JSON object') from error
            if not isinstance(base,dict):
                raise ToolFailure('INVALID_FEATURE','properties_json must contain a JSON object')
            extras={**base,**extras}
        properties['properties_json']=json.dumps(extras,ensure_ascii=False,sort_keys=True)
    for k,v in properties.items():
        if layer.fields().indexFromName(k) >= 0:
            feature[k]=json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else v
    if not layer.addFeature(feature):
        raise ToolFailure('FEATURE_WRITE_FAILED','Feature could not be added')
    return feature

def _feature_json(feature):
    from qgis.PyQt.QtCore import QVariant
    attrs={}
    for field in feature.fields():
        v=feature[field.name()]
        attrs[field.name()]=None if isinstance(v,QVariant) and v.isNull() else v
    geometry=feature.geometry()
    return {'type':'Feature','id':int(feature.id()),'properties':attrs,
            'geometry':None if geometry.isNull() else json.loads(geometry.asJson(15))}

def _find_feature(layer,stable_id):
    from qgis.core import QgsFeatureRequest,QgsExpression
    req=QgsFeatureRequest().setFilterExpression(QgsExpression.createFieldEqualityExpression('stable_id',stable_id))
    features=list(layer.getFeatures(req))
    if len(features)!=1:
        raise ToolFailure('FEATURE_NOT_FOUND' if not features else 'DUPLICATE_STABLE_ID','Expected one stable feature')
    return features[0]

def _commit(layer):
    if not layer.commitChanges():
        errors=layer.commitErrors()
        layer.rollBack()
        raise ToolFailure('FEATURE_WRITE_FAILED','GeoPackage transaction failed',details={'errors':errors})

def create_gpkg(paths,args):
    path=paths.new_output(args['path'],suffixes={'.gpkg'})
    requested=MINIMUM_LAYERS if args.get('minimal_layers',True) else {'source_figures':'None'}
    for name,kind in requested.items():
        is_source=name in {'geology_sections','source_figures','stratigraphy'}
        meta=_meta('SOURCE_COORDINATES' if is_source else 'LOCAL_ENGINEERING',None if is_source else args.get('frame_id','SKRU1_LOCAL_ENGINEERING'),'unknown' if is_source else 'm')
        fields=GCP_FIELDS if name in {'control_points','control_point_matches'} else {}
        layer=_memory(name,kind,meta,fields)
        _write_layer(layer,path,name,meta)
    return {'path':args['path'],'layers':list(requested),'outputs':[args['path']], 'epsg':None}

def list_layers(paths,args):
    from qgis.core import QgsWkbTypes,QgsCoordinateReferenceSystem
    path=paths.resolve(args['path'],exists=True,suffixes={'.gpkg'})
    out=[]
    with sqlite3.connect(path) as db:
        rows=db.execute('SELECT table_name,data_type,srs_id FROM gpkg_contents ORDER BY table_name').fetchall()
        authorities={}
        for identifier,definition in db.execute('SELECT srs_id,definition FROM gpkg_spatial_ref_sys'):
            crs=QgsCoordinateReferenceSystem()
            authorities[identifier]=(crs.authid() or None) if crs.createFromWkt(definition) else None
        # Reopening an OGR provider for every table repeatedly parses the whole
        # GeoPackage schema. Read container metadata/counts directly; geometry
        # operations still hydrate the addressed layer with the real provider.
        for name,kind,srs in rows:
            name=layer_name(name)
            if kind not in {'features','attributes'}:
                raise ToolFailure('UNSUPPORTED_LAYER','This API lists vector/aspatial GeoPackage layers')
            metadata=db.execute('SELECT meta_json FROM _vkm_layers WHERE table_name=?',(name,)).fetchone()
            if not metadata: raise ToolFailure('UNKNOWN_FRAME','Layer has no VKM coordinate-frame metadata')
            meta=json.loads(metadata[0])
            geometry=db.execute('SELECT column_name,geometry_type_name,z,m FROM gpkg_geometry_columns WHERE table_name=?',(name,)).fetchone()
            if geometry:
                suffix=('Z' if geometry[2]==1 else '')+('M' if geometry[3]==1 else '')
                geometry_type=QgsWkbTypes.parseType(geometry[1]+suffix).name
            else: geometry_type='NoGeometry'
            fields=[row[1] for row in db.execute(f'PRAGMA table_info("{name}")') if not geometry or row[1]!=geometry[0]]
            count=db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out.append({'name':name,'data_type':kind,'feature_count':count,'geometry_type':geometry_type,
                        'fields':fields,'coordinate_reference':meta,'authority_id':authorities.get(srs)})
    return {'path':args['path'],'layers':out}

def create_layer(paths,args):
    path=paths.resolve(args['path'],write=True,exists=True,suffixes={'.gpkg'})
    name=layer_name(args['name'])
    if name in [x['name'] for x in list_layers(paths,{'path':args['path']})['layers']]:
        raise ToolFailure('LAYER_EXISTS','Layer already exists; no overwrite is performed')
    meta=_meta(args.get('crs_kind','SOURCE_COORDINATES'),args.get('frame_id'),args.get('unit','unknown'))
    layer=_memory(name,args['geometry_type'],meta,args.get('fields'))
    _write_layer(layer,path,name,meta)
    return {'path':args['path'],'layer':name,'coordinate_reference':meta,'outputs':[args['path']]}

def feature(paths,args):
    action=args.get('action','get')
    path=paths.resolve(args['path'],write=action not in {'get','list'},exists=True,suffixes={'.gpkg'})
    layer=_layer(path,args['layer'])
    if action=='list':
        from itertools import islice
        limit,offset=args.get('limit',100),args.get('offset',0)
        if not isinstance(limit,int) or not 1<=limit<=1000 or not isinstance(offset,int) or offset<0:
            raise ToolFailure('INVALID_ARGUMENT','List limit is 1..1000; offset is non-negative')
        return {'features':[_feature_json(f) for f in islice(layer.getFeatures(),offset,offset+limit)],
                'count':layer.featureCount(),'coordinate_reference':_layer_meta(path,args['layer'])}
    stable_id=args['stable_id']
    if action=='get':
        return {'feature':_feature_json(_find_feature(layer,stable_id)),'coordinate_reference':_layer_meta(path,args['layer'])}
    if action not in {'add','update'}:
        raise ToolFailure('INVALID_ARGUMENT','Feature action is add/update/get/list')
    if args.get('expected_sha256') and sha(path)!=args['expected_sha256']:
        raise ToolFailure('FILE_CONFLICT','GeoPackage changed since the caller read it')
    props=_props({**(args.get('properties') or {}),'stable_id':stable_id})
    geom=_geometry(args.get('geometry')) if action=='add' or 'geometry' in args else None
    if action=='add':
        from qgis.core import QgsFeatureRequest,QgsExpression
        req=QgsFeatureRequest().setFilterExpression(QgsExpression.createFieldEqualityExpression('stable_id',stable_id))
        if next(layer.getFeatures(req),None) is not None:
            raise ToolFailure('DUPLICATE_STABLE_ID','stable_id already exists')
        if not layer.startEditing():
            raise ToolFailure('FEATURE_WRITE_FAILED','Layer is not editable')
        try:
            out=_add_memory(layer,geom,props)
            _commit(layer)
        except Exception:
            layer.rollBack(); raise
    else:
        out=_find_feature(layer,stable_id)
        candidate=geom if geom is not None else out.geometry()
        verification=(args.get('properties') or {}).get('verification',str(out['verification']))
        if not candidate.isNull() and not candidate.isGeosValid() and (
            _layer_meta(path,args['layer'])['crs_kind']!='SOURCE_COORDINATES' or
            verification not in {'AUTO_EXTRACTED_UNREVIEWED','ASTRA_REVIEW_REQUIRED'}):
            raise ToolFailure('SOURCE_ARCHIVE_POLICY','Invalid source candidates cannot be promoted through a feature update')
        if not layer.startEditing():
            raise ToolFailure('FEATURE_WRITE_FAILED','Layer is not editable')
        try:
            if geom is not None:
                out.setGeometry(geom)
                extras=json.loads(_feature_json(out)['properties'].get('properties_json') or '{}')
                extras['qgis_geometry_validity']=_validity(geom)
                out['properties_json']=json.dumps(extras,sort_keys=True,ensure_ascii=False)
            for k,v in (args.get('properties') or {}).items():
                if k=='stable_id' and v!=stable_id:
                    raise ToolFailure('IMMUTABLE_ID','stable_id cannot change')
                if layer.fields().indexFromName(k)<0:
                    raise ToolFailure('INVALID_FIELDS','Update names an unknown field',details={'field':k})
                out[k]=json.dumps(v,sort_keys=True,ensure_ascii=False) if isinstance(v,(dict,list)) else v
            if not layer.updateFeature(out):
                raise ToolFailure('FEATURE_WRITE_FAILED','Feature update failed')
            _commit(layer)
        except Exception:
            layer.rollBack(); raise
    return {'feature':_feature_json(out),'outputs':[args['path']]}

def import_vector(paths,args):
    source=paths.resolve(args['source_path'],exists=True,suffixes={'.json','.geojson'})
    if source.stat().st_size>32_000_000:
        raise ToolFailure('BUDGET_EXCEEDED','Source GeoJSON exceeds 32 MB')
    doc=json.loads(source.read_text(encoding='utf-8'))
    if doc.get('type')!='FeatureCollection' or not isinstance(doc.get('features'),list) or len(doc['features'])>100000:
        raise ToolFailure('INVALID_VECTOR','Use a bounded source-coordinate GeoJSON FeatureCollection')
    path=paths.resolve(args['path'],write=True,exists=True,suffixes={'.gpkg'})
    name=layer_name(args['name'])
    with sqlite3.connect(path) as db:
        existing=db.execute('SELECT 1 FROM gpkg_contents WHERE table_name=?',(name,)).fetchone() is not None
    if existing and not args.get('append',False):
        raise ToolFailure('LAYER_EXISTS','Import needs a new layer name or explicit append')
    meta=_meta(args.get('crs_kind','SOURCE_COORDINATES'),args.get('frame_id'),args.get('unit','unknown'))
    preserve=args.get('preserve_invalid_source',False)
    if not isinstance(preserve,bool) or (preserve and meta['crs_kind']!='SOURCE_COORDINATES'):
        raise ToolFailure('SOURCE_ARCHIVE_POLICY','Invalid candidates may only be explicitly preserved in SOURCE_COORDINATES')
    geometry_type=args.get('geometry_type') or next((x['geometry']['type'] for x in doc['features'] if x.get('geometry')),'None')
    layer=_memory(name,geometry_type,meta)
    layer.startEditing()
    seen=set()
    invalid=[]
    source_sha=sha(source)
    for index,item in enumerate(doc['features']):
        props=dict(item.get('properties') or {})
        if preserve and props.get('verification','AUTO_EXTRACTED_UNREVIEWED') not in {'AUTO_EXTRACTED_UNREVIEWED','ASTRA_REVIEW_REQUIRED'}:
            raise ToolFailure('SOURCE_ARCHIVE_POLICY','Preserved source candidates must remain AUTO_EXTRACTED_UNREVIEWED or ASTRA_REVIEW_REQUIRED')
        if not props.get('stable_id'):
            props['stable_id']='SRC-'+hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest()[:16]
        if props['stable_id'] in seen:
            raise ToolFailure('DUPLICATE_STABLE_ID','Duplicate source feature ID')
        seen.add(props['stable_id'])
        props['source_frame_id']=meta['frame_id']
        if not props.get('provenance'):
            props['provenance']={'input_ref':args['source_path'],'input_sha256':source_sha,
                'source_object_id':props.get('source_object_id','UNKNOWN'),'source_sha256':props.get('source_sha256','UNKNOWN'),
                'source_locator':props.get('locator'),'upstream_method':props.get('method'),'method_choices':props.get('method_choices')}
        geometry=_geometry(item.get('geometry'),preserve_invalid_source=preserve)
        validity=_validity(geometry)
        props['qgis_geometry_validity']=validity
        if validity['status']=='FAIL': invalid.append({'stable_id':props['stable_id'],'reason':validity['reason']})
        _add_memory(layer,geometry,props)
    _commit(layer)
    if existing:
        target=_layer(path,name)
        if not _same_frame(_layer_meta(path,name),meta) or target.wkbType()!=layer.wkbType():
            raise ToolFailure('FRAME_MISMATCH','Append requires matching geometry type and coordinate frame')
        ids={str(f['stable_id']) for f in target.getFeatures()}
        if seen & ids:
            raise ToolFailure('DUPLICATE_STABLE_ID','An imported ID already exists')
        if not target.startEditing():
            raise ToolFailure('FEATURE_WRITE_FAILED','Target layer is not editable')
        try:
            for item in layer.getFeatures():
                _add_memory(target,item.geometry(),_feature_json(item)['properties'])
            _commit(target)
        except Exception:
            target.rollBack(); raise
    else:
        _write_layer(layer,path,name,meta)
    return {'path':args['path'],'layer':name,'count':layer.featureCount(),'input_sha256':source_sha,
            'coordinate_reference':meta,'ignored_geojson_geographic_default':True,'outputs':[args['path']],
            'preserve_invalid_source':preserve,'topology_status':'FAIL' if invalid else 'PASS',
            'invalid_geometry_count':len(invalid),'invalid_geometry_examples':invalid[:1000],
            'raw_coordinate_policy':'preserved; no MakeValid or interpolation'}

def register_gcps(paths,args):
    if any(not isinstance(args.get(k,'unknown'),str) for k in ['source_unit','target_unit']) or not isinstance(args.get('target_unit_verified',False),bool):
        raise ToolFailure('INVALID_UNITS','Units are explicit strings and verification is a boolean')
    points=args['points']
    if not isinstance(points,list) or not 2<=len(points)<=2000:
        raise ToolFailure('INVALID_GCP','Use 2 to 2000 control-point matches')
    normalized=[]
    seen=set()
    roles={'GCP','SHAFT','LABELLED_BOREHOLE','GRID_INTERSECTION','PROFILE_INTERSECTION',
           'WORKINGS_INTERSECTION','BOUNDARY_VERTEX','ENGINEERING_OBJECT'}
    for item in points:
        identifier=item.get('id')
        if not isinstance(identifier,str) or not identifier or identifier in seen:
            raise ToolFailure('INVALID_GCP','Control-point IDs must be unique')
        seen.add(identifier)
        src,dst=item.get('source'),item.get('target')
        if any(not isinstance(p,list) or len(p)!=2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in p) for p in [src,dst]):
            raise ToolFailure('INVALID_GCP','Control points must have finite 2D source and target pairs')
        if item.get('role','GCP') not in roles:
            raise ToolFailure('GCP_ROLE_POLICY','Use persistent engineering objects; observed subsidence is not a GCP')
        _props({'stable_id':identifier,'verification':item.get('verification','AUTO_EXTRACTED_UNREVIEWED')})
        normalized.append((item,identifier,src,dst))
    path=paths.resolve(args['path'],write=True,exists=True,suffixes={'.gpkg'})
    layer_meta=_layer_meta(path,'control_points')
    if layer_meta['frame_id']!=args['target_frame_id']:
        raise ToolFailure('FRAME_MISMATCH','GCP target frame must match the GeoPackage control_points layer')
    if args.get('target_unit','unknown')!=layer_meta['unit']:
        raise ToolFailure('UNIT_MISMATCH','Declare target_unit matching the control_points layer; unknown is never guessed')
    # SQLite commits the point and its aspatial match in ONE transaction. The only
    # geometry written here is a finite Point, using the standard GeoPackage header
    # and little-endian WKB. QGIS reads it back in the acceptance test.
    import struct
    from contextlib import closing
    with closing(sqlite3.connect(path)) as db:
        def xy(blob,index):
            return struct.unpack_from('<d',blob,13+index*8)[0]
        db.create_function('ST_IsEmpty',1,lambda blob:1 if blob is None else 0)
        for name,index in [('ST_MinX',0),('ST_MaxX',0),('ST_MinY',1),('ST_MaxY',1)]:
            db.create_function(name,1,lambda blob,i=index:xy(blob,i))
        db.execute('BEGIN IMMEDIATE')
        try:
            existing={row[0] for row in db.execute('SELECT stable_id FROM control_points UNION SELECT stable_id FROM control_point_matches')}
            if any(identifier in existing or identifier+'_match' in existing for _,identifier,_,_ in normalized):
                raise ToolFailure('DUPLICATE_STABLE_ID','A GCP ID is already registered')
            row=db.execute("SELECT column_name,srs_id FROM gpkg_geometry_columns WHERE table_name='control_points'").fetchone()
            if not row: raise ToolFailure('LAYER_NOT_FOUND','control_points needs a Point geometry column')
            geometry_column,srs=row
            layer_name(geometry_column)
            for item,identifier,src,dst in normalized:
                props=_props({'stable_id':identifier,'src_x':src[0],'src_y':src[1],'dst_x':dst[0],'dst_y':dst[1],
                    'source_figure_id':item.get('source_figure_id','UNKNOWN'),'source_id':item.get('source_id','UNKNOWN'),
                    'source_frame_id':args['source_frame_id'],'target_frame_id':args['target_frame_id'],
                    'source_unit':args.get('source_unit','unknown'),'target_unit':args.get('target_unit','unknown'),
                    'target_unit_verified':bool(args.get('target_unit_verified',False)),
                    'verification':item.get('verification','AUTO_EXTRACTED_UNREVIEWED'),'role':item.get('role','GCP'),
                    'provenance':item.get('provenance',{})})
                for table,stable_id in [('control_points',identifier),('control_point_matches',identifier+'_match')]:
                    values={k:json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in props.items()}
                    values['stable_id']=stable_id
                    if table=='control_points':
                        values[geometry_column]=struct.pack('<2sBBiBIdd',b'GP',0,1,srs,1,1,*dst)
                    columns=','.join('"'+k+'"' for k in values)
                    db.execute('INSERT INTO '+table+' ('+columns+') VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
            db.execute("UPDATE gpkg_contents SET min_x=(SELECT MIN(dst_x) FROM control_points),min_y=(SELECT MIN(dst_y) FROM control_points),max_x=(SELECT MAX(dst_x) FROM control_points),max_y=(SELECT MAX(dst_y) FROM control_points),last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name='control_points'")
            db.commit()
        except Exception:
            db.rollback(); raise
    return {'ids':[x[1] for x in normalized],'outputs':[args['path']],'target_unit':'m','epsg':None}

def fit(paths,args):
    from vkm_qgis.fitting import fit_transform
    path=paths.resolve(args['path'],exists=True,suffixes={'.gpkg'})
    matches=_layer(path,'control_point_matches')
    selected=args.get('control_point_ids')
    rows=[f for f in matches.getFeatures() if selected is None or str(f['stable_id']).removesuffix('_match') in selected]
    if selected and len(rows)!=len(set(selected)):
        raise ToolFailure('INVALID_GCP','Requested matches are missing or duplicated')
    sources={str(f['source_frame_id']) for f in rows}
    targets={str(f['target_frame_id']) for f in rows}
    if len(sources)!=1 or len(targets)!=1:
        raise ToolFailure('FRAME_MISMATCH','A transform must connect exactly one source frame to one target frame')
    source_units={str(f['source_unit']) for f in rows}; target_units={str(f['target_unit']) for f in rows}
    if len(source_units)!=1 or len(target_units)!=1:
        raise ToolFailure('UNIT_MISMATCH','All selected GCPs must use identical source and target units')
    verified=all(bool(f['target_unit_verified']) for f in rows)
    result=fit_transform([[f['src_x'],f['src_y']] for f in rows],[[f['dst_x'],f['dst_y']] for f in rows],
                         args.get('model','similarity'),[str(f['stable_id']).removesuffix('_match') for f in rows],
                         source_unit=next(iter(source_units)),target_unit=next(iter(target_units)),target_unit_verified=verified)
    result['source_frame_id']=next(iter(sources))
    result['target_frame_id']=next(iter(targets))
    output=paths.new_output(args['output_path'],suffixes={'.json'})
    output.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    return {**result,'outputs':[args['output_path']]}

def residuals(paths,args):
    path=paths.resolve(args['transform_path'],exists=True,suffixes={'.json'})
    result=json.loads(path.read_text(encoding='utf-8'))
    if result.get('schema')!='vkm.qgis_transform/1':
        raise ToolFailure('INVALID_TRANSFORM','Unsupported transform schema')
    return {'transform_id':result['transform_id'],'transform_sha256':sha(path),'rms':result['rms'],
            'max_residual':result['max_residual'],'residuals':result['residuals'],'leave_one_out':result['leave_one_out']}

def apply_transform(paths,args):
    from qgis.PyQt.QtGui import QTransform
    source_path=paths.resolve(args['path'],exists=True,suffixes={'.gpkg'})
    transform_path=paths.resolve(args['transform_path'],exists=True,suffixes={'.json'})
    transform=json.loads(transform_path.read_text(encoding='utf-8'))
    if transform.get('schema')!='vkm.qgis_transform/1':
        raise ToolFailure('INVALID_TRANSFORM','Unsupported transform schema')
    from vkm_qgis.fitting import apply_points
    apply_points([[0,0]],transform['matrix'])
    source=_layer(source_path,args['layer'])
    _require_valid_features(source)
    smeta=_layer_meta(source_path,args['layer'])
    if not smeta['frame_id'] or smeta['frame_id']!=transform['source_frame_id']:
        raise ToolFailure('FRAME_MISMATCH','Source layer does not match the transform source frame')
    output=paths.new_output(args['output_path'],suffixes={'.gpkg'})
    if transform.get('target_unit')=='m' and transform.get('target_unit_verified'):
        meta=_meta('LOCAL_ENGINEERING',transform['target_frame_id'],'m')
    else:
        meta=_meta('SOURCE_COORDINATES',transform['target_frame_id'],transform.get('target_unit','unknown'))
    target=_memory(args['output_layer'],source.wkbType().name,meta)
    m=transform['matrix']; qt=QTransform(m[0][0],m[1][0],m[0][1],m[1][1],m[0][2],m[1][2])
    target.startEditing()
    for item in source.getFeatures():
        geometry=item.geometry()
        if not geometry.isNull():
            geometry.transform(qt)
        props=_feature_json(item)['properties']
        props.pop('fid',None)
        props['stable_id']=str(props['stable_id'])+'__'+transform['transform_id']
        props['source_frame_id']=meta['frame_id']
        props['extraction_method']='PLANAR_TRANSFORM'
        props['epistemic_status']='DERIVATION'
        props['verification']='AUTO_EXTRACTED_UNREVIEWED'
        props['provenance']={'parent_feature':str(item['stable_id']),'transform_id':transform['transform_id'],
                             'transform_sha256':sha(transform_path)}
        _add_memory(target,geometry,props)
    _commit(target); _write_layer(target,output,args['output_layer'],meta)
    return {'path':args['output_path'],'layer':args['output_layer'],'count':target.featureCount(),'coordinate_reference':meta,'outputs':[args['output_path']]}

def overlay(paths,args):
    from qgis.core import QgsGeometry,QgsWkbTypes
    paths_in=[paths.resolve(args[k],exists=True,suffixes={'.gpkg'}) for k in ['left_path','right_path']]
    left,right=[_layer(p,args[k]) for p,k in zip(paths_in,['left_layer','right_layer'])]
    _require_valid_features(left); _require_valid_features(right)
    lm,rm=[_layer_meta(p,args[k]) for p,k in zip(paths_in,['left_layer','right_layer'])]
    if not lm['frame_id'] or not _same_frame(lm,rm):
        raise ToolFailure('FRAME_MISMATCH','Overlay requires identical explicit coordinate-frame metadata')
    lf,rf=list(left.getFeatures()),list(right.getFeatures())
    if len(lf)*len(rf)>100000:
        raise ToolFailure('BUDGET_EXCEEDED','Overlay is limited to 100000 feature pairs')
    operation=args.get('operation','intersection')
    outputs=[]
    if operation=='union':
        geom=QgsGeometry.unaryUnion([f.geometry() for f in lf+rf])
        outputs=[(geom,[str(f['stable_id']) for f in lf+rf])]
    elif operation in {'intersection','difference'}:
        for a in lf:
            if operation=='intersection':
                for b in rf:
                    if a.geometry().boundingBox().intersects(b.geometry().boundingBox()):
                        outputs.append((a.geometry().intersection(b.geometry()),[str(a['stable_id']),str(b['stable_id'])]))
            else:
                geom=a.geometry()
                for b in rf:
                    geom=geom.difference(b.geometry())
                outputs.append((geom,[str(a['stable_id']),*[str(f['stable_id']) for f in rf]]))
    else:
        raise ToolFailure('INVALID_ARGUMENT','Overlay is intersection/difference/union')
    families={int(QgsWkbTypes.geometryType(g.wkbType())) for g,_ in outputs if not g.isEmpty()}
    if len(families)>1:
        raise ToolFailure('UNSUPPORTED_OVERLAY','Mixed-dimensional overlay requires separate outputs; it is not silently flattened')
    family=next(iter(families),int(QgsWkbTypes.geometryType(left.wkbType())))
    geometry_type={0:'MultiPoint',1:'MultiLineString',2:'MultiPolygon'}.get(family)
    if geometry_type is None:
        raise ToolFailure('UNSUPPORTED_OVERLAY','Overlay requires simple point, line or polygon geometries')
    output=paths.new_output(args['output_path'],suffixes={'.gpkg'})
    target=_memory(args['output_layer'],geometry_type,lm); target.startEditing()
    total_area=0.0; n=0
    for geom,parents in outputs:
        if geom.isEmpty():
            continue
        if not geom.isGeosValid():
            raise ToolFailure('GEOMETRY_INVALID','Overlay produced invalid geometry')
        total_area+=geom.area()
        geom.convertToMultiType()
        sid='OVR-'+hashlib.sha256(json.dumps([operation,parents,json.loads(geom.asJson(15))],sort_keys=True).encode()).hexdigest()[:16]
        _add_memory(target,geom,{'stable_id':sid,'source_frame_id':lm['frame_id'],'extraction_method':'QGIS_'+operation.upper(),
                                     'provenance':{'parent_features':parents,'operation':operation}})
        n+=1
    _commit(target); _write_layer(target,output,args['output_layer'],lm)
    return {'path':args['output_path'],'layer':args['output_layer'],'count':n,'area':total_area,
            'area_unit':lm['unit']+'^2','coordinate_reference':lm,'outputs':[args['output_path']]}

def export_vector(paths,args):
    path=paths.resolve(args['path'],exists=True,suffixes={'.gpkg'})
    layer=_layer(path,args['layer'])
    if layer.featureCount()>100000:
        raise ToolFailure('BUDGET_EXCEEDED','Export is limited to 100000 features')
    meta=_layer_meta(path,args['layer'])
    output=paths.new_output(args['output_path'],suffixes={'.json','.geojson'})
    doc={'type':'FeatureCollection','vkm_coordinate_reference':meta,
         'vkm_status':{'epistemic_status':'DERIVATION','verification':'AUTO_EXTRACTED_UNREVIEWED'},
         'features':[_feature_json(f) for f in layer.getFeatures()]}
    output.write_text(json.dumps(doc,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return {'path':args['output_path'],'count':len(doc['features']),'coordinate_reference':meta,
            'format_note':'source-coordinate FeatureCollection; not RFC7946 longitude/latitude','outputs':[args['output_path']]}

def import_raster(paths,args):
    from osgeo import gdal
    source=paths.resolve(args['source_path'],exists=True,suffixes={'.tif','.tiff','.png','.jpg','.jpeg'})
    output=paths.new_output(args['output_path'],suffixes={'.tif','.tiff'})
    gdal.UseExceptions()
    ds=gdal.Translate(str(output),str(source),format='GTiff',creationOptions=['COMPRESS=DEFLATE'])
    if ds is None:
        raise ToolFailure('RASTER_IMPORT_FAILED','GDAL could not copy raster')
    info={'width':ds.RasterXSize,'height':ds.RasterYSize,'bands':ds.RasterCount,
          'has_projection':bool(ds.GetProjection()),'geotransform':ds.GetGeoTransform(can_return_null=True),
          'projection_policy':'copied as supplied; no EPSG assigned or inferred'}
    ds=None
    return {'path':args['output_path'],'input_sha256':sha(source),'info':info,'outputs':[args['output_path']]}

def project(paths,args):
    from qgis.core import QgsProject,QgsVectorLayer,QgsRasterLayer
    action=args.get('action','open')
    if action=='open':
        path=paths.resolve(args['path'],exists=True,suffixes={'.qgs','.qgz'})
        # Reject embedded code, network/external providers and datasource escapes before QGIS reads them.
        import zipfile
        from xml.etree import ElementTree as ET
        if path.suffix.lower()=='.qgz':
            with zipfile.ZipFile(path) as archive:
                members=archive.infolist()
                if len(members)>3 or sum(m.file_size for m in members)>20_000_000 or any(
                    Path(m.filename).name!=m.filename or '/' in m.filename or '\\' in m.filename or ':' in m.filename
                    or not (Path(m.filename).suffix.lower() in {'.qgs','.qgd'} or m.filename.endswith('_styles.db')) for m in members):
                    raise ToolFailure('PROJECT_POLICY','Project archive may contain only bounded QGS/QGD and QGIS style database basenames')
                names=[n for n in archive.namelist() if n.endswith('.qgs')]
                if len(names)!=1 or archive.getinfo(names[0]).file_size>20_000_000:
                    raise ToolFailure('PROJECT_INVALID','Expected one bounded project XML')
                xml=archive.read(names[0])
        else:
            if path.stat().st_size>20_000_000:
                raise ToolFailure('PROJECT_INVALID','Project XML exceeds size budget')
            xml=path.read_bytes()
        doc=ET.fromstring(xml)
        if any((node.text or '').strip() for node in doc.findall('.//pythonCode')):
            raise ToolFailure('PROJECT_POLICY','Embedded Python macros are rejected')
        for node in doc.findall('.//projectlayers/maplayer'):
            if node.findtext('provider') not in {'ogr','gdal'}:
                raise ToolFailure('PROJECT_POLICY','Only local OGR/GDAL layers are supported')
            datasource=node.findtext('datasource') or ''
            src,separator,option=datasource.partition('|')
            if separator:
                if not option.startswith('layername=') or '|' in option:
                    raise ToolFailure('PROJECT_POLICY','Arbitrary provider URI options are rejected')
                layer_name(option.removeprefix('layername='))
            resolved=(path.parent/Path(src)).resolve()
            if not any(resolved.is_relative_to(root) for root in paths.roots.values()) or not resolved.is_file():
                raise ToolFailure('PROJECT_POLICY','A datasource leaves configured roots or is missing')
            if resolved.suffix.lower() not in {'.gpkg','.tif','.tiff','.png','.jpg','.jpeg'}:
                raise ToolFailure('PROJECT_POLICY','Unsupported project datasource extension')
        p=QgsProject()
        if not p.read(str(path)):
            raise ToolFailure('PROJECT_INVALID','QGIS project could not be opened')
        result={'path':args['path'],'layer_count':len(p.mapLayers()),'layers':[l.name() for l in p.mapLayers().values()]}
        p.clear(); return result
    if action!='create':
        raise ToolFailure('INVALID_ARGUMENT','Project action is create/open')
    path=paths.new_output(args['path'],suffixes={'.qgs','.qgz'})
    p=QgsProject()
    for spec in args.get('layers',[]):
        input_path=paths.resolve(spec['path'],exists=True)
        if input_path.suffix.lower()=='.gpkg':
            layer=_layer(input_path,spec['layer'])
        else:
            layer=QgsRasterLayer(str(input_path),spec.get('name',input_path.stem))
        if not layer.isValid():
            raise ToolFailure('LAYER_INVALID','Project input layer is invalid')
        p.addMapLayer(layer)
    p.setFilePathStorage(Qgis.FilePathType.Relative)
    p.setFileName(str(path))
    if not p.write():
        raise ToolFailure('PROJECT_WRITE_FAILED','QGIS project could not be written')
    result={'path':args['path'],'layer_count':len(p.mapLayers()),'outputs':[args['path']]}
    p.clear(); return result

def render(paths,args):
    from qgis.core import QgsMapSettings,QgsMapRendererParallelJob,QgsRectangle,QgsSingleSymbolRenderer,QgsMarkerSymbol,QgsLineSymbol,QgsFillSymbol
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor
    width,height=args.get('width',1024),args.get('height',768)
    if not isinstance(width,int) or not isinstance(height,int) or not 32<=width<=4096 or not 32<=height<=4096:
        raise ToolFailure('INVALID_ARGUMENT','Render dimensions must be integers 32..4096')
    path=paths.resolve(args['path'],exists=True,suffixes={'.gpkg'})
    layers=[_layer(path,n) for n in args['layers']]
    for layer in layers: _require_valid_features(layer)
    metas=[_layer_meta(path,n) for n in args['layers']]
    if not layers or any(not _same_frame(m,metas[0]) for m in metas) or not metas[0]['frame_id']:
        raise ToolFailure('FRAME_MISMATCH','Render requires an explicit common coordinate frame')
    extent=QgsRectangle()
    for layer in layers:
        family=int(layer.geometryType())
        factory={0:QgsMarkerSymbol,1:QgsLineSymbol,2:QgsFillSymbol}.get(family)
        if factory is None: raise ToolFailure('UNSUPPORTED_RENDER','Render supports point, line and polygon layers')
        style=({'color':'64,91,131,255','outline_color':'24,42,68,255','size':'2.5'} if family==0 else
               {'line_color':'64,91,131,255','line_width':'0.5'} if family==1 else
               {'color':'96,122,155,255','outline_color':'24,42,68,255','outline_width':'0.35'})
        layer.setRenderer(QgsSingleSymbolRenderer(factory.createSimple(style)))
        layer.updateExtents(); extent.combineExtentWith(layer.extent())
    if extent.isEmpty():
        raise ToolFailure('EMPTY_EXTENT','Nothing spatial to render')
    extent.scale(1.1)
    settings=QgsMapSettings(); settings.setLayers(layers); settings.setDestinationCrs(layers[0].crs())
    settings.setExtent(extent); settings.setOutputSize(QSize(width,height)); settings.setBackgroundColor(QColor('white'))
    settings.setOutputDpi(96)
    job=QgsMapRendererParallelJob(settings); job.start(); job.waitForFinished()
    output=paths.new_output(args['output_path'],suffixes={'.png'})
    image=job.renderedImage()
    if image.isNull() or not image.save(str(output),'PNG'):
        raise ToolFailure('RENDER_FAILED','QGIS did not save a PNG')
    return {'path':args['output_path'],'width':image.width(),'height':image.height(),'layers':args['layers'],
            'coordinate_reference':metas[0],'style_policy':'fixed point/line/polygon symbols; 96 DPI','outputs':[args['output_path']]}

def runtime(paths,args):
    from qgis.core import Qgis
    from osgeo import gdal
    return {'available':True,'status':'READY','qgis_version':Qgis.QGIS_VERSION,'gdal_version':gdal.VersionInfo('RELEASE_NAME'),
            'python_version':sys.version.split()[0],'bridge_version':__version__,'gui':False,'gpu':False,
            'roots':list(paths.roots),'default_write_roots':['work','private'],'epsg_inference':False,
            'minimum_layers':list(MINIMUM_LAYERS),'transform_models':['similarity','affine']}

OPERATIONS={'qgis_status':runtime,'qgis_create_geopackage':create_gpkg,'qgis_list_layers':list_layers,
            'qgis_create_layer':create_layer,'qgis_feature':feature,'qgis_import_vector':import_vector,
            'qgis_register_control_points':register_gcps,'qgis_fit_transform':fit,'qgis_residuals':residuals,
            'qgis_apply_transform':apply_transform,'qgis_overlay':overlay,'qgis_export_layer':export_vector,
            'qgis_import_raster':import_raster,'qgis_project':project,'qgis_render':render}

def main():
    request=json.load(sys.stdin)
    app=None
    try:
        if request.get('tool') not in OPERATIONS:
            raise ToolFailure('UNKNOWN_TOOL','Operation is not exposed')
        paths=Paths.from_config(request['paths'])
        app=bootstrap(paths)
        # A module global for the QGIS enum needed only by project serialization.
        global Qgis
        from qgis.core import Qgis
        result=OPERATIONS[request['tool']](paths,request.get('args',{}))
        response={'ok':True,'result':result,'error':None}
    except ToolFailure as exc:
        response={'ok':False,'result':None,'error':exc.as_dict()}
    except (ImportError,OSError) as exc:
        response={'ok':False,'result':None,'error':{'code':'QGIS_RUNTIME_UNAVAILABLE','message':str(exc)[:1000],
                                                  'retryable':False,'details':{}}}
        if app is not None and isinstance(exc,OSError):
            response['error']['code']='IO_ERROR'
    except Exception as exc:
        response={'ok':False,'result':None,'error':{'code':'QGIS_OPERATION_FAILED','message':type(exc).__name__+': '+str(exc)[:1000],
                                                  'retryable':False,'details':{}}}
    if app is not None:
        from qgis.core import QgsProject
        QgsProject.instance().clear()
        app.exitQgis()
    print(json.dumps({'request_id':request.get('request_id'),**response},ensure_ascii=True,allow_nan=False),flush=True)

if __name__=='__main__':
    main()
