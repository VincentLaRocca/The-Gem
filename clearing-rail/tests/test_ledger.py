import random
from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.ledger import Ledger
from clearing_rail.types import CreditLimitExceeded, D, Hop, Initiator, LedgerError, Node, ValidationError
from conftest import T0


def mk(*ids, ceiling="100"):
    l = Ledger()
    for i in ids:
        l.add_node(Node(i, Decimal(ceiling)))
    return l


def test_floats_refused():
    with pytest.raises(TypeError):
        D(1.5)
    with pytest.raises(TypeError):
        Node("x", 10.0)


def test_trade_moves_balances_and_sums_to_zero():
    l = mk("A", "B")
    l.record_trade("A", "B", Decimal("40"), T0)
    assert l.node("A").current_balance == Decimal("-40")
    assert l.node("B").current_balance == Decimal("40")
    assert l.total_balance() == 0
    assert l.outstanding("A", "B") == Decimal("40")


def test_nodes_must_join_at_zero():
    l = Ledger()
    with pytest.raises(ValidationError):
        l.add_node(Node("A", Decimal(100), current_balance=Decimal(5)))


def test_credit_floor_is_ceiling_minus_locked_stake():
    l = mk("A", "B")
    l.node("A").locked_vouch_stake = Decimal("20")
    l.record_trade("A", "B", Decimal("80"), T0)  # exactly at -(100-20)
    with pytest.raises(CreditLimitExceeded):
        l.record_trade("A", "B", Decimal("0.01"), T0)
    assert l.total_balance() == 0


def test_bilateral_netting_offsets_reverse_obligation():
    l = mk("A", "B")
    l.record_trade("B", "A", Decimal("50"), T0)
    l.record_trade("A", "B", Decimal("30"), T0 + timedelta(days=1))
    assert l.outstanding("B", "A") == Decimal("20")
    assert l.outstanding("A", "B") == 0
    l.record_trade("A", "B", Decimal("30"), T0 + timedelta(days=2))
    assert l.outstanding("B", "A") == 0
    assert l.outstanding("A", "B") == Decimal("10")
    assert l.total_balance() == 0


def test_rolling_window_boundaries():
    l = mk("B", "C")
    as_of = T0 + timedelta(days=60)
    l.record_trade("B", "C", Decimal("10"), as_of - timedelta(days=30))          # excluded (start is open)
    l.record_trade("B", "C", Decimal("20"), as_of - timedelta(days=30) + timedelta(seconds=1))
    l.record_trade("C", "B", Decimal("5"), as_of)                                # included (end is closed)
    out, inn = l.bilateral_volume("B", "C", as_of)
    assert (out, inn) == (Decimal("20"), Decimal("5"))
    assert l.bilateral_net("B", "C", as_of) == Decimal("15")


def test_stale_balance_90_day_boundary():
    l = mk("A", "B")
    l.record_trade("A", "B", Decimal("10"), T0)
    ninety = timedelta(days=90)
    assert l.stale_balance("A", T0 + ninety, ninety) == 0
    assert l.stale_balance("A", T0 + ninety + timedelta(seconds=1), ninety) == Decimal("10")


def test_clear_leg_fifo_and_insufficient():
    l = mk("A", "B", ceiling="1000")
    l.record_trade("A", "B", Decimal("10"), T0)
    l.record_trade("A", "B", Decimal("10"), T0 + timedelta(days=50))
    l.clear_leg(Hop("A", "B", Decimal("10")))
    # oldest lot cleared first -> nothing stale at day 95
    assert l.stale_balance("A", T0 + timedelta(days=95), timedelta(days=90)) == 0
    with pytest.raises(LedgerError):
        l.clear_leg(Hop("A", "B", Decimal("10.01")))


def test_transaction_rolls_back_exactly():
    l = mk("A", "B", "C", ceiling="1000")
    for a, b in (("A", "B"), ("B", "C"), ("C", "A")):
        l.record_trade(a, b, Decimal("50"), T0)
    before = l.state_fingerprint()
    with pytest.raises(RuntimeError):
        with l.transaction():
            l.apply_cycle([Hop("A", "B", Decimal(50)), Hop("B", "C", Decimal(50))])
            raise RuntimeError("boom")
    assert l.state_fingerprint() == before


def test_uniform_cycle_clear_is_net_neutral():
    l = mk("A", "B", "C", ceiling="1000")
    for a, b in (("A", "B"), ("B", "C"), ("C", "A")):
        l.record_trade(a, b, Decimal("50"), T0)
    bal = {k: n.current_balance for k, n in l.nodes.items()}
    l.apply_cycle([Hop("A", "B", Decimal(30)), Hop("B", "C", Decimal(30)), Hop("C", "A", Decimal(30))])
    assert {k: n.current_balance for k, n in l.nodes.items()} == bal
    assert l.outstanding("A", "B") == Decimal(20)


def test_ledger_always_sums_to_zero_under_random_activity():
    rng = random.Random(1337)
    ids = [f"N{i}" for i in range(8)]
    l = mk(*ids, ceiling="500")
    t = T0
    for _ in range(600):
        a, b = rng.sample(ids, 2)
        amt = Decimal(rng.randint(1, 5000)) / 100
        t += timedelta(hours=rng.randint(1, 12))
        try:
            l.record_trade(a, b, amt, t, rng.choice(list(Initiator)))
        except CreditLimitExceeded:
            pass
        if rng.random() < 0.2:
            x, y, z = rng.sample(ids, 3)
            c = min(l.outstanding(x, y), l.outstanding(y, z), l.outstanding(z, x))
            if c > 0:
                l.apply_cycle([Hop(x, y, c), Hop(y, z, c), Hop(z, x, c)])
        assert l.total_balance() == 0
        for n in l.nodes.values():
            assert n.current_balance >= n.credit_floor
