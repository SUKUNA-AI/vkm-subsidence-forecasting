"""Classify frozen invalid candidates; split only at existing repeated vertices.

No MakeValid, buffering, snapping, invented intersection point or hole deletion.
A repaired graphic representation remains AUTO_EXTRACTED_UNREVIEWED.
"""
from __future__ import annotations

import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3

from benchmarks.abc_completion_v1.package import load,sha,write


def nonzero_edges(rings,*,directed=False):
    edges=Counter()
    for ring in rings:
        for a,b in zip(ring,ring[1:]):
            a,b=tuple(a),tuple(b)
            if a!=b:edges[(a,b) if directed else tuple(sorted((a,b)))]+=1
    return edges


def signed_area(ring):
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:]))/2


def split_closed_ring(ring):
    """Decompose a closed edge walk at repeated nodes; reject non-area branches."""
    if len(ring)<4 or tuple(ring[0])!=tuple(ring[-1]):raise ValueError('ring must be closed')
    points=[]
    for point in ring:
        point=tuple(point)
        if not points or point!=points[-1]:points.append(point)
    walk=[];positions={};cycles=[]
    for point in points:
        if point in positions:
            offset=positions[point];cycle=walk[offset:]+[point]
            if len(cycle)<4 or signed_area(cycle)==0:raise ValueError('non-area branch retained as unresolved')
            cycles.append(cycle);walk=walk[:offset+1];positions={p:i for i,p in enumerate(walk)}
        else:
            positions[point]=len(walk);walk.append(point)
    if len(walk)!=1 or not cycles:raise ValueError('closed walk decomposition failed')
    orientation=signed_area(ring)
    if orientation==0 or any(signed_area(cycle)*orientation<=0 for cycle in cycles):
        raise ValueError('mixed winding requires a fill-rule choice')
    if nonzero_edges([ring],directed=True)!=nonzero_edges(cycles,directed=True):raise ValueError('source edges changed')
    return cycles


def rings_of(geometry):
    if geometry['type']=='Polygon':return geometry['coordinates']
    if geometry['type']=='MultiPolygon':return [ring for polygon in geometry['coordinates'] for ring in polygon]
    raise ValueError('invalid candidate is not a polygon')


def classify(geometry):
    from shapely.geometry import Polygon,shape
    from shapely.validation import explain_validity
    from shapely.strtree import STRtree
    geom=shape(geometry)
    if geom.is_valid:return ['QGIS_GEOS_VALIDITY_DISAGREEMENT'],explain_validity(geom)
    issues=set();polygons=[geometry['coordinates']] if geometry['type']=='Polygon' else geometry['coordinates']
    for rings in polygons:
        simple=[]
        for ring in rings:
            polygon=Polygon(ring);simple.append(polygon)
            if not polygon.is_valid:issues.add('SELF_CROSSED_OR_SELF_TOUCHED_RING')
            if polygon.area==0:issues.add('ZERO_AREA_RING')
        exterior=simple[0]
        if exterior.is_valid:
            exterior_boundary=exterior.boundary
            for hole in simple[1:]:
                if hole.is_valid:
                    if not exterior.covers(hole):issues.add('HOLE_OUTSIDE_OR_CROSSING_EXTERIOR')
                    elif exterior_boundary.intersects(hole.boundary):issues.add('HOLE_TOUCHES_EXTERIOR')
        holes=[hole for hole in simple[1:] if hole.is_valid]
        tree=STRtree(holes)
        for index,hole in enumerate(holes):
            for other_index in tree.query(hole):
                if int(other_index)<=index:continue
                other=holes[int(other_index)]
                if hole.contains(other) or other.contains(hole):issues.add('NESTED_HOLES')
                elif hole.intersection(other).area>0:issues.add('OVERLAPPING_HOLES')
                elif hole.boundary.intersects(other.boundary):issues.add('TOUCHING_HOLES')
    if not issues:issues.add('OTHER_INVALID_TOPOLOGY')
    return sorted(issues),explain_validity(geom)


def safe_restructure(geometry):
    from shapely.geometry import Polygon,MultiPolygon,mapping,shape
    if shape(geometry).is_valid:return None,'already valid in current GEOS; no repair inferred'
    polygons=[geometry['coordinates']] if geometry['type']=='Polygon' else geometry['coordinates']
    rebuilt=[];did_split=False
    try:
        for rings in polygons:
            exteriors=split_closed_ring(rings[0]);did_split|=len(exteriors)>1
            shells=[Polygon(ring) for ring in exteriors]
            if any(not shell.is_valid for shell in shells):return None,'crossing requires new intersection vertices'
            holes=[[] for shell in shells]
            for ring in rings[1:]:
                cycles=split_closed_ring(ring);did_split|=len(cycles)>1
                for cycle in cycles:
                    hole=Polygon(cycle)
                    if not hole.is_valid:return None,'hole remains self-crossed'
                    owners=[i for i,shell in enumerate(shells) if shell.contains(hole)
                            and shell.boundary.disjoint(hole.boundary)]
                    if len(owners)!=1:return None,'hole role or owner is not uniquely supported'
                    holes[owners[0]].append(cycle)
            rebuilt.extend(Polygon(shell,assigned) for shell,assigned in zip(exteriors,holes))
        if not did_split:return None,'no existing-vertex split resolves this topology'
        result=rebuilt[0] if len(rebuilt)==1 else MultiPolygon(rebuilt)
        if not result.is_valid:return None,'existing-vertex split remains invalid'
        output=mapping(result)
        if nonzero_edges(rings_of(geometry),directed=True)!=nonzero_edges(rings_of(output),directed=True):
            return None,'boundary edge multiset changed'
        return output,'existing-vertex split preserves every directed nonzero source edge, winding and coordinate'
    except ValueError as error:
        return None,str(error)


def main(args):
    import shapely
    from shapely.geometry import mapping
    repo=Path.cwd().resolve();out=(repo/args.out).resolve()
    if not out.is_relative_to(repo/'work'):raise ValueError('source topology must remain PRIVATE')
    acceptance_file=repo/'work/qgis_2026-09-30/source_import/acceptance.json';acceptance=load(acceptance_file)
    gpkg=repo/'work/geometry_2026-09-29/geometry_sources.gpkg'
    if sha(gpkg)!=acceptance['output_sha256']:raise ValueError('frozen GeoPackage changed')
    records=[];repaired=defaultdict(list);inputs={gpkg.relative_to(repo).as_posix():acceptance['output_sha256'],
        acceptance_file.relative_to(repo).as_posix():sha(acceptance_file)}
    # Read only frozen source polygons flagged FAIL by the preceding actual QGIS import.
    with sqlite3.connect(gpkg.resolve().as_uri()+'?mode=ro',uri=True) as db:
        for layer in acceptance['records']:
            if not layer.get('invalid_geometry_count'):continue
            name=layer['layer'];quoted='"'+name.replace('"','""')+'"'
            rows=db.execute(f"SELECT stable_id,source_id,page_id,figure_id,source_frame_id,geom,properties_json FROM {quoted} WHERE json_extract(properties_json,'$.qgis_geometry_validity.status')='FAIL'").fetchall()
            if len(rows)!=layer['invalid_geometry_count']:raise ValueError('invalid feature accounting changed')
            for identifier,source,page,figure,frame,blob,properties in rows:
                # GeoPackage header: 8 bytes plus the declared envelope; WKB follows.
                if blob[:2]!=b'GP':raise ValueError('invalid GeoPackage geometry header')
                envelope=(blob[3]>>1)&7
                if envelope>4:raise ValueError('unsupported envelope')
                offset=8+[0,32,48,48,64][envelope]
                geometry=mapping(shapely.from_wkb(blob[offset:]));props=json.loads(properties)
                issues,reason=classify(geometry);candidate,method=safe_restructure(geometry)
                row={'record_id':identifier,'layer':name,'source_id':source,'page_id':page,'figure':figure,
                    'source_frame_id':frame,'original_gpkg_geometry_sha256':hashlib.sha256(blob).hexdigest(),
                    'qgis_prior_reason':props['qgis_geometry_validity']['reason'],'geos_reason':reason,
                    'issue_classes':issues,'status':'REPAIRED_GRAPHIC_CANDIDATE' if candidate else 'UNRESOLVED_GRAPHIC_TOPOLOGY',
                    'verification_method':'GEOS_CLASSIFICATION_AND_EXISTING_VERTEX_EDGE_PRESERVATION',
                    'reason':method,'admitted_to_evidence':False,'verification':'AUTO_EXTRACTED_UNREVIEWED',
                    'epistemic_status':'DERIVATION','geometry_semantics_accepted':False,
                    'normal_methods_attempted':['frozen QGIS validity','individual rings','hole/exterior relationships',
                                              'existing repeated-vertex split','strict GEOS and boundary-edge equality'],
                    'astra_candidate':False,'astra_exclusion_reason':'Automatic primitive without established scientific semantics; escalate only if reviewed semantic geometry depends on it'}
                if candidate:
                    derived={**props,'stable_id':identifier+'-topology','parent_stable_id':identifier,
                        'source_frame_id':frame,'verification':'AUTO_EXTRACTED_UNREVIEWED','epistemic_status':'DERIVATION',
                        'topology_status':'PASS','qgis_geometry_validity':{'status':'PASS','reason':None},
                        'topology_method':method,'source_boundary_edges_preserved':True,'admitted_to_evidence':False}
                    repaired[name].append({'type':'Feature','id':identifier+'-topology','geometry':candidate,'properties':derived})
                records.append(row)
            print(f"{name}: {len(rows)} classified",flush=True)
    if len(records)!=acceptance['invalid_geometry_count'] or len(records)!=1938:
        raise ValueError('all original invalid candidates must be accounted for')
    outputs={}
    for name,features in sorted(repaired.items()):
        path=out/'repaired_layers'/f'{name}.geojson'
        write(path,{'type':'FeatureCollection','features':features,'coordinate_reference':'SOURCE_FRAME_ONLY',
                    'source_layer':name,'semantic_geometry_accepted':False})
        outputs[path.relative_to(repo).as_posix()]=sha(path)
    path=out/'classification.json';write(path,{'schema':'vkm.sol_topology_classification/1','records':records})
    outputs[path.relative_to(repo).as_posix()]=sha(path)
    receipt={'schema':'vkm.sol_topology_receipt/1','original_invalid':len(records),
        'status_counts':dict(Counter(row['status'] for row in records)),
        'issue_class_counts':dict(Counter(issue for row in records for issue in row['issue_classes'])),
        'class_counts_are_multilabel':True,'layers_with_invalid':sum(bool(layer.get('invalid_geometry_count')) for layer in acceptance['records']),
        'repaired_layers':len(repaired),'makevalid_calls':0,'new_coordinates_or_nonzero_edges':0,
        'raw_frozen_unchanged':True,'semantic_geometry_accepted':False,'shapely_version':shapely.__version__,
        'geos_version':shapely.geos_version_string,'inputs':inputs,'outputs':outputs,
        'command':'existing-qgis-python -B -m benchmarks.abc_completion_v1.topology'}
    write(out/'receipt.json',receipt);print(json.dumps({key:receipt[key] for key in ('original_invalid','status_counts','issue_class_counts')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',default='work/abc_completion_2026-09-30/geometry/topology')
    main(parser.parse_args())
