"""vkm_world — evidence-backed 3D+time world of the Verkhnekamsk potash deposit / SKRU-1.

Layers (kept strictly separate):

* ``core``          provenance, epistemic status, uncertainty, units;
* ``spatial``       coordinates, analytic/differential geometry, spatial hierarchy;
* ``geology``       stratigraphy, boreholes, horizons, interpolation/geostatistics;
* ``mining``        mining objects, chronology, backfill;
* ``materials``     material parameters (LAB/MASSIF/...), rheology laws;
* ``physics``       process registry and analytical/semi-analytical process models;
* ``observations``  observation catalog and observation operators (state → measurement);
* ``inference``     inverse problems, uncertainty and sensitivity;
* ``worldspec``     the typed WorldSpec document, IO and validation;
* ``viz``           diagnostic visualisation.

A WorldSpec is NOT a solver model. Solver adapters (OGS, FEniCS, MATLAB, gprMax ...) are future
consumers of a WorldSpec state and must not define the world.
"""
__version__ = "0.1.0.dev0"
