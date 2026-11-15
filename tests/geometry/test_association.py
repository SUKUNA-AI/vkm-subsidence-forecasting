"""Synthetic incidence tests; no document geometries or ownership gold data."""
import copy
from fractions import Fraction
import itertools
import math
import random
import unittest

from vkm_geometry.association import associate


SYNTHETIC_SHA = 'ab' * 32


def payload(segments, *, roi=(-1, -1, 1, 1), complete=True, scalar=None):
    return {
        'frame': {'id': 'synthetic-grid', 'unit': 'pixel',
                  'source_sha': SYNTHETIC_SHA, 'crs': None},
        'complete_pool': complete,
        'labels': [{'id': 'label', 'frame_id': 'synthetic-grid',
                    'source_sha': SYNTHETIC_SHA, 'roi': list(roi), 'scalar': scalar}],
        'fragments': [
            {'id': f'fragment-{i}', 'frame_id': 'synthetic-grid',
             'source_sha': SYNTHETIC_SHA, 'role': 'candidate', 'points': list(points)}
            for i, points in enumerate(segments)
        ],
    }


def orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def on_segment(a, b, p):
    return (orient(a, b, p) == 0
            and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def segments_cross(a, b, c, d):
    # Independent orientation/edge oracle; no parameter clipping or divisions.
    signs = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if signs[0] * signs[1] < 0 and signs[2] * signs[3] < 0:
        return True
    return any((on_segment(a, b, c), on_segment(a, b, d),
                on_segment(c, d, a), on_segment(c, d, b)))


def reference_hit(points, roi):
    points = [tuple(Fraction(x) for x in p) for p in points]
    x0, y0, x1, y1 = map(Fraction, roi)
    inside = lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    edges = list(zip(corners, corners[1:] + corners[:1]))
    return any(a != b and (inside(a) or inside(b)
               or any(segments_cross(a, b, c, d) for c, d in edges))
               for a, b in zip(points, points[1:]))


class TestAssociation(unittest.TestCase):
    def assert_unchanged(self, data):
        before = copy.deepcopy(data)
        result = associate(data)
        self.assertEqual(data, before)
        return result['labels']

    def assert_invalid(self, change):
        data = payload([[(-2, 0), (2, 0)]])
        change(data)
        before = copy.deepcopy(data)
        with self.assertRaises(ValueError):
            associate(data)
        self.assertEqual(data, before)

    def test_lattice_against_independent_edge_oracle(self):
        lattice = list(itertools.product(range(-2, 3), repeat=2))
        for roi in [(-1, -1, 1, 1), (0, 0, 2, 2), (-2, -1, 0, 1)]:
            for a, b in itertools.combinations(lattice, 2):
                with self.subTest(roi=roi, a=a, b=b):
                    labels = self.assert_unchanged(payload([[a, b]], roi=roi))
                    self.assertEqual(bool(labels[0]['candidates']), reference_hit([a, b], roi))

    def test_multisegment_random_against_independent_oracle(self):
        rng = random.Random(618)
        for _ in range(120):
            parts = []
            while len(parts) < 4:
                pts = [tuple(rng.randint(-8, 8) for _ in range(2)) for _ in range(4)]
                if any(a != b for a, b in zip(pts, pts[1:])):
                    parts.append(pts)
            data = payload(parts, roi=(-2, -3, 1, 4))
            expected = [f'fragment-{i}' for i, p in enumerate(parts)
                        if reference_hit(p, data['labels'][0]['roi'])]
            self.assertEqual(self.assert_unchanged(data)[0]['candidates'], expected)

    def test_closed_boundaries_and_bbox_false_positive(self):
        cases = [([(-2, 1), (2, 1)], True),  # edge overlap
                 ([(-2, 0), (0, 2)], True),  # single corner
                 ([(-2, 1), (1, 2)], False),  # bbox overlaps, segment misses
                 ([(0, 0), (0, .5)], True),
                 ([(-2, 2), (2, 2)], False)]
        for points, expected in cases:
            with self.subTest(points=points):
                self.assertEqual(bool(associate(payload([points]))['labels'][0]['candidates']), expected)

    def test_no_implicit_chord_closure_or_gap_join(self):
        # Endpoints straddle the ROI but all actual polyline segments detour above.
        detour = [(-2, 0), (-2, 2), (2, 2), (2, 0)]
        separate = [[(-3, 0), (-2, 0)], [(2, 0), (3, 0)]]
        for parts in [[detour], separate]:
            self.assertEqual(self.assert_unchanged(payload(parts))[0]['status'], 'NO_EVIDENCE')

    def test_all_branches_roles_and_partial_pool(self):
        data = payload([[(-2, 0), (2, 0)], [(0, -2), (0, 2)], [(-2, -2), (2, 2)]])
        row = self.assert_unchanged(data)[0]
        self.assertEqual(row['candidates'], ['fragment-0', 'fragment-1', 'fragment-2'])
        self.assertEqual(row['status'], 'AMBIGUOUS')
        data['fragments'][1]['role'] = 'annotation'
        data['fragments'][2]['role'] = 'unqualified'
        self.assertEqual(self.assert_unchanged(data)[0]['status'], 'CANDIDATE')
        data['complete_pool'] = False
        self.assertEqual(self.assert_unchanged(data)[0]['status'], 'AMBIGUOUS')
        data['fragments'][0]['role'] = 'annotation'
        self.assertEqual(self.assert_unchanged(data)[0]['status'], 'NO_EVIDENCE')

    def test_unknown_and_known_scalars_never_become_height(self):
        for scalar in [None, 0, -3.5, 10 ** 400]:
            for complete in [True, False]:
                row = self.assert_unchanged(payload([[(-2, 0), (2, 0)]], scalar=scalar, complete=complete))[0]
                self.assertEqual(row['label_scalar'], scalar)
                self.assertIsNone(row['height_assignment'])
                self.assertEqual(set(row), {'id', 'status', 'candidates', 'label_scalar',
                                           'height_assignment', 'pool_complete'})

    def test_order_reversal_translation_and_axis_reflection(self):
        data = payload([[(-3, 0), (3, 0)], [(2, 2), (3, 3)]])
        data['labels'].append(dict(data['labels'][0], id='second', roi=[1, 1, 4, 4]))
        original = self.assert_unchanged(data)
        modified = copy.deepcopy(data)
        modified['labels'].reverse()
        modified['fragments'].reverse()
        for fragment in modified['fragments']:
            fragment['points'].reverse()
        self.assertEqual(self.assert_unchanged(modified), original)
        for item in modified['fragments']:
            item['points'] = [(x + 7, -y + 11) for x, y in item['points']]
        for label in modified['labels']:
            x0, y0, x1, y1 = label['roi']
            label['roi'] = [x0 + 7, -y1 + 11, x1 + 7, -y0 + 11]
        self.assertEqual(self.assert_unchanged(modified), original)

    def test_repeated_vertices_and_explicit_closure(self):
        data = payload([[(-2, 0), (-2, 0), (2, 0), (-2, 0)]])
        self.assertEqual(self.assert_unchanged(data)[0]['status'], 'CANDIDATE')

    def test_extreme_finite_integers_and_floats(self):
        for magnitude in [10 ** 400, 10 ** 5000, float.fromhex('0x1.fffffffffffffp+1023')]:
            for offset, expected in [(0, True), (2, False)]:
                data = payload([[(-magnitude, offset), (magnitude, offset)]])
                self.assertEqual(bool(self.assert_unchanged(data)[0]['candidates']), expected)
        tiny = float.fromhex('0x0.0000000000001p-1022')
        self.assertEqual(self.assert_unchanged(payload([[(-tiny, 0), (tiny, 0)]], roi=(-tiny, -tiny, tiny, tiny)))[0]['status'], 'CANDIDATE')

    def test_hash_case_is_allowed_but_exact_consistency_required(self):
        for source_hash in ['AB' * 32, 'aB' * 32]:
            data = payload([[(-2, 0), (2, 0)]])
            data['frame']['source_sha'] = source_hash
            for item in data['labels'] + data['fragments']:
                item['source_sha'] = source_hash
            self.assertEqual(self.assert_unchanged(data)[0]['status'], 'CANDIDATE')
            data['fragments'][0]['source_sha'] = source_hash.swapcase()
            with self.assertRaises(ValueError):
                associate(data)

    def test_nonfinite_bool_dimension_and_zero_only_reject(self):
        for value in [True, float('inf'), float('-inf'), float('nan'), '1', None]:
            with self.subTest(kind=type(value).__name__):
                self.assert_invalid(lambda d: d['fragments'][0].__setitem__('points', [(value, 0), (1, 0)]))
        for points in [[(0, 0)], [(0, 0), (0, 0)], [(0, 0, 0), (1, 0, 0)]]:
            self.assert_invalid(lambda d, p=points: d['fragments'][0].__setitem__('points', p))
        for roi in [[0, 0, 0, 1], [1, 0, -1, 1], [0, 0, math.nan, 1], [0, 0, 1]]:
            self.assert_invalid(lambda d, r=roi: d['labels'][0].__setitem__('roi', r))

    def test_metadata_roles_duplicates_and_schema_fail_closed(self):
        for field, value in [('unit', 'm'), ('crs', 'EPSG:4326'), ('source_sha', 'not-hex'), ('source_sha', 'a' * 63)]:
            self.assert_invalid(lambda d, f=field, v=value: d['frame'].__setitem__(f, v))
        for array in ['labels', 'fragments']:
            self.assert_invalid(lambda d, a=array: d[a][0].__setitem__('frame_id', 'different'))
            self.assert_invalid(lambda d, a=array: d[a][0].__setitem__('source_sha', 'cd' * 32))
            self.assert_invalid(lambda d, a=array: d[a].append(copy.deepcopy(d[a][0])))
            self.assert_invalid(lambda d, a=array: d[a][0].pop('source_sha'))
        self.assert_invalid(lambda d: d['fragments'][0].__setitem__('role', 'confirmed'))
        self.assert_invalid(lambda d: d['fragments'][0].__setitem__('height', 0))
        self.assert_invalid(lambda d: d.__setitem__('complete_pool', 1))
        self.assert_invalid(lambda d: d['labels'][0].__setitem__('scalar', True))
        self.assert_invalid(lambda d: d.__setitem__('labels', tuple(d['labels'])))

    def test_empty_pool_is_only_supplied_absence(self):
        row = self.assert_unchanged(payload([], complete=False))[0]
        self.assertEqual(row['status'], 'NO_EVIDENCE')
        self.assertFalse(row['pool_complete'])
        self.assertEqual(row['candidates'], [])
        data = payload([])
        data['labels'] = []
        self.assertEqual(self.assert_unchanged(data), [])


if __name__ == '__main__':
    unittest.main()
