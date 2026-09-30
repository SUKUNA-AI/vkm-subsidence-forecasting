"""Planar similarity/affine fits with rank checks, residuals and leave-one-out."""
from __future__ import annotations
import hashlib
import json
import numpy as np
from vkm_qgis.errors import ToolFailure

def _points(value, label):
    try:
        out = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ToolFailure('INVALID_GCP', f'{label} must be numeric pairs') from exc
    if out.ndim != 2 or out.shape[1] != 2 or not np.isfinite(out).all():
        raise ToolFailure('INVALID_GCP', f'{label} must be finite 2D pairs')
    return out

def apply_points(points, matrix):
    p = _points(points, 'points')
    m = np.asarray(matrix, dtype=float)
    if m.shape != (3, 3) or not np.isfinite(m).all() or not np.allclose(m[2], [0, 0, 1]):
        raise ToolFailure('INVALID_TRANSFORM', 'Only finite planar affine matrices are supported')
    _nondegenerate(m[:2,:2])
    return (np.column_stack([p, np.ones(len(p))]) @ m.T)[:, :2].tolist()

def _nondegenerate(linear):
    values=np.linalg.svd(linear,compute_uv=False)
    if values[-1]<=64*np.finfo(float).eps*max(1.0,values[0]) or values[-1]/values[0]<=1e-12:
        raise ToolFailure('DEGENERATE_TRANSFORM','Linear transform is singular or too close to collapse')

def _solve(src, dst, model):
    minimum = {'similarity': 2, 'affine': 3}.get(model)
    if minimum is None:
        raise ToolFailure('UNSUPPORTED_TRANSFORM', 'Only similarity and affine are supported')
    if len(src) < minimum:
        raise ToolFailure('INSUFFICIENT_GCP', f'{model} needs at least {minimum} control points')
    center = src.mean(axis=0)
    scale = float(np.sqrt(np.mean(np.sum((src-center)**2, axis=1))))
    if scale <= np.finfo(float).eps:
        raise ToolFailure('DEGENERATE_GCP', 'Coincident source control points')
    s = (src-center)/scale
    if model == 'affine':
        design = np.column_stack([s, np.ones(len(s))])
        coeff, _, rank, singular = np.linalg.lstsq(design, dst, rcond=None)
        if rank < 3:
            raise ToolFailure('DEGENERATE_GCP', 'Affine control points are collinear')
        linear = coeff[:2].T / scale
        offset = coeff[2] - linear @ center
    else:
        design = np.zeros((len(src)*2, 4))
        design[::2] = np.column_stack([s[:,0], -s[:,1], np.ones(len(s)), np.zeros(len(s))])
        design[1::2] = np.column_stack([s[:,1], s[:,0], np.zeros(len(s)), np.ones(len(s))])
        coeff, _, rank, singular = np.linalg.lstsq(design, dst.reshape(-1), rcond=None)
        if rank < 4:
            raise ToolFailure('DEGENERATE_GCP', 'Similarity control points are rank deficient')
        a,b,tx,ty = coeff
        linear = np.asarray([[a,-b],[b,a]])/scale
        offset = np.asarray([tx,ty]) - linear @ center
    matrix = np.eye(3)
    _nondegenerate(linear)
    matrix[:2,:2], matrix[:2,2] = linear, offset
    condition = float(singular[0]/singular[-1])
    return matrix, condition

def fit_transform(source, target, model='similarity', ids=None, *, source_unit='unknown',target_unit='unknown',target_unit_verified=False):
    if not isinstance(source_unit,str) or not isinstance(target_unit,str) or not isinstance(target_unit_verified,bool):
        raise ToolFailure('INVALID_UNITS','Units are explicit strings and verification is a boolean')
    src, dst = _points(source, 'source'), _points(target, 'target')
    if len(src) != len(dst) or len(src) > 2000:
        raise ToolFailure('INVALID_GCP', 'Source/target lengths differ or exceed 2000')
    if ids is None:
        ids = [f'GCP-{i+1}' for i in range(len(src))]
    if len(ids) != len(src) or len(set(ids)) != len(ids):
        raise ToolFailure('INVALID_GCP', 'GCP IDs must be unique and match the points')
    matrix, condition = _solve(src, dst, model)
    predicted = np.asarray(apply_points(src, matrix))
    delta = predicted-dst
    distances = np.linalg.norm(delta, axis=1)
    loo=[]
    for i, identifier in enumerate(ids):
        mask=np.arange(len(src)) != i
        try:
            m, _ = _solve(src[mask], dst[mask], model)
            point=np.asarray(apply_points(src[i:i+1],m))[0]
            error=point-dst[i]
            loo.append({'id':identifier,'status':'PASS','residual':error.tolist(),'distance':float(np.linalg.norm(error))})
        except ToolFailure as exc:
            loo.append({'id':identifier,'status':'NOT_RUN','reason':exc.code,'distance':None})
    valid=[x['distance'] for x in loo if x['distance'] is not None]
    inputs={'model':model,'source':src.tolist(),'target':dst.tolist(),'ids':ids,'source_unit':source_unit,
            'target_unit':target_unit,'target_unit_verified':bool(target_unit_verified)}
    signature=hashlib.sha256(json.dumps(inputs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'schema':'vkm.qgis_transform/1','transform_id':'TRF-'+signature[:16],'model':model,'matrix':matrix.tolist(),
            'inputs':inputs,'input_sha256':signature,'rms':float(np.sqrt(np.mean(distances**2))),
            'max_residual':float(distances.max()),'condition_normalized':condition,
            'residuals':[{'id':identifier,'residual':d.tolist(),'distance':float(r)} for identifier,d,r in zip(ids,delta,distances)],
            'leave_one_out':{'status':'PASS' if len(valid)==len(src) else 'NOT_RUN','results':loo,
                             'rms':float(np.sqrt(np.mean(np.asarray(valid)**2))) if valid else None},
            'source_extent':[src.min(axis=0).tolist(),src.max(axis=0).tolist()],
            'target_extent':[dst.min(axis=0).tolist(),dst.max(axis=0).tolist()],
            'epistemic_status':'DERIVATION','model_choice':model,'verification':'AUTO_EXTRACTED_UNREVIEWED',
            'source_unit':source_unit,'target_unit':target_unit,'target_unit_verified':bool(target_unit_verified),
            'crs_status':'LOCAL_ENGINEERING_CRS / AUTHORITY_UNKNOWN' if target_unit=='m' and target_unit_verified else 'UNKNOWN_CRS',
            'epsg':None}
