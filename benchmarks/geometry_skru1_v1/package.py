"""Freeze the current source layers for coordinate-preserving QGIS/DXF imports."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from run import sha,write,local_file


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--out",default="work/geometry_2026-09-29")
    args=parser.parse_args();repo=Path.cwd().resolve();out=local_file(repo,args.out)
    if not out.is_relative_to(repo/"work"):raise ValueError("PRIVATE package must stay work/")
    auto=json.loads((out/"layers_manifest.json").read_text(encoding="utf-8"))
    reviewed=json.loads((out/"reviewed_layers_manifest.json").read_text(encoding="utf-8"))
    layers=auto["layers"]+reviewed["layers"]
    if len({v['name'] for v in layers})!=len(layers):raise ValueError("duplicate source layer names")
    for layer in layers:
        path=repo/layer["file"]
        if sha(path)!=layer['sha256']:raise ValueError(f"input changed before freeze: {layer['file']}")
        if layer.get('overlay') and sha(repo/layer['overlay'])!=layer['overlay_sha256']:raise ValueError("overlay changed before freeze")
    obj={"schema":"vkm.source_geometry_import_snapshot/1","layers":layers,"required_layers":auto["required_layers"],
         "raw_figures":auto['source_figures'],"raw_stats":auto["stats"],
         "verification":"AUTO_EXTRACTED_UNREVIEWED_AND_PENDING_INDEPENDENT_ANNOTATION_ACCEPTANCE",
         "source_frame_contract":"One source frame per layer; px/pt/unknown table units; no EPSG or mine-grid registration",
         "table_coordinate_unit":"UNKNOWN_NOT_PROVEN_BY_NUMBER_RANGE",
         "independent_semantic_review":reviewed["required_independent_review"],
         "inputs":{str(p.relative_to(repo).as_posix()):sha(p) for p in (out/"layers_manifest.json",out/"reviewed_layers_manifest.json",out/"receipt.json",out/"reviewed_annotations_receipt.json")}}
    path=out/"qgis_import_manifest.json";write(path,obj)
    print(json.dumps({"manifest":path.relative_to(repo).as_posix(),"sha256":sha(path),"layers":len(layers),
                      "nonempty_layers":sum(l['feature_count']>0 for l in layers),"features":sum(l['feature_count'] for l in layers)}))


if __name__ == "__main__":main()
