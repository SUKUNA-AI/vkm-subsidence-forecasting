"""A rectangular block [0, lx] × [0, ly] × [0, lz] as an ISO 10303-21 (STEP AP214) file in metres — TOY geometry.

Mechanical has no primitive modeller in batch or embedded mode, so tool tests and toy problems import this B-rep:
8 vertices, 12 straight edges, 6 planar faces with outward normals and counter-clockwise loops (each edge is used
once in each direction), one closed shell, one manifold solid. The bytes are deterministic (fixed timestamp).
"""
from __future__ import annotations

# vertices v0..v7 and directed edges e0..e11 (start, end)
_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))
# faces: (name, outward normal, reference direction, point index on the plane, [(edge, same_sense), ...])
_FACES = (
    ("bottom", (0, 0, -1), (1, 0, 0), 0, ((3, False), (2, False), (1, False), (0, False))),
    ("top", (0, 0, 1), (1, 0, 0), 4, ((4, True), (5, True), (6, True), (7, True))),
    ("front_y0", (0, -1, 0), (1, 0, 0), 0, ((0, True), (9, True), (4, False), (8, False))),
    ("back_ymax", (0, 1, 0), (1, 0, 0), 3, ((11, True), (6, False), (10, False), (2, True))),
    ("left_x0", (-1, 0, 0), (0, 1, 0), 0, ((8, True), (7, False), (11, False), (3, True))),
    ("right_xmax", (1, 0, 0), (0, 1, 0), 1, ((1, True), (10, True), (5, False), (9, False))),
)


def _f(x: float) -> str:
    text = repr(float(x))
    if "e" in text or "E" in text:
        mant, exp = text.lower().split("e")
        return (mant if "." in mant else mant + ".") + "E" + str(int(exp))
    return text if "." in text else text + "."


def box_step(lx: float, ly: float, lz: float, name: str = "BOX") -> bytes:
    if min(lx, ly, lz) <= 0:
        raise ValueError("box dimensions must be positive")
    verts = [(0, 0, 0), (lx, 0, 0), (lx, ly, 0), (0, ly, 0), (0, 0, lz), (lx, 0, lz), (lx, ly, lz), (0, ly, lz)]
    rows: list[str] = []

    def add(text: str) -> int:
        rows.append(text)
        return len(rows)

    def point(p) -> int:
        return add(f"CARTESIAN_POINT('',({_f(p[0])},{_f(p[1])},{_f(p[2])}))")

    def direction(d) -> int:
        return add(f"DIRECTION('',({_f(d[0])},{_f(d[1])},{_f(d[2])}))")

    app_ctx = add("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
    add(f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#{app_ctx})")
    prod_ctx = add(f"PRODUCT_CONTEXT('',#{app_ctx},'mechanical')")
    product = add(f"PRODUCT('{name}','{name}','',(#{prod_ctx}))")
    add(f"PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{product}))")
    formation = add(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
    pd_ctx = add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{app_ctx},'design')")
    pdef = add(f"PRODUCT_DEFINITION('design','',#{formation},#{pd_ctx})")
    pds = add(f"PRODUCT_DEFINITION_SHAPE('','',#{pdef})")

    v_ids = [add(f"VERTEX_POINT('',#{point(v)})") for v in verts]
    e_ids = []
    for a, b in _EDGES:
        pa, pb = verts[a], verts[b]
        vec = tuple(pb[k] - pa[k] for k in range(3))
        length = sum(c * c for c in vec) ** 0.5
        d = direction(tuple(c / length for c in vec))
        vector = add(f"VECTOR('',#{d},{_f(length)})")
        line = add(f"LINE('',#{point(pa)},#{vector})")
        e_ids.append(add(f"EDGE_CURVE('',#{v_ids[a]},#{v_ids[b]},#{line},.T.)"))
    f_ids = []
    for _name, normal, ref, pidx, loop in _FACES:
        oriented = [add(f"ORIENTED_EDGE('',*,*,#{e_ids[e]},{'.T.' if sense else '.F.'})") for e, sense in loop]
        eloop = add("EDGE_LOOP('',(" + ",".join(f"#{o}" for o in oriented) + "))")
        bound = add(f"FACE_OUTER_BOUND('',#{eloop},.T.)")
        axis = add(f"AXIS2_PLACEMENT_3D('',#{point(verts[pidx])},#{direction(normal)},#{direction(ref)})")
        plane = add(f"PLANE('',#{axis})")
        f_ids.append(add(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)"))
    shell = add("CLOSED_SHELL('',(" + ",".join(f"#{f}" for f in f_ids) + "))")
    solid = add(f"MANIFOLD_SOLID_BREP('{name}',#{shell})")
    origin = add(f"AXIS2_PLACEMENT_3D('',#{point((0, 0, 0))},#{direction((0, 0, 1))},#{direction((1, 0, 0))})")
    length_unit = add("(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT($,.METRE.))")
    angle_unit = add("(NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.))")
    solid_angle = add("(NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT())")
    uncertainty = add(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{length_unit},'distance_accuracy_value',"
                      "'confusion accuracy')")
    context = add(f"(GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{uncertainty})) "
                  f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length_unit},#{angle_unit},#{solid_angle})) "
                  "REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY'))")
    rep = add(f"ADVANCED_BREP_SHAPE_REPRESENTATION('',(#{origin},#{solid}),#{context})")
    add(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{rep})")

    header = ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('VKM toy block (TOY geometry, metres)'),'2;1');\n"
              f"FILE_NAME('{name}.step','2026-01-01T00:00:00',('vkm-ansys'),('VKM'),'vkm_ansys.mechanical.step_box',"
              "'vkm-ansys','');\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n")
    body = "".join(f"#{i}={row};\n" for i, row in enumerate(rows, 1))
    return (header + body + "ENDSEC;\nEND-ISO-10303-21;\n").encode("ascii")


def face_loops() -> list[tuple[str, tuple[int, int, int], list[int]]]:
    """(face name, outward normal, vertex indices in loop order) — the topology the STEP file encodes."""
    out = []
    for name, normal, _ref, _p, loop in _FACES:
        out.append((name, normal, [(_EDGES[e][0] if sense else _EDGES[e][1]) for e, sense in loop]))
    return out


def edge_uses() -> dict[int, list[bool]]:
    uses: dict[int, list[bool]] = {}
    for *_rest, loop in _FACES:
        for e, sense in loop:
            uses.setdefault(e, []).append(sense)
    return uses
