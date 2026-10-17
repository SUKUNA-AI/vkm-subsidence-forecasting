import numpy as np
import pytest

from vkm_world.geometry_build.column_display import TINSurface, column_translation, translate_contacts


def test_closure_preserves_vertical_intervals_but_not_native_roof_shape():
    contacts = np.array([[-170., -140., -180.], [-90., -70., -100.], [40., 90., 60.]])
    surface = np.array([110., 115., 104.])
    delta = column_translation(contacts[-1], surface)
    translated = translate_contacts(contacts, delta)
    np.testing.assert_allclose(translated[-1], surface)
    np.testing.assert_allclose(np.diff(translated, axis=0), np.diff(contacts, axis=0))
    np.testing.assert_allclose(translated-delta, contacts)
    assert not np.allclose(np.diff(translated[0]), np.diff(contacts[0]))


def test_sampler_respects_a_supplied_diagonal_and_never_extrapolates():
    # The two valid diagonals produce different centre heights in this saddle.
    pytest.importorskip("matplotlib")
    points = np.array([[0., 0., 0.], [2., 0., 0.], [2., 2., 4.], [0., 2., 0.]])
    a = TINSurface(points, [[0, 1, 2], [0, 2, 3]])
    b = TINSurface(points, [[0, 1, 3], [1, 2, 3]])
    assert a.sample([[1., 1.]])[0] == 2.
    assert b.sample([[1., 1.]])[0] == 0.
    assert np.isnan(a.sample([[3., 1.]])[0])
    assert a.sample([[2.000001, 1.]])[0] == 2.


def test_rejects_reversed_contacts_and_inconsistent_shapes():
    with pytest.raises(ValueError):
        translate_contacts([[0., 0.], [-1., 2.]], [1., 1.])
    with pytest.raises(ValueError):
        column_translation([1., 2.], [1.])
    with pytest.raises(ValueError):
        translate_contacts([[0.], [1.]], [np.nan])
