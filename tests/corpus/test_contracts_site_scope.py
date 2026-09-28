"""Source scope table (CP-07, audit A §2.4): all 18 raw register values are mapped; AMBIGUOUS is not resolved."""
from __future__ import annotations

import pytest

from vkm_corpus.contracts.site_scope import SITE_SCOPE_TABLE, UnknownScopeError, map_site_scope
from vkm_world.core.provenance import Scope

# the 18 raw values of the register with their counts (audit A §2.4); the sum is 251
REGISTER_VALUES = {
    "GENERAL_METHOD": 79, "VKM_REGIONAL": 76, "OTHER_VKM_SITE": 33, "NON_VKM_ANALOG": 30, "SKRU1": 9,
    "METHOD_GENERAL": 4, "VKM_Berezniki": 3, "VKM_REGIONAL_and_SKRU1": 3, "OTHER_POTASH_SITE": 2,
    "SKRU1_SKRU2_SKRU3": 2, "VKM_Uralkali": 2, "SKRU1_SKRU2_PILLAR": 2, "VKM_regional": 1, "VKM_Solikamsk": 1,
    "other_potash_deposit": 1, "BKPRU4_VKM": 1, "LEGACY_RETIRED": 1, "SALT_DEPOSITS_USSR_incl_VKM": 1,
}


def test_all_18_register_values_are_mapped():
    assert len(REGISTER_VALUES) == 18 and sum(REGISTER_VALUES.values()) == 251
    assert set(SITE_SCOPE_TABLE) == set(REGISTER_VALUES)


def test_mapped_values_are_vkm_world_scopes():
    scopes = {s.value for s in Scope}
    for m in SITE_SCOPE_TABLE.values():
        assert set(m.scopes) <= scopes and set(m.candidates) <= scopes


def test_ambiguous_and_not_a_scope_have_no_true_scope():
    for raw in ("SKRU1_SKRU2_SKRU3", "VKM_Solikamsk"):
        m = map_site_scope(raw)
        assert m.mapping == "AMBIGUOUS" and m.scopes == () and m.candidates
    assert map_site_scope("LEGACY_RETIRED").scopes == ()
    assert map_site_scope("NON_VKM_ANALOG").scopes == ("NON_VKM",)          # the label is not ANALOGUE
    assert map_site_scope("VKM_REGIONAL_and_SKRU1").mapping == "MULTI"


def test_unknown_value_is_an_error():
    with pytest.raises(UnknownScopeError):
        map_site_scope("SKRU-1")
