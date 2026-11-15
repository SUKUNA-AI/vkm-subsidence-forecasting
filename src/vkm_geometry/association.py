"""Conservative observed-segment / label-ROI incidence (engineering candidate).

Returns ALL eligible supplied fragments whose actual non-degenerate segments
intersect a label's closed, axis-aligned ROI. This is candidate incidence only:
no nearest selection, no snapping/gap filling, no height assignment and no
promotion of any fragment to a proved isoline or source ownership.
"""
from fractions import Fraction
import math
import re

ROLES = frozenset({'candidate', 'annotation', 'unqualified'})
ELIGIBLE_ROLES = frozenset({'candidate'})
_SHA = re.compile(r'[0-9a-fA-F]{64}')
_PAYLOAD_KEYS = frozenset({'frame', 'complete_pool', 'labels', 'fragments'})
_FRAME_KEYS = frozenset({'id', 'unit', 'source_sha', 'crs'})
_LABEL_KEYS = frozenset({'id', 'frame_id', 'source_sha', 'roi', 'scalar'})
_FRAGMENT_KEYS = frozenset({'id', 'frame_id', 'source_sha', 'role', 'points'})


def _fail(msg):
    raise ValueError(msg)


def _keys(obj, expected, what):
    if not isinstance(obj, dict):
        _fail(f'{what} must be an object')
    if set(obj) != expected:
        _fail(f'{what} keys must be exactly {sorted(expected)}')


def _ident(value, what):
    if not isinstance(value, str) or not value:
        _fail(f'{what} must be a non-empty string')
    return value


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f'{what} must be a non-bool number')
    if isinstance(value, int):
        # Python ints are always finite; never route them through float().
        return Fraction(value)
    if not math.isfinite(value):
        _fail(f'{what} must be finite')
    return Fraction(value)


def _frame(frame):
    _keys(frame, _FRAME_KEYS, 'frame')
    fid = _ident(frame['id'], 'frame.id')
    if frame['unit'] != 'pixel':
        _fail('frame.unit must be "pixel"')
    sha = frame['source_sha']
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        _fail('frame.source_sha must be 64 hex characters')
    if frame['crs'] is not None:
        _fail('frame.crs must be null for pixel frames')
    return fid, sha


def _same_source(item, fid, sha, what):
    if item['frame_id'] != fid:
        _fail(f'{what}.frame_id does not match frame.id')
    if item['source_sha'] != sha:
        _fail(f'{what}.source_sha does not match frame.source_sha')


def _label(label, fid, sha):
    _keys(label, _LABEL_KEYS, 'label')
    lid = _ident(label['id'], 'label.id')
    _same_source(label, fid, sha, f'label {lid!r}')
    roi = label['roi']
    if not isinstance(roi, (list, tuple)) or len(roi) != 4:
        _fail(f'label {lid!r} roi must be [x0,y0,x1,y1]')
    x0, y0, x1, y1 = (_number(v, f'label {lid!r} roi') for v in roi)
    if not (x0 < x1 and y0 < y1):
        _fail(f'label {lid!r} roi must be ordered with positive area')
    scalar = label['scalar']
    if scalar is not None:
        _number(scalar, f'label {lid!r} scalar')
    return lid, (x0, y0, x1, y1), scalar


def _fragment(fragment, fid, sha):
    _keys(fragment, _FRAGMENT_KEYS, 'fragment')
    gid = _ident(fragment['id'], 'fragment.id')
    _same_source(fragment, fid, sha, f'fragment {gid!r}')
    role = fragment['role']
    if not isinstance(role, str) or role not in ROLES:
        _fail(f'fragment {gid!r} has unsupported role {role!r}')
    points = fragment['points']
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        _fail(f'fragment {gid!r} needs >=2 points')
    pts = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            _fail(f'fragment {gid!r} points must be 2D')
        pts.append((_number(p[0], f'fragment {gid!r} x'), _number(p[1], f'fragment {gid!r} y')))
    segments = [(a, b) for a, b in zip(pts, pts[1:]) if a != b]
    if not segments:
        _fail(f'fragment {gid!r} has no non-zero segment')
    return gid, role, segments


def segment_hits_rect(a, b, rect):
    """Closed segment vs closed axis-aligned rectangle (exact Liang-Barsky)."""
    x0, y0, x1, y1 = rect
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t0, t1 = Fraction(0), Fraction(1)
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return False
            if r < t1:
                t1 = r
    return t0 <= t1


def associate(payload):
    _keys(payload, _PAYLOAD_KEYS, 'payload')
    fid, sha = _frame(payload['frame'])
    complete = payload['complete_pool']
    if not isinstance(complete, bool):
        _fail('complete_pool must be bool')
    labels, fragments = payload['labels'], payload['fragments']
    if not isinstance(labels, list) or not isinstance(fragments, list):
        _fail('labels and fragments must be lists')

    parsed_labels, seen = [], set()
    for label in labels:
        lid, rect, scalar = _label(label, fid, sha)
        if lid in seen:
            _fail(f'duplicate label id {lid!r}')
        seen.add(lid)
        parsed_labels.append((lid, rect, scalar))

    parsed_fragments, seen = [], set()
    for fragment in fragments:
        gid, role, segments = _fragment(fragment, fid, sha)
        if gid in seen:
            _fail(f'duplicate fragment id {gid!r}')
        seen.add(gid)
        parsed_fragments.append((gid, role, segments))

    out = []
    for lid, rect, scalar in sorted(parsed_labels, key=lambda t: t[0]):
        hits = sorted(
            gid for gid, role, segments in parsed_fragments
            if role in ELIGIBLE_ROLES and any(segment_hits_rect(a, b, rect) for a, b in segments)
        )
        if not hits:
            status = 'NO_EVIDENCE'
        elif len(hits) == 1 and complete:
            status = 'CANDIDATE'
        else:
            status = 'AMBIGUOUS'
        out.append({'id': lid, 'status': status, 'candidates': hits, 'label_scalar': scalar,
                    'height_assignment': None, 'pool_complete': complete})
    return {'labels': out}
