"""Continuous geological interpretation under the complete supplied terrain.

Real source geometry stays in ignored work/. Domain extension and display
smoothing are declared choices; source elevations and contacts are preserved.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np
from scipy.spatial import cKDTree
from shapely import linestrings, polygonize
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

from build_detailed_geology import Model, UNITS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from vkm_world.geometry_build.linked_geology import layer_mesh, write_vtu, write_vtm


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def encoded(array, dtype):
    array = np.asarray(array, dtype=np.dtype(dtype).newbyteorder('<'), order='C')
    return {'dtype': dtype, 'shape': list(array.shape), 'data': base64.b64encode(array.tobytes()).decode('ascii')}


def terrain_footprint(points, triangles):
    edges = np.sort(np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]]), axis=1)
    edges, counts = np.unique(edges, axis=0, return_counts=True)
    if np.any(counts > 2):
        raise ValueError('nonmanifold terrain')
    parts = list(polygonize(linestrings(points[edges[counts == 1], :2])).geoms)
    if len(parts) != 1 or not parts[0].is_valid or len(parts[0].interiors):
        raise ValueError('complete terrain must have one continuous footprint without holes')
    return parts[0]


def build(args):
    repo = Path(__file__).resolve().parents[1]
    out = args.output.resolve()
    if not out.is_relative_to((repo / 'work').resolve()):
        raise ValueError('private model outputs belong in ignored work/')
    out.mkdir(parents=True, exist_ok=False)
    (out / 'data').mkdir()
    files = {
        'builder': Path(__file__),
        'geometry': repo / 'src/vkm_world/geometry_build/linked_geology.py',
        'source_model_code': repo / 'scripts/build_detailed_geology.py',
        'base_manifest': args.base / 'data/geometry_manifest.json',
        'terrain': args.terrain,
        'surface_display': args.surface_display,
        'plans': args.plans,
        'terrain_vtp': args.terrain.with_name('terrain_local_SH1_EGM2008_full.vtp'),
        'display_vtp': args.surface_display.with_suffix('.vtp'),
        'plotly_runtime': args.previous / 'plotly.min.js',
    }
    pins = {k: sha(p) for k, p in files.items()}
    source = read(files['base_manifest'])
    original = read(args.terrain)
    smooth = read(args.surface_display)
    terrain = np.asarray(original['points'], dtype=np.float64)
    displayed = np.asarray(smooth['points'], dtype=np.float64)
    triangles = np.asarray(original['triangles'], dtype=np.int64)
    if not np.array_equal(terrain[:, :2], displayed[:, :2]) or not np.array_equal(triangles, smooth['triangles']):
        raise ValueError('smoothing changed source XY or topology')
    plans = read(args.plans)['features']
    model = Model(source)
    domain = terrain_footprint(terrain, triangles)
    mapped = unary_union([shape(f['geometry']) for f in plans if f['properties']['kind'] in ('BLOCK', 'PANEL')])
    print('Building continuous full-terrain footprint; spacing=' + str(args.spacing), flush=True)
    ref = layer_mesh(domain, lambda xy: np.zeros(len(xy)), 1, spacing=args.spacing)
    n = len(ref['points']) // 2
    xy = ref['points'][:n, :2]
    cells = ref['wedges'][:, :3]
    boundary = ref['faces'][2 * len(cells):][::2, :2]
    levels = model.interfaces(xy)
    if levels.shape != (10, n) or not np.all(np.diff(levels, axis=0) > 0):
        raise ValueError('stratigraphy order failure')
    entries = []
    checks = []
    for i, (code, name, color) in enumerate(UNITS):
        print('Writing continuous ' + code, flush=True)
        points = np.vstack([np.column_stack([xy, levels[i]]), np.column_stack([xy, levels[i + 1]])])
        mesh = {**ref, 'points': points}
        filename = 'data/complete_' + code + '.vtu'
        write_vtu(out / filename, mesh, i)
        entries.append((name + ' | INTERPOLATION / MODEL_CHOICE', filename))
        checks.append({'unit': code, 'points': len(points), 'wedges': len(cells),
                       'min_thickness_m': float(np.min(levels[i + 1] - levels[i])),
                       'plan_area_m2': domain.area})
    print('Preparing shared full-resolution exterior renderer', flush=True)
    tree = cKDTree(terrain[:, :2])
    plan_lines = []
    colors = ['#2b6078', '#ac7c32', '#638681', '#795485']
    for ki, kind in enumerate(('BLOCK', 'PANEL', 'PROTECTIVE_PILLAR', 'REMAINING_RESERVES_AREA')):
        x, y, z = [], [], []
        for f in plans:
            if f['properties']['kind'] != kind:
                continue
            geometry = shape(f['geometry'])
            for part in ([geometry] if geometry.geom_type == 'Polygon' else geometry.geoms):
                for ring in [part.exterior, *part.interiors]:
                    a = np.asarray(ring.segmentize(25).coords)
                    h = model.roof(a[:, :2])
                    x.extend([*a[:, 0], None]); y.extend([*a[:, 1], None]); z.extend([*h, None])
        plan_lines.append({'x': x, 'y': y, 'z': z, 'name': kind + ' · исходные контуры', 'color': colors[ki]})
    terrain_source = args.terrain.with_name('terrain_local_SH1_EGM2008_full.vtp')
    shutil.copyfile(terrain_source, out / 'data/terrain_source.vtp')
    display_vtp = args.surface_display.with_suffix('.vtp')
    shutil.copyfile(display_vtp, out / 'data/terrain_display.vtp')
    entries += [('DSM source EGM2008 | no vertical tie', 'data/terrain_source.vtp'),
                ('Smoothed display | DERIVATION, not DTM', 'data/terrain_display.vtp')]
    write_vtm(out / 'complete_geology.vtm', entries)
    manifest = {
        'schema': 'vkm.complete_geological_interpretation/1', 'inputs_sha256': pins,
        'domain': {'role': 'complete original terrain footprint', 'status': 'MODEL_CHOICE',
                   'area_m2': domain.area, 'components': 1, 'holes': 0,
                   'mapped_mining_area_m2': domain.intersection(mapped).area,
                   'extension_area_m2': domain.difference(mapped).area,
                   'extension_status': 'EXTRAPOLATION / MODEL_CHOICE; not new observation'},
        'source012_interval_inputs': source['source012_interval_inputs'],
        'source012_branch': source['source012_branch'],
        'source040_prior_support_values_m': source['source040_prior_support_values_m'],
        'base_manifest_reference': os.path.relpath(files['base_manifest'], out / 'data').replace('\\', '/'),
        'surface_smoothing': smooth.get('smoothing_metadata', smooth.get('smoothing')),
        'geology_to_DSM_vertical_tie': None,
        'joint_display_delta_Z_m': None,
        'source_terrain_unchanged': True,
        'default_cutaway': False, 'default_all_9_units': True,
        'default_vertical_exaggeration': 1,
        'rendering': 'complete exterior of all nine native volumes; shared internal material faces culled, no spatial decimation',
        'grid_spacing_m': args.spacing,
        'native_wedges': len(cells) * 9,
        'native_plan_vertices': n, 'native_plan_triangles': len(cells),
        'units': checks,
        'source_mining_constraints_reference': os.path.relpath(files['base_manifest'], out / 'data').replace('\\', '/'),
        'internal_productive_horizons': 'KrII local scenarios available separately; individual AB/V/VS/VK boundaries remain UNKNOWN',
        'scientific_status': 'INTERPOLATION / EXTRAPOLATION / MODEL_CHOICE',
        'full_solvers': 'NOT_RUN', 'field_validation': False,
    }
    save(out / 'data/complete_manifest.json', manifest)
    save(out / 'data/display_domains.geojson', {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'role': 'continuous_massif_MODEL_CHOICE'}, 'geometry': mapping(domain)},
        {'type': 'Feature', 'properties': {'role': 'mapped_mining_overlay_not_geological_boundary'}, 'geometry': mapping(mapped)}]})
    scene = {
        'schema': 'vkm.complete_scene/1', 'xy': encoded(xy, 'f4'), 'triangles': encoded(cells, 'i4'),
        'levels': encoded(levels, 'f4'), 'edges': encoded(boundary, 'i4'),
        'terrain_xy': encoded(terrain[:, :2], 'f4'), 'terrain_triangles': encoded(triangles, 'i4'),
        'terrain_raw_z': encoded(terrain[:, 2], 'f4'), 'terrain_smooth_z': encoded(displayed[:, 2], 'f4'),
        'units': [{'id': c, 'name': name, 'color': color} for c, name, color in UNITS],
        'plan_lines': plan_lines, 'recent_points': [],
        'meta': {'bounds': list(domain.bounds), 'z_range': [float(levels.min()), float(max(levels.max(), terrain[:, 2].max()))],
                 'smoothing_sigma_m': 36, 'field_spacing_m': args.spacing,
                 'source_note': 'Полный непрерывный массив; продолжение за опоры — сценарий. Высотная связь геологии и DSM UNKNOWN; совместный ΔZ=0 — сценарный.'},
    }
    (out / 'data/scene.js').write_text('window.VKM_SCENE=' + json.dumps(scene, ensure_ascii=False, separators=(',', ':')) + ';\n', encoding='utf-8')
    # The renderer and late-data overlay are finalized separately, after independent readback.
    shutil.copyfile(args.previous / 'plotly.min.js', out / 'plotly.min.js')
    if pins != {k: sha(p) for k, p in files.items()}:
        raise RuntimeError('input changed during build')
    outputs = {p.relative_to(out).as_posix(): sha(p) for p in sorted(out.rglob('*')) if p.is_file()}
    save(out / 'geometry_receipt.json', {'schema': 'vkm.complete_geometry_receipt/1',
        'command': sys.argv, 'inputs_sha256': pins, 'outputs_sha256': outputs,
        'counts': {'units': 9, 'wedges': len(cells) * 9, 'plan_triangles': len(cells), 'plan_vertices': n,
                   'terrain_points': len(terrain), 'terrain_triangles': len(triangles), 'plan_features': len(plans)},
        'input_unchanged': True, 'full_solvers': 'NOT_RUN'})
    print(json.dumps({'status': 'BUILT_GEOMETRY', 'wedges': len(cells) * 9, 'domain_km2': domain.area / 1e6}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'terrain', 'surface-display', 'plans', 'previous', 'output'):
        p.add_argument('--' + name, required=True, type=Path)
    p.add_argument('--spacing', type=float, default=10.)
    build(p.parse_args())
