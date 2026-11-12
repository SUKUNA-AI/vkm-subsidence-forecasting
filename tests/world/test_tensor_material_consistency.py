"""Keep staged material declarations consistent with the assembled elastic operator."""

import numpy as np
import pytest

from vkm_world.representations.world0.multiseam import ElasticMesh
from vkm_world.representations.world0.multiseam_stages import TensorStages


def make_stages(mesh_young, stage_young, mesh_poisson=(0.25, 0.25), stage_poisson=(0.25, 0.25)):
    xy = np.array([[0.0, 0.0], [2.0, 0.0], [2.0, 3.0], [0.0, 3.0]])
    coordinates = np.column_stack((np.tile(xy, (3, 1)), np.repeat([0.0, 1.0, 3.0], 4)))
    cells = np.stack((np.arange(8), np.arange(4, 12)))
    mesh = ElasticMesh(coordinates, cells, mesh_young, mesh_poisson, np.arange(4))
    return TensorStages(
        mesh,
        young_pa=stage_young,
        poisson=stage_poisson,
        density=[2200.0, 2400.0],
        rock_cells=[True, False],
        patch_index=[0, 0],
        excavation_fraction=[0.25],
        events=[(3e6, 0, "excavate"), (5e6, 0, "backfill")],
        viscosity_pa_s=1e16,
        fill_young_pa=28e6,
        fill_density=1800.0,
        fill_fraction=0.7,
    )


@pytest.mark.parametrize(
    "stage_young, stage_poisson",
    [
        ([4e9, 8e9], [0.25, 0.25]),
        ([2e9, 16e9], [0.25, 0.25]),
        ([2e9, 8e9], [0.2, 0.25]),
        ([2e9, 8e9], [0.25, 0.3]),
    ],
)
def test_mismatched_active_or_exterior_material_rejected(stage_young, stage_poisson):
    with pytest.raises(ValueError, match="material field differs"):
        make_stages([2e9, 8e9], stage_young, stage_poisson=stage_poisson)


def test_consistent_stiffness_reassembly_changes_equilibrium():
    original = make_stages([2e9, 8e9], [2e9, 8e9])
    stiffer = make_stages([4e9, 16e9], [4e9, 16e9])
    original_state, original_diagnostics = original.initial()
    stiffer_state, stiffer_diagnostics = stiffer.initial()

    assert np.linalg.norm(original_state.displacement_m) > 0
    np.testing.assert_allclose(
        stiffer_state.displacement_m, 0.5 * original_state.displacement_m, rtol=1e-10, atol=1e-16
    )
    np.testing.assert_array_equal(
        original_diagnostics["body_force_n"], stiffer_diagnostics["body_force_n"]
    )
    assert original_diagnostics["global_relative_balance"] < 1e-10
    assert stiffer_diagnostics["global_relative_balance"] < 1e-10
