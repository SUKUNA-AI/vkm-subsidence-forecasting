"""Explicit geological scenarios with source ranges and bounded plan support.

These are interpreted geometries, never newly observed contacts or workings.
No real source values or coordinates are embedded in this module.
"""
from __future__ import annotations

import math
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from shapely import constrained_delaunay_triangles, box as vector_boxes, covers, intersects, prepare, set_precision
from shapely.affinity import rotate
from shapely.geometry import Polygon, box


def polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        return [geometry] if geometry.area > 0 else []
    return [part for item in getattr(geometry, "geoms", []) for part in polygon_parts(item)]


def plan_triangles(polygon, spacing):
    """Clip a conforming grid to the footprint, preserving concavities and holes.

    Clipping can introduce a vertex in the middle of a shared grid side. Both
    cells receive the union of those side vertices before triangulation. Merely
    welding equal coordinates afterwards does not repair a hanging vertex.
    """
    if not polygon.is_valid or polygon.is_empty or not math.isfinite(spacing) or spacing <= 0:
        raise ValueError("valid nonempty polygon and positive spacing required")
    source_area=polygon.area
    # Nanometre-scale computational snapping removes machine-epsilon slivers at
    # coincident source/grid edges. Source records are never rewritten.
    polygon=set_precision(polygon,1e-8)
    if polygon.is_empty:
        raise ValueError("footprint collapsed at computational precision")
    xmin, ymin, xmax, ymax = polygon.bounds
    # Bulk classify complete interior grid cells, reserving polygon operations for
    # the source boundary. This retains every cell at fine display resolutions.
    x_edges = np.round(np.append(np.arange(xmin, xmax, spacing), xmax), 8)
    y_edges = np.round(np.append(np.arange(ymin, ymax, spacing), ymax), 8)
    if np.any(np.diff(x_edges) <= 0) or np.any(np.diff(y_edges) <= 0):
        raise ValueError("spacing creates collapsed grid cells at computational precision")
    gx,gy=np.meshgrid(x_edges[:-1],y_edges[:-1],indexing='ij')
    hx,hy=np.meshgrid(x_edges[1:],y_edges[1:],indexing='ij')
    x=gx.ravel();y=gy.ravel();xx=hx.ravel();yy=hy.ravel()
    cells=vector_boxes(x,y,xx,yy);prepare(polygon)
    interior=covers(polygon,cells);boundary=intersects(polygon,cells)&~interior
    ny = gy.shape[1]
    clipped_parts = {}
    side_nodes = {}
    affected_interior = set()
    for index in np.flatnonzero(boundary):
        parts = polygon_parts(polygon.intersection(cells[index], grid_size=1e-8))
        clipped_parts[index] = parts
        # Keys describe the entire grid side, so adjacent cells share a key.
        sides = ((0, x[index], y[index], yy[index], index-ny),
                 (0, xx[index], y[index], yy[index], index+ny),
                 (1, y[index], x[index], xx[index], index-1),
                 (1, yy[index], x[index], xx[index], index+1))
        for part in parts:
            for ring in (part.exterior, *part.interiors):
                for vertex in ring.coords[:-1]:
                    for axis, constant, low, high, neighbour in sides:
                        other = 1-axis
                        if vertex[axis] == constant and low < vertex[other] < high:
                            side_nodes.setdefault((axis, constant, low, high), set()).add(vertex[other])
                            # A horizontal side at the row end has no neighbour
                            # across the array wrap, even when that index exists.
                            same_column = axis == 0 or index//ny == neighbour//ny
                            if same_column and 0 <= neighbour < len(cells) and interior[neighbour]:
                                affected_interior.add(neighbour)
    for index in sorted(affected_interior):
        interior[index] = False
        clipped_parts[index] = [cells[index]]

    def node_ring(ring, index):
        result = []
        coords = list(ring.coords)
        for a, b in zip(coords, coords[1:]):
            result.append(a)
            for axis, constant, low, high in (
                (0, x[index], y[index], yy[index]), (0, xx[index], y[index], yy[index]),
                (1, y[index], x[index], xx[index]), (1, yy[index], x[index], xx[index]),
            ):
                if a[axis] != constant or b[axis] != constant:
                    continue
                other = 1-axis
                values = side_nodes.get((axis, constant, low, high), ())
                values = sorted((v for v in values if min(a[other], b[other]) < v < max(a[other], b[other])),
                                reverse=b[other] < a[other])
                result.extend((constant, v) if axis == 0 else (v, constant) for v in values)
        return result

    lo=np.column_stack([x[interior],y[interior]]);hi=np.column_stack([xx[interior],yy[interior]])
    se=np.column_stack([hi[:,0],lo[:,1]]);nw=np.column_stack([lo[:,0],hi[:,1]])
    full=np.concatenate([np.stack([lo,se,hi],axis=1),np.stack([lo,hi,nw],axis=1)])
    triangles = []
    for index, parts in sorted(clipped_parts.items()):
        for part in parts:
            part = Polygon(node_ring(part.exterior, index),
                           [node_ring(ring, index) for ring in part.interiors])
            for triangle in constrained_delaunay_triangles(part).geoms:
                if triangle.area < 1e-10:
                    continue
                vertices = np.asarray(triangle.exterior.coords)[:3, :2].copy()
                cross = np.linalg.det(np.array([vertices[1] - vertices[0], vertices[2] - vertices[0]]))
                if cross < 0:
                    vertices[[1, 2]] = vertices[[2, 1]]
                triangles.append(vertices)
    triangles=np.concatenate([full,np.asarray(triangles).reshape(-1,3,2)])
    a=triangles[:,1]-triangles[:,0];b=triangles[:,2]-triangles[:,0]
    area=float(np.abs(a[:,0]*b[:,1]-a[:,1]*b[:,0]).sum()/2)
    if not math.isclose(area, source_area, rel_tol=1e-8, abs_tol=1e-6):
        raise ValueError("triangulation lost source footprint area")
    return np.asarray(triangles)


def folded_roof(xy, bounds, z_min, z_max, wing_span, amplitude, phase=0., direction_deg=90.):
    """A deterministic range-bounded fold scenario, not a fitted geological map.

    Wing span is mapped to HALF a sine period by an explicit model choice.
    Printed amplitude is interpreted as peak-to-trough relief for this scenario.
    Broad trend consumes the remainder of the reported areal height range.
    No printed dip is imposed across an unsupported full-panel footprint.
    """
    if not all(math.isfinite(v) for v in (z_min, z_max, wing_span, amplitude, phase, direction_deg)):
        raise ValueError("finite scenario parameters required")
    if z_min > z_max or wing_span <= 0 or amplitude < 0:
        raise ValueError("ordered heights, positive wing span, nonnegative amplitude required")
    xy = np.asarray(xy, dtype=float)
    xmin, ymin, xmax, ymax = bounds
    angle = math.radians(direction_deg)
    direction = np.array([math.cos(angle), math.sin(angle)])
    corners = np.array([[xmin,ymin],[xmin,ymax],[xmax,ymin],[xmax,ymax]]) @ direction
    u = xy @ direction
    normalized = (u-corners.min()) / max(float(np.ptp(corners)), 1e-12)
    relief = min(amplitude, z_max-z_min)
    trend = (z_max-z_min-relief) * normalized
    fold = relief * (1 + np.sin(math.pi*u/wing_span + phase)) / 2
    return z_min + trend + fold


def layer_mesh(polygon, roof_function, thickness_m, spacing=30.):
    """Wedges for volume consumers and a watertight exterior for rendering.

    Thickness is vertical, explicitly a model choice if the source is ambiguous.
    Exact shared XY vertices are welded; interior faces are removed topologically.
    """
    if not math.isfinite(thickness_m) or thickness_m <= 0:
        raise ValueError("positive finite thickness required")
    triangles = plan_triangles(polygon, spacing)
    unique, inverse = np.unique(triangles.reshape(-1, 2), axis=0, return_inverse=True)
    del triangles
    roof = np.asarray(roof_function(unique), dtype=float)
    if roof.shape != (len(unique),) or not np.isfinite(roof).all():
        raise ValueError("one finite roof height per plan vertex required")
    n = len(unique)
    bottom = inverse.reshape(-1, 3)
    # Triangle-major keys preserve the former dictionary's first-encounter order.
    # Packed uint64 keys halve edge storage for practical mesh sizes.
    if n <= 2**32:
        edge_keys = np.empty(bottom.shape, dtype=np.uint64)
        for edge in range(3):
            a, b = bottom[:,edge], bottom[:,(edge+1)%3]
            edge_keys[:,edge] = np.minimum(a,b)
            edge_keys[:,edge] *= n
            edge_keys[:,edge] += np.maximum(a,b).astype(np.uint64)
        keys, first, counts = np.unique(edge_keys, return_index=True, return_counts=True)
        del edge_keys, keys
    else:
        # Preserve the general indexing contract beyond the packed-key range.
        edges = bottom[:,[0,1,1,2,2,0]].reshape(-1,2)
        keys, first, counts = np.unique(np.sort(edges,axis=1), axis=0,
                                       return_index=True, return_counts=True)
        del edges, keys
    if np.any(counts > 2):
        raise ValueError("nonmanifold plan triangulation")
    boundary = np.sort(first[counts == 1])
    del first, counts
    a = bottom[boundary//3, boundary%3]
    b = bottom[boundary//3, (boundary%3+1)%3]
    points = np.vstack([np.column_stack([unique, roof-thickness_m]), np.column_stack([unique, roof])])
    wedges = np.column_stack([bottom, bottom+n])
    nt = len(bottom)
    faces = np.empty((2*nt+2*len(boundary),3), dtype=int)
    faces[:nt] = bottom[:,::-1]
    faces[nt:2*nt] = bottom+n
    faces[2*nt::2] = np.column_stack([a,b,b+n])
    faces[2*nt+1::2] = np.column_stack([a,b+n,a+n])
    return {"points":points, "wedges":wedges, "faces":faces,
            "plan_area_m2":float(polygon.area), "volume_m3":float(polygon.area*thickness_m),
            "thickness_orientation":"VERTICAL_MODEL_CHOICE"}


def room_footprints(polygon, width, pillar_width, azimuth_deg, phase_fraction=0.):
    """Dimensioned scenario stripes clipped to a panel, never actual chambers.

    Azimuth is clockwise from north. Phase has no surveyed chamber tie.
    """
    if min(width, pillar_width) <= 0 or not all(math.isfinite(x) for x in (width,pillar_width,azimuth_deg,phase_fraction)):
        raise ValueError("positive finite room and pillar dimensions required")
    center = polygon.centroid.coords[0]
    angle = 90-azimuth_deg
    local = rotate(polygon, -angle, origin=center)
    xmin,ymin,xmax,ymax = local.bounds
    pitch = width+pillar_width
    start = math.floor(ymin/pitch)*pitch + (phase_fraction % 1)*pitch - pitch
    result = []
    for y in np.arange(start, ymax, pitch):
        stripe = local.intersection(box(xmin-1, y, xmax+1, y+width))
        result.extend(rotate(p, angle, origin=center) for p in polygon_parts(stripe))
    return result


def idw_values(xy, supports, power=2.):
    """Deterministic scalar interpolation; extrapolation is the caller's choice.

    Supports are (x,y,value). Repeated XY with conflicting values are rejected.
    """
    xy=np.asarray(xy,dtype=float); supports=np.asarray(supports,dtype=float)
    if supports.ndim!=2 or supports.shape[1]!=3 or not len(supports) or power<=0:
        raise ValueError("finite nonempty x,y,value supports and positive power required")
    if not np.isfinite(xy).all() or not np.isfinite(supports).all():
        raise ValueError("finite interpolation inputs required")
    unique={}
    for x,y,z in supports:
        if (x,y) in unique and unique[(x,y)]!=z:
            raise ValueError("conflicting values at repeated support XY")
        unique[(x,y)]=z
    supports=np.array([[x,y,z] for (x,y),z in sorted(unique.items())])
    distance=np.linalg.norm(xy[:,None,:]-supports[None,:,:2],axis=2)
    nearest=np.argmin(distance,axis=1)
    weights=np.maximum(distance,1e-12)**(-power)
    result=weights@supports[:,2]/weights.sum(axis=1)
    exact=distance[np.arange(len(xy)),nearest]<1e-9
    result[exact]=supports[nearest[exact],2]
    return result


def write_vtu(path, mesh, material_id=0):
    """ASCII VTK wedge mesh with integer MaterialIDs, no solver execution."""
    points = mesh["points"]
    # OGS PrismRule expects clockwise lower face. Verified against checkMesh -v;
    # keep mathematical exterior winding independent of the volume adapter.
    cells = mesh["wedges"][:,[0,2,1,3,5,4]]
    values = lambda array: " ".join(format(float(v), ".17g") for v in np.asarray(array).flat)
    ints = lambda array: " ".join(str(int(v)) for v in np.asarray(array).flat)
    xml = ('<?xml version="1.0"?>\n<VTKFile type="UnstructuredGrid" version="0.1" byte_order="LittleEndian">'
           f'<UnstructuredGrid><Piece NumberOfPoints="{len(points)}" NumberOfCells="{len(cells)}">'
           f'<Points><DataArray type="Float64" NumberOfComponents="3" format="ascii">{values(points)}</DataArray></Points>'
           f'<Cells><DataArray type="Int64" Name="connectivity" format="ascii">{ints(cells)}</DataArray>'
           f'<DataArray type="Int64" Name="offsets" format="ascii">{ints(np.arange(1,len(cells)+1)*6)}</DataArray>'
           f'<DataArray type="UInt8" Name="types" format="ascii">{ints(np.full(len(cells),13))}</DataArray></Cells>'
           f'<CellData Scalars="MaterialIDs"><DataArray type="Int32" Name="MaterialIDs" format="ascii">'
           f'{ints(np.full(len(cells),material_id))}</DataArray></CellData></Piece></UnstructuredGrid></VTKFile>\n')
    Path(path).write_text(xml, encoding="utf-8")


def write_vtm(path, entries):
    lines = ['<?xml version="1.0"?>','<VTKFile type="vtkMultiBlockDataSet" version="1.0" byte_order="LittleEndian">','<vtkMultiBlockDataSet>']
    for index,(name,filename) in enumerate(entries):
        lines.append(f'<DataSet index="{index}" name="{escape(name, {chr(34): "&quot;"})}" file="{escape(filename, {chr(34): "&quot;"})}"/>')
    Path(path).write_text("\n".join(lines+['</vtkMultiBlockDataSet>','</VTKFile>','']),encoding="utf-8")
