"""GPU plane-strain geomechanics of a layered slice (a fast representation for ensembles).

Q4 B-bar elements on structured column meshes, gravity + geostatic initial state, excavation of explicit chambers,
optional backfill, Norton creep (implicit, consistent tangent), exact batched block-tridiagonal factorisation in
torch (CPU or CUDA). Verified against analytic solutions and against MAPDL/OGS on the project's reference slice
(``docs/implementation_work/GPU_SUBSIDENCE_ENSEMBLE_RU.md``). Results are MODEL results; the representation error of this solver
against MAPDL/OGS is measured separately and is not part of the uncertainty of the world.
"""
from .fem import ColumnMesh, Q4BBar, column_vertical_stress, elastic_D, elastic_moduli, stress_from_lambda
from .creep import norton_return, von_mises
from .solver import Materials, PlaneStrainCreepSolver, State, time_steps
from .slice_mesh import MeshSpec, build_slice_batch, chamber_count, mesh_from_npz

__all__ = ["ColumnMesh", "Q4BBar", "column_vertical_stress", "elastic_D", "elastic_moduli", "stress_from_lambda",
           "norton_return", "von_mises", "Materials", "PlaneStrainCreepSolver", "State", "time_steps",
           "MeshSpec", "build_slice_batch", "chamber_count", "mesh_from_npz"]
