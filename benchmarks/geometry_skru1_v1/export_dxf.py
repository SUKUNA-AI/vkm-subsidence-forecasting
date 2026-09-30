"""Export source-coordinate GeoJSON using an already installed ezdxf runtime.

No CAD application is started. This only serializes existing feature coordinates;
individual frame/units and provenance remain attached as DXF XDATA and a sidecar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    import ezdxf
    # Process-local serialization policy; no persisted runtime/config changes.
    ezdxf.options.write_fixed_meta_data_for_testing=True
    parser=argparse.ArgumentParser()
    parser.add_argument("--manifest",default="work/geometry_2026-09-29/layers_manifest.json")
    parser.add_argument("--out",default="work/geometry_2026-09-29/dxf")
    args=parser.parse_args(); repo=Path.cwd().resolve(); out=(repo/args.out).resolve()
    if not out.is_relative_to(repo/"work"): raise ValueError("private DXF must stay in work/")
    out.mkdir(parents=True,exist_ok=True)
    manifest=(repo/args.manifest).resolve()
    if not manifest.is_relative_to(repo): raise ValueError("manifest escaped workspace")
    data=json.loads(manifest.read_text(encoding="utf-8")); results=[]
    for item in data["layers"]:
        source=repo/item["file"]; content=json.loads(source.read_text(encoding="utf-8"))
        doc=ezdxf.new("R2013"); doc.units=6 if item["coordinate_units"] == "m" else 0
        doc.appids.new("VKM_GEOMETRY"); model=doc.modelspace(); count=0
        expected=[]
        for feat in content["features"]:
            geom=feat.get("geometry"); properties=feat.get("properties",{})
            if not geom: continue
            typ,coords=geom["type"],geom["coordinates"]
            entities=[]
            if typ == "Point":
                entities=[model.add_point(coords)]; expected.append(("POINT",coords[:2]))
            elif typ in ("LineString","MultiLineString","Polygon"):
                lines=coords if typ in ("MultiLineString","Polygon") else [coords]
                for line in lines:
                    closed=typ == "Polygon"
                    points=line[:-1] if closed and line[0] == line[-1] else line
                    entities.append(model.add_lwpolyline(points,close=closed)); expected.append(("LWPOLYLINE",points))
            elif typ == "MultiPolygon":
                for polygon in coords:
                    for ring in polygon:
                        points=ring[:-1] if ring[0] == ring[-1] else ring
                        entities.append(model.add_lwpolyline(points,close=True)); expected.append(("LWPOLYLINE",points))
            else:
                raise ValueError(f"unsupported source geometry {typ}")
            tags=[(1000,item["source_frame_id"]),(1000,item["coordinate_reference"]),(1000,item["coordinate_units"])]
            for field in ("stable_id","source_id","source_object_id","verification"):
                tags.append((1000,f"{field}={properties.get(field,'UNKNOWN')}"))
            for entity in entities: entity.set_xdata("VKM_GEOMETRY",tags)
            count+=len(entities)
        path=out/(item["name"]+".dxf"); doc.saveas(path)
        loaded=ezdxf.readfile(path); actual=list(loaded.modelspace())
        if len(actual) != count: raise ValueError("DXF entity count changed on roundtrip")
        maximum=0.
        for entity,(kind,points) in zip(actual,expected):
            if entity.dxftype() != kind: raise ValueError("DXF geometry type changed on roundtrip")
            restored=list(entity.dxf.location)[:2] if kind == "POINT" else [list(p[:2]) for p in entity.get_points()]
            left=[points] if kind == "POINT" else points; right=[restored] if kind == "POINT" else restored
            if len(left)!=len(right): raise ValueError("DXF vertex count changed on roundtrip")
            maximum=max(maximum,max((abs(float(a)-float(b)) for p,q in zip(left,right) for a,b in zip(p,q)),default=0))
            if not entity.has_xdata("VKM_GEOMETRY"): raise ValueError("provenance XDATA lost")
        results.append({"layer":item["name"],"source":item["file"],"source_sha256":digest(source),
                        "file":path.relative_to(repo).as_posix(),"sha256":digest(path),"entities":count,
                        "frame":item["source_frame_id"],"unit":item["coordinate_units"],
                        "dxf_insunits":loaded.units,"roundtrip_max_coordinate_delta":maximum,
                        "crs_authority":"UNKNOWN","verification":"AUTO_EXTRACTED_UNREVIEWED"})
    receipt={"schema":"vkm.source_geometry_dxf/1","ezdxf_version":ezdxf.__version__,
             "input_manifest":args.manifest,"input_manifest_sha256":digest(manifest),"layers":results,
             "command":"work/venv-desktop/Scripts/python.exe -B benchmarks/geometry_skru1_v1/export_dxf.py --manifest "+args.manifest,
             "coordinates":"original source coordinates; no rotation, registration or unit conversion",
             "dxf_polygon_holes":"separate closed polylines; authoritative topology remains in GeoJSON",
             "deterministic_metadata":"fixed serialization date/GUID; not an observation or actual creation timestamp",
             "invalid_polygon_policy":"preserved as closed source polylines without MakeValid; no accepted topology or area claim",
             "cad_application_executed":False,"registration_to_mine_grid":"NOT_ACCEPTED"}
    output=out/"DXF_RECEIPT.json"; output.write_text(json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"layers":len(results),"entities":sum(r['entities'] for r in results),
                      "roundtrip_max_delta":max((r['roundtrip_max_coordinate_delta'] for r in results),default=0)}))


if __name__ == "__main__": main()
