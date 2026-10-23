"""World representations (theory v0.2, FW §6): computational representations of a PhysicalWorld.

A representation never creates a new physical world (REPRESENTATION_CHOICE, FW §5.2). Sub-packages:

* ``world0`` — microkernels of the reduced representation World-0 (MESO_LAYER_OVERBURDEN_SURFACE):
  kernel types, local laws, ports, closure and equivalence checks, influence operators, gravity roles.
* ``mapdl_batch`` — a thin batch adapter for Ansys MAPDL runs with receipts (solver as adapter).
"""
