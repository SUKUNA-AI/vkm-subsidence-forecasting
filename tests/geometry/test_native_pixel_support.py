"""Synthetic native-pixel coverage and finite RDP; no source-document fixtures."""
import copy
from fractions import Fraction
import itertools
import json
import math
import random
import unittest

from vkm_geometry.native_pixel_support import (
    _squared_distance_to_chord,
    simplify_supported_path,
    validate_polyline,
)

SHA = 'a3' * 32


def frame(width=12, height=12):
    return {
        'id': 'synthetic-pixels', 'width': width, 'height': height,
        'origin': 'top-left image edge', 'x_axis': 'right', 'y_axis': 'down',
        'unit': 'pixel', 'crs': None, 'registration_status': 'NOT_GEOREFERENCED',
        'pixel_centres': '(column+0.5,row+0.5)',
    }


def arguments():
    return dict(frame=frame(), expected_frame=frame(),
                source_sha256=SHA, expected_source_sha256=SHA)


def reference_support(a, b, cells):
    # Partition at *all integer grid crossings*, then check endpoints and one
    # point inside each open interval. Independent of cell clipping/interval union.
    a, b = tuple(map(Fraction, a)), tuple(map(Fraction, b))
    cuts = {Fraction(0), Fraction(1)}
    for dim in (0, 1):
        delta = b[dim] - a[dim]
        if delta:
            for boundary in range(math.floor(min(a[dim], b[dim])),
                                  math.ceil(max(a[dim], b[dim])) + 1):
                t = (boundary - a[dim]) / delta
                if 0 <= t <= 1:
                    cuts.add(t)
    cuts = sorted(cuts)
    probes = cuts + [(s + t) / 2 for s, t in zip(cuts, cuts[1:])]
    for t in probes:
        p = [x + t * (y - x) for x, y in zip(a, b)]
        if not any(x <= p[0] <= x + 1 and y <= p[1] <= y + 1 for x, y in cells):
            return False
    return True


def finite_distance(point, start, end):
    # Quadratic interpolation from q(0),q(1/2),q(1); no projection/clamp formula.
    p, a, b = [tuple(map(Fraction, v)) for v in (point, start, end)]
    def q(t):
        return sum((x - (s + t * (e - s))) ** 2 for x, s, e in zip(p, a, b))
    q0, qh, q1 = q(0), q(Fraction(1, 2)), q(1)
    aa = 2 * (q0 + q1 - 2 * qh)
    bb = q1 - q0 - aa
    candidates = [q0, q1]
    if aa:
        t = -bb / (2 * aa)
        if 0 <= t <= 1:
            candidates.append(q(t))
    return min(candidates)


class TestNativePixelSupport(unittest.TestCase):
    def proof(self, path, cells, **kwargs):
        data = copy.deepcopy((path, cells, kwargs))
        result = validate_polyline(path, cells, **(arguments() | kwargs))
        self.assertEqual((path, cells, kwargs), data)
        return result

    def simplify(self, path, cells, tolerance, **kwargs):
        data = copy.deepcopy((path, cells, kwargs))
        result = simplify_supported_path(path, cells, tolerance=tolerance,
                                         **(arguments() | kwargs))
        self.assertEqual((path, cells, kwargs), data)
        return result

    def test_grid_partition_oracle_including_closed_boundaries(self):
        cells = [(0, 0), (1, 0), (0, 1), (1, 1)]
        points = list(itertools.product([0, .5, 1, 1.5, 2], repeat=2))
        for bits in range(16):
            selected = [c for i, c in enumerate(cells) if bits & (1 << i)]
            for a, b in itertools.combinations_with_replacement(points, 2):
                with self.subTest(mask=bits, a=a, b=b):
                    proof = self.proof([a, b], selected)
                    self.assertEqual(proof['supported'], reference_support(a, b, selected))

    def test_endpoint_only_gap_rejected_and_witness_intervals(self):
        proof = self.proof([[.5, .5], [5.5, .5]], [[0, 0], [5, 0]])
        self.assertFalse(proof['supported'])
        self.assertEqual(proof['uncovered_segment_indices'], [0])
        gap = proof['segments'][0]['uncovered_intervals'][0]
        self.assertEqual(Fraction(**gap['start']), Fraction(1, 10))
        self.assertEqual(Fraction(**gap['end']), Fraction(9, 10))
        self.assertFalse(proof['policy']['endpoint_only'])
        self.assertIsNone(proof['policy']['epsilon'])

    def test_diagonal_single_corner_touch_is_closed_not_dilated(self):
        cells = [[0, 0], [1, 1]]
        self.assertTrue(self.proof([[.5, .5], [1.5, 1.5]], cells)['supported'])
        self.assertFalse(self.proof([[.5, .5], [1.5, math.nextafter(1.5, math.inf)]], cells)['supported'])

    def test_point_membership_duplicate_vertices_and_empty_support(self):
        for path in ([[.5, .5]], [[.5, .5], [.5, .5]]):
            self.assertTrue(self.proof(path, [[0, 0]])['supported'])
            self.assertFalse(self.proof(path, [])['supported'])
        self.assertFalse(self.proof([[1.1, .5]], [[0, 0]])['supported'])

    def test_explicit_closing_edge_must_be_checked(self):
        cells = [[x, 0] for x in range(4)] + [[3, y] for y in range(1, 4)]
        path = [[.5, .5], [3.5, .5], [3.5, 3.5]]
        self.assertTrue(self.proof(path, cells)['supported'])
        closed = self.proof(path + [path[0]], cells)
        self.assertFalse(closed['supported'])
        self.assertEqual(closed['uncovered_segment_indices'], [2])

    def test_blocked_cells_are_not_removed_from_source_observation(self):
        cells = [[x, 0] for x in range(5)]
        result = self.proof([[.5, .5], [4.5, .5]], cells, blocked_cells=[[2, 0]])
        self.assertFalse(result['supported'])
        self.assertEqual(result['observed_support_cells'], cells)
        self.assertEqual(result['blocked_cells'], [[2, 0]])
        self.assertEqual(result['effective_support_cell_count'], 4)

    def test_source_order_invariance_and_json_roundtrip(self):
        cells = [[x, 0] for x in range(5)]
        a = self.proof([[.5, .5], [4.5, .5]], cells)
        b = self.proof([[.5, .5], [4.5, .5]], cells[::-1])
        self.assertEqual(a, b)
        self.assertEqual(a, json.loads(json.dumps(a, allow_nan=False)))
        self.assertEqual(a['source_sha256'], SHA)
        self.assertEqual(a['frame'], frame())

    def test_unknown_frame_metadata_retained_but_no_geodetic_claim_created(self):
        f = frame() | {'datum': 'UNKNOWN', 'interpretation': None}
        result = self.proof([[.5, .5]], [[0, 0]], frame=f, expected_frame=copy.deepcopy(f))
        self.assertEqual(result['frame'], f)
        self.assertIsNone(result['frame']['crs'])
        self.assertNotIn('scientifically_validated', result)

    def test_finite_distance_quadratic_reference_and_metamorphic(self):
        points = list(itertools.product(range(-1, 2), repeat=2))
        for p, a, b in itertools.product(points, repeat=3):
            p, a, b = [tuple(map(Fraction, q)) for q in (p, a, b)]
            expected = finite_distance(p, a, b)
            self.assertEqual(_squared_distance_to_chord(p, a, b), expected)
            self.assertEqual(_squared_distance_to_chord(p, b, a), expected)
            rotate = lambda q: (-q[1], q[0])
            self.assertEqual(_squared_distance_to_chord(rotate(p), rotate(a), rotate(b)), expected)
            scale = lambda q: tuple(Fraction(3, 2) * v + 7 for v in q)
            self.assertEqual(_squared_distance_to_chord(scale(p), scale(a), scale(b)), expected * Fraction(9, 4))

    def test_backtracking_cannot_hide_overshoot_past_finite_chord(self):
        path = [[.5, .5], [4.5, .5], [2.5, .5]]
        result = self.simplify(path, [[x, 0] for x in range(5)], .1)
        self.assertEqual(result['path'], path)
        self.assertEqual(result['original_vertex_indices'], [0, 1, 2])
        self.assertTrue(result['supported'])

    def test_ulp_below_at_above_tolerance(self):
        path = [[.5, .5], [2.75, .5], [2.5, .5]]
        cells = [[x, 0] for x in range(4)]
        for tol in (math.nextafter(.25, 0), .25, math.nextafter(.25, math.inf)):
            result = self.simplify(path, cells, tol)
            self.assertEqual(len(result['path']), 3 if tol < .25 else 2)
            indices = result['original_vertex_indices']
            for a, b in zip(indices, indices[1:]):
                for p in path[a:b + 1]:
                    self.assertLessEqual(finite_distance(p, path[a], path[b]), Fraction(tol) ** 2)

    def test_supported_simplification_preserves_source_vertices(self):
        path = [[.5, .5], [1.5, .5], [2.5, .5], [3.5, .5]]
        result = self.simplify(path, [[x, 0] for x in range(4)], .1)
        self.assertEqual(result['path'], [path[0], path[-1]])
        self.assertEqual(result['original_vertex_indices'], [0, 3])
        self.assertFalse(result['new_coordinates_inserted'])
        self.assertFalse(result['tolerance_expands_support_corridor'])

    def test_rdp_shortcut_cannot_cross_white_space_even_at_large_tolerance(self):
        cells = [[x, 0] for x in range(5)] + [[4, y] for y in range(1, 5)]
        path = [[.5, .5], [4.5, .5], [4.5, 4.5]]
        result = self.simplify(path, cells, 100)
        self.assertEqual(result['path'], path)
        self.assertTrue(result['supported'])
        self.assertIn('SPLIT_UNSUPPORTED_CHORD', [r['decision'] for r in result['rdp_checks']])

    def test_unsupported_original_not_repaired_by_simplification(self):
        path = [[.5, .5], [5.5, .5]]
        for cells, blocked in (([[0, 0], [5, 0]], ()), ([], ())):
            result = self.simplify(path, cells, 100, blocked_cells=blocked)
            self.assertFalse(result['supported'])
            self.assertFalse(result['simplification_performed'])
            self.assertEqual(result['path'], path)

    def test_closed_paths_preserved_with_explicit_status(self):
        cells = [[x, y] for x in range(3) for y in range(3)]
        path = [[.5, .5], [2.5, .5], [2.5, 2.5], [.5, .5]]
        result = self.simplify(path, cells, 10)
        self.assertEqual(result['status'], 'REJECTED_CLOSED_LOOP_TOPOLOGY_PRESERVED')
        self.assertEqual(result['path'], path)
        self.assertFalse(result['simplification_performed'])

    def test_zero_tolerance_preserves_all_and_short_paths(self):
        for path in ([[.5, .5]], [[.5, .5], [1.5, .5]],
                     [[.5, .5], [1.5, .5], [2.5, .5]]):
            result = self.simplify(path, [[x, 0] for x in range(3)], 0)
            self.assertEqual(result['path'], path)
            self.assertEqual(result['status'], 'DISABLED_ZERO_TOLERANCE_ORIGINAL_VERTICES_EXACT')
            self.assertFalse(result['simplification_performed'])
        result = self.simplify([[.5, .5]], [[0, 0]], 1)
        self.assertEqual(result['status'], 'PATH_TOO_SHORT_FOR_RDP')

    def test_generated_rdp_error_bound_per_original_subchain(self):
        rng = random.Random(113)
        cells = [[x, y] for x in range(6) for y in range(6)]
        for _ in range(32):
            path = [[rng.randrange(6) + .5, rng.randrange(6) + .5] for _ in range(8)]
            path[-1] = [5.5, 5.5] if path[0] != [5.5, 5.5] else [.5, .5]
            result = self.simplify(path, cells, 1.25)
            indices = result['original_vertex_indices']
            self.assertEqual(result['path'], [path[i] for i in indices])
            self.assertEqual(indices, sorted(set(indices)))
            self.assertEqual(indices[0], 0)
            self.assertEqual(indices[-1], len(path) - 1)
            self.assertTrue(result['support_proof']['supported'])
            for a, b in zip(indices, indices[1:]):
                for point in path[a:b + 1]:
                    self.assertLessEqual(finite_distance(point, path[a], path[b]), Fraction(1.25) ** 2)

    def assert_invalid(self, path=None, cells=None, **kwargs):
        path = [[.5, .5]] if path is None else path
        cells = [[0, 0]] if cells is None else cells
        data = copy.deepcopy((path, cells, kwargs))
        with self.assertRaises(ValueError):
            validate_polyline(path, cells, **(arguments() | kwargs))
        # NaN != NaN, so compare stable representations for invalid payloads.
        self.assertEqual(repr((path, cells, kwargs)), repr(data))

    def test_frame_pins_units_orientation_registration_and_dimensions_fail_closed(self):
        changes = [('id', 'different'), ('unit', 'm'), ('crs', 'EPSG:32640'),
                   ('registration_status', 'GEODETICALLY_REGISTERED'),
                   ('x_axis', 'left'), ('y_axis', 'up'), ('origin', 'centre'),
                   ('width', 0), ('height', True), ('width', 2**31)]
        for key, value in changes:
            with self.subTest(key=key):
                f = frame() | {key: value}
                self.assert_invalid(frame=f)
        f = frame()
        del f['pixel_centres']
        self.assert_invalid(frame=f)
        for key in ('epsg', 'EPSG_CODE', 'meter-per-pixel', 'metric_calibration'):
            self.assert_invalid(frame=frame() | {key: None})
        # Expected metadata uses exact canonical JSON, not Python bool/int equality.
        self.assert_invalid(expected_frame=frame() | {'width': 12.0})

    def test_source_hash_guards_and_no_hash_case_normalisation(self):
        for bad in ('0' * 63, 'g' * 64, None, True):
            self.assert_invalid(source_sha256=bad)
        self.assert_invalid(expected_source_sha256='b4' * 32)
        self.assert_invalid(source_sha256=SHA.upper())
        result = self.proof([[.5, .5]], [[0, 0]], source_sha256=SHA.upper(), expected_source_sha256=SHA.upper())
        self.assertEqual(result['source_sha256'], SHA.upper())

    def test_metric_spelling_variants_rejected_even_with_matching_frame_pin(self):
        for key in ('metres_per_pixel', 'metres-per-pixel', 'METRES-PER-PIXEL',
                    'meters_per_pixel', 'meter-per-pixel', 'metre_per_pixel'):
            f = frame() | {key: 1000.0}
            pins = dict(frame=f, expected_frame=copy.deepcopy(f))
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'EPSG_OR_METRIC_CALIBRATION_NOT_ADMITTED'):
                    self.proof([[.5, .5]], [[0, 0]], **pins)
                with self.assertRaisesRegex(ValueError, 'EPSG_OR_METRIC_CALIBRATION_NOT_ADMITTED'):
                    self.simplify([[.5, .5]], [[0, 0]], .1, **pins)

    def test_bad_paths_rejected(self):
        for bad in ([], 'path', [[True, 0]], [[float('nan'), 0]],
                    [[float('inf'), 0]], [[0]], [[0, 0, 0]], [[None, 0]],
                    [[Fraction(1, 2), 0]], [[type('CustomInt', (int,), {})(1), 0]],
                    [[type('CustomFloat', (float,), {})(.5), 0]]):
            self.assert_invalid(path=bad)

    def test_bad_cells_and_blocked_masks_rejected(self):
        for bad in ('mask', [[0, 0], [0, 0]], [[True, 0]], [[.0, 0]],
                    [[-1, 0]], [[12, 0]], [[0]], [[0, 0, 0]]):
            self.assert_invalid(cells=bad)
        self.assert_invalid(blocked_cells=[[1, 1]])
        self.assert_invalid(blocked_cells=[[0, 0], [0, 0]])
        self.assert_invalid(cells=[[0, 0]] * 100_001)

    def test_bad_tolerances_rejected(self):
        for bad in (-1, True, None, float('nan'), float('inf'), '.1'):
            with self.assertRaises(ValueError):
                self.simplify([[.5, .5]], [[0, 0]], bad)

    def test_finite_builtin_integer_above_float_range(self):
        # A finite integer outside the frame is unsupported, not an overflow.
        huge = 10**400
        self.assertFalse(self.proof([[huge, 0]], [[0, 0]])['supported'])
        self.assertFalse(self.proof([[0, 0], [huge, 0]], [[0, 0]])['supported'])
        result = self.simplify([[.5, .5]], [[0, 0]], huge)
        self.assertTrue(result['supported'])
        self.assertEqual(result['path'], [[.5, .5]])


if __name__ == '__main__':
    unittest.main()
