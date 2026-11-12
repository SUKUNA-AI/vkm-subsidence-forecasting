"""Same grouped-history state schema with lossless uncompressed NPZ storage.

Only serialization differs from ConstitutiveHistory.checkpoint. This writer does
not mutate model/state, change physical identity, or manufacture compatibility.
Use the normal model.reopen for validation and restoration.
"""
from pathlib import Path
import hashlib
import json
import numpy as np

def write_raw_checkpoint(model, path, state, *, accepted=True):
    model._check_state(state)
    path = Path(path)
    arrays = dict(time_s=state.time_s, displacement_m=state.displacement_m, support=state.support, event_cursor=state.event_cursor, identity=model.identity, domain_cells=model.domain)
    metadata = dict(schema='vkm.grouped-history-state/1', identity=model.identity, materials=[], cohorts=[])
    for i, (group, s) in enumerate(zip(model.groups, state.materials, strict=True)):
        arrays[f'm{i}_strain'] = s.strain
        arrays[f'm{i}_stress'] = s.stress
        for k, v in s.memory.items():
            arrays[f'm{i}_memory_{k}'] = v
        metadata['materials'].append(dict(id=group.identifier, signature=s.signature, keys=list(s.memory)))
    for i, c in enumerate(state.cohorts):
        metadata['cohorts'].append(dict(identifier=c.identifier, birth_s=c.birth_s))
        for k in ('cells', 'reference_strain', 'stiffness_pa', 'mass_kg', 'capacity_reference_fraction'):
            arrays[f'c{i}_{k}'] = getattr(c, k)
    if state.water is not None:
        arrays.update(pressure_increment_pa=state.water.pressure_pa, dilation_increment_m3=state.water.dilation_m3)
        metadata['water_topology_id'] = state.water.topology_id
    arrays['metadata_json'] = json.dumps(metadata, sort_keys=True)
    np.savez(path, **arrays)
    sidecar = dict(schema='vkm.grouped-history-binding/1', identity=model.identity, accepted=bool(accepted), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), dependencies=model.identity_payload)
    Path(str(path) + '.binding.json').write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding='utf-8')
