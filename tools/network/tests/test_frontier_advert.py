"""The connector's frontier advert decision (auto-xs9hz): an unchanged map
sends nothing, a value never regresses, and a fresh persona is sent."""
from tools.network.fleet_relay_sync import frontier_advert

P1, P2 = "11" * 32, "22" * 32


def test_unchanged_frontier_sends_nothing():
    assert frontier_advert({P1: 10}, {P1: 10}) is None
    assert frontier_advert({P1: 10}, {}) is None


def test_a_rise_or_a_new_persona_sends_the_merged_map():
    assert frontier_advert({P1: 10}, {P1: 12}) == {P1: 12}
    assert frontier_advert({P1: 10}, {P2: 3}) == {P1: 10, P2: 3}


def test_a_value_never_regresses():
    assert frontier_advert({P1: 10, P2: 5}, {P1: 4, P2: 5}) is None
    assert frontier_advert({P1: 10}, {P1: 4, P2: 1}) == {P1: 10, P2: 1}
