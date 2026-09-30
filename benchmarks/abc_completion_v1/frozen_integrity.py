"""Compare frozen first-pass bytes with hashes from the preceding public receipts."""
from __future__ import annotations

import argparse
from pathlib import Path

from benchmarks.abc_completion_v1.package import load, sha, write


def verify(repo):
    old=repo/'work/figure_readings_2026-09-29/v2'
    prior=load(repo/'docs/corpus_platform/receipts/figure_readings_v2.json')
    checked={}

    def check(path,expected):
        actual=sha(path)
        if actual!=expected:raise ValueError(f'frozen input changed: {path.relative_to(repo)}')
        checked[path.relative_to(repo).as_posix()]=actual

    for filename,expected in prior['input_hashes'].items():check(old/filename,expected)
    raw_manifest=old/'raw_responses_manifest.json'
    assembled_manifest=old/'assembled_outputs_manifest.json'
    check(raw_manifest,prior['raw_response_manifest_sha256'])
    check(assembled_manifest,prior['output_manifest_hash'])
    for reader,files in load(raw_manifest).items():
        for filename,expected in files.items():check(old/'raw'/reader/filename,expected)
    for filename,expected in load(assembled_manifest)['files'].items():check(old/filename,expected)
    for filename,expected in prior['manual_review_hashes'].items():check(old/filename,expected)
    qgis=load(repo/'docs/corpus_platform/receipts/qgis_mcp_2026-09-30.json')
    acceptance_file=repo/'work/qgis_2026-09-30/source_import/acceptance.json'
    check(acceptance_file,qgis['acceptance_artifacts'][acceptance_file.relative_to(repo).as_posix()])
    acceptance=load(acceptance_file)
    check(repo/'work/geometry_2026-09-29/geometry_sources.gpkg',acceptance['output_sha256'])
    check(repo/'work/geometry_2026-09-29/qgis_import_manifest.json',acceptance['manifest_sha256'])
    geometry=load(repo/'work/geometry_2026-09-29/layers_manifest.json')
    for layer in geometry['layers']:
        check(repo/layer['file'],layer['sha256'])
        if layer.get('overlay'):check(repo/layer['overlay'],layer['overlay_sha256'])
    return {'schema':'vkm.sol_frozen_integrity/1','status':'PASS','checked_file_count':len(checked),
            'files':checked,'raw_response_counts':{reader:len(files) for reader,files in load(raw_manifest).items()},
            'original_geopackage_sha256':acceptance['output_sha256'],
            'command':'python -B -m benchmarks.abc_completion_v1.frozen_integrity'}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',default='work/abc_completion_2026-09-30/frozen_integrity.json')
    args=parser.parse_args();repo=Path.cwd().resolve();out=(repo/args.out).resolve()
    if not out.is_relative_to(repo/'work'):raise ValueError('frozen provenance detail stays PRIVATE')
    result=verify(repo);write(out,result)
    print(f"{result['status']}: {result['checked_file_count']} frozen files unchanged")
