from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.amortization import (
    AmortizationEngine,
    AmortizationPolicy,
    AmortizationStatus as S,
    is_wash,
    unlock_amount,
)
from clearing_rail.events import StakeReleased
from clearing_rail.ledger import Ledger
from clearing_rail.types import CreditLimitExceeded, Node, ValidationError
from clearing_rail.vouch import VouchGraph, VouchPolicy
from conftest import T0

AS_OF = T0 + timedelta(days=60)   # FIX-ADJUST: was 40; day-20 legs must be >= 30 d old (matured)


def setup(depth=1, unlock_ratio=Decimal("1.0")):
    l = Ledger()
    for i in ("A", "B", "C", "X", "Y", "Z"):
        l.add_node(Node(i, Decimal("1000")))
    g = VouchGraph(l, VouchPolicy(tree_depth=depth))
    edge = g.vouch("A", "B", T0)
    eng = AmortizationEngine(l, g, AmortizationPolicy(unlock_ratio=unlock_ratio))
    return l, g, edge, eng


def test_vouch_locks_fixed_slice_and_reduces_available_credit():
    l, g, edge, _ = setup()
    assert edge.delta_c == Decimal("100")
    assert l.node("A").locked_vouch_stake == Decimal("100")
    assert l.node("A").available_credit == Decimal("900")
    with pytest.raises(ValidationError):
        g.vouch("A", "B", T0)
    with pytest.raises(ValidationError):
        g.vouch("A", "A", T0)


def test_vouch_cannot_lock_into_existing_debt():
    l = Ledger()
    l.add_node(Node("A", Decimal("100")))
    l.add_node(Node("B", Decimal("100")))
    l.record_trade("A", "B", Decimal("95"), T0)
    with pytest.raises(CreditLimitExceeded):
        VouchGraph(l).vouch("A", "B", T0)


def test_vouch_tree_depth():
    l, g, _, _ = setup()
    g.vouch("X", "A", T0)
    g.vouch("Y", "X", T0)
    assert g.vouch_tree(("A", "B"), 1) == {"A", "B", "X"}
    assert g.vouch_tree(("A", "B"), 2) == {"A", "B", "X", "Y"}
    assert g.vouch_tree(("A", "B"), 0) == {"A", "B"}


@pytest.mark.parametrize("voucher_side", [("C", "A"), ("A", "C"), ("C", "B"), ("B", "C")])
def test_outside_vouch_rejects_counterparty_in_immediate_tree(voucher_side):
    l, g, _, eng = setup()
    g.vouch(*voucher_side, T0)
    l.record_trade("B", "C", Decimal("50"), T0 + timedelta(days=20))
    r = eng.amortize("A", "B", "C", AS_OF)
    assert r.status is S.INSIDE_VOUCH_TREE and r.unlocked == 0


def test_outside_vouch_depth_param():
    l, g, _, _ = setup()
    g.vouch("X", "A", T0)
    g.vouch("C", "X", T0)      # C is 2 hops from A
    l.record_trade("B", "C", Decimal("50"), T0 + timedelta(days=20))
    shallow = AmortizationEngine(l, g, AmortizationPolicy(tree_depth=1))
    deep = AmortizationEngine(l, g, AmortizationPolicy(tree_depth=2))
    assert deep.amortize("A", "B", "C", AS_OF).status is S.INSIDE_VOUCH_TREE
    assert shallow.amortize("A", "B", "C", AS_OF).status is S.UNLOCKED


def test_counterparty_cannot_be_voucher_or_vouchee():
    _, _, _, eng = setup()
    assert eng.amortize("A", "B", "A", AS_OF).status is S.INSIDE_VOUCH_TREE


def test_wash_filter_exactly_at_033_passes():
    l, g, edge, eng = setup()
    l.record_trade("B", "C", Decimal("100"), T0 + timedelta(days=20))
    l.record_trade("C", "B", Decimal("33"), T0 + timedelta(days=21))
    r = eng.amortize("A", "B", "C", AS_OF)
    assert r.status is S.UNLOCKED
    assert r.net_transfer == Decimal("67")
    assert r.unlocked == Decimal("67")
    assert edge.remaining == Decimal("33")
    assert l.node("A").locked_vouch_stake == Decimal("33")


def test_wash_filter_just_above_033_disqualifies_entirely():
    l, g, edge, eng = setup()
    l.record_trade("B", "C", Decimal("100"), T0 + timedelta(days=20))
    l.record_trade("C", "B", Decimal("33.01"), T0 + timedelta(days=21))
    r = eng.amortize("A", "B", "C", AS_OF)
    assert r.status is S.WASH_DISQUALIFIED and r.unlocked == 0
    assert edge.remaining == Decimal("100")


def test_pure_rules():
    assert is_wash(Decimal(0), Decimal("0.01"))
    assert not is_wash(Decimal(0), Decimal(0))
    assert unlock_amount(Decimal(0), Decimal(0), Decimal(100)) == (S.NO_NET_TRANSFER, 0)
    assert unlock_amount(Decimal(0), Decimal(1), Decimal(100)) == (S.WASH_DISQUALIFIED, 0)
    assert unlock_amount(Decimal(500), Decimal(0), Decimal(100)) == (S.UNLOCKED, Decimal(100))
    assert unlock_amount(Decimal(50), Decimal(0), Decimal(0)) == (S.EDGE_EXHAUSTED, 0)


def test_no_volume_and_inbound_only():
    l, _, _, eng = setup()
    assert eng.amortize("A", "B", "C", AS_OF).status is S.NO_NET_TRANSFER
    l.record_trade("C", "B", Decimal("5"), T0 + timedelta(days=20))
    assert eng.amortize("A", "B", "C", AS_OF).status is S.WASH_DISQUALIFIED


def test_unlock_capped_at_remaining_and_ratio():
    l, _, edge, eng = setup(unlock_ratio=Decimal("0.5"))
    l.record_trade("B", "C", Decimal("60"), T0 + timedelta(days=20))
    r = eng.amortize("A", "B", "C", AS_OF)
    assert r.unlocked == Decimal("30.0")
    l.record_trade("B", "C", Decimal("900"), T0 + timedelta(days=21))
    r = eng.amortize("A", "B", "C", AS_OF)
    assert r.unlocked == Decimal("70.0") and edge.remaining == 0
    assert eng.amortize("A", "B", "C", AS_OF).status is S.EDGE_EXHAUSTED


def test_same_window_volume_cannot_amortize_twice():
    l, _, edge, eng = setup()
    l.record_trade("B", "C", Decimal("40"), T0 + timedelta(days=20))
    assert eng.amortize("A", "B", "C", AS_OF).unlocked == Decimal("40")
    again = eng.amortize("A", "B", "C", AS_OF)
    assert again.status is S.NO_NET_TRANSFER and again.unlocked == 0
    later = AS_OF + timedelta(days=1)
    l.record_trade("B", "C", Decimal("10"), later)
    # FIX-ADJUST: was amortize(..., later); a leg now unlocks only once matured (30 d)
    assert eng.amortize("A", "B", "C", later + timedelta(days=30)).unlocked == Decimal("10")
    assert edge.released == Decimal("50")


def test_wash_filter_uses_full_window_including_consumed_volume():
    # FIX-ADJUST: same trade timeline; amortize calls moved 30 d later (maturity) and
    # the unlock on day 33 is now min(eligible 40, aggregate net 110) = 40 -> 4.0
    # (was the pair-only fresh net 10 -> 1.0).
    l, _, _, eng = setup(unlock_ratio=Decimal("0.1"))
    d = lambda n: T0 + timedelta(days=n)
    l.record_trade("B", "C", Decimal("100"), d(1))
    assert eng.amortize("A", "B", "C", d(31)).unlocked == Decimal("10.0")
    l.record_trade("C", "B", Decimal("30"), d(2))
    assert eng.amortize("A", "B", "C", d(32)).status is S.NO_NET_TRANSFER
    l.record_trade("B", "C", Decimal("40"), d(3))
    r = eng.amortize("A", "B", "C", d(33))
    assert r.status is S.UNLOCKED and r.unlocked == Decimal("4.0")
    # day-1 outbound rolls out of the span; consumed day-3 outbound and inbound 30 do not
    l.record_trade("B", "C", Decimal("10"), d(31))
    r = eng.amortize("A", "B", "C", d(61) + timedelta(hours=12))
    # eligible slice alone (out 10 / in 0) would pass; span (out 50 / in 30) is a wash
    assert r.status is S.WASH_DISQUALIFIED and r.unlocked == 0


def test_volume_outside_30_day_window_ignored():
    l, _, _, eng = setup()
    # FIX-ADJUST: was 31 d; matured legs are eligible for (as_of - 60 d, as_of - 30 d]
    l.record_trade("B", "C", Decimal("40"), AS_OF - timedelta(days=61))
    assert eng.amortize("A", "B", "C", AS_OF).status is S.NO_NET_TRANSFER


def test_release_event_marks_outside_volume_path():
    l, g, edge, eng = setup()
    l.record_trade("B", "C", Decimal("10"), T0 + timedelta(days=20))
    eng.amortize("A", "B", "C", AS_OF)
    g.release(edge, Decimal("5"), AS_OF, via_outside_volume=False, reason="governance")
    evs = l.events.of_type(StakeReleased)
    assert [(e.amount, e.via_outside_volume, e.counterparty) for e in evs] == [
        (Decimal("10"), True, "C"), (Decimal("5"), False, None)]
