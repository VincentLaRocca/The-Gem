"""STARTER CAP (v1 rule, pinned with ``StarterPolicy.v1()``): new members start at the
starter limit and grow only with matured, counterparty-diverse repayment to established
members. The V2 defaults are covered in test_starter_v2.py."""
from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.ledger import Ledger
from clearing_rail.limits import CreditLimits, StarterPolicy
from clearing_rail.types import CreditLimitExceeded, CycleCandidate, Hop, Node, ValidationError
from clearing_rail.vouch import VouchGraph
from conftest import T0, make_world, secret_for

D = Decimal
DAY = timedelta(days=1)
V1 = StarterPolicy.v1()
STARTER_LIMIT, GROWTH_RATE, GROWTH_PERIOD = V1.starter_limit, V1.growth_rate, V1.period
PERIOD_GROWTH_CAP_RATIO, COUNTERPARTY_CAP_RATIO = V1.period_growth_cap_ratio, V1.counterparty_cap_ratio
STARTER_MATURITY, ESTABLISHED_LIMIT = V1.maturity, V1.established_limit
GENESIS = ("G1", "G2", "G3", "G4", "G5")


def book(new=("M",), genesis=GENESIS, ceiling="100000", new_ceiling="20000"):
    l = Ledger()
    lim = CreditLimits(l, V1)
    for g in genesis:
        l.add_node(Node(g, D(ceiling)))
        lim.register(g, T0, genesis=True)
    for n in new:
        l.add_node(Node(n, D(new_ceiling)))
        lim.register(n, T0)
    return l, lim


def repay(l, node, cp, amount, ts):
    """node borrows ``amount`` from cp, then sells it back (repays) one hour later."""
    l.record_trade(node, cp, D(amount), ts)
    l.record_trade(cp, node, D(amount), ts + timedelta(hours=1))


def test_constants():
    assert STARTER_LIMIT == D(250) and GROWTH_RATE == D("0.5") and GROWTH_PERIOD == 30 * DAY
    assert PERIOD_GROWTH_CAP_RATIO == D("0.5") and COUNTERPARTY_CAP_RATIO == D("0.25")
    assert STARTER_MATURITY == 30 * DAY and ESTABLISHED_LIMIT == D(1000)
    for v in (STARTER_LIMIT, GROWTH_RATE, PERIOD_GROWTH_CAP_RATIO, COUNTERPARTY_CAP_RATIO, ESTABLISHED_LIMIT):
        assert isinstance(v, Decimal)


def test_unregistered_and_genesis_nodes_unrestricted():
    l, lim = book(new=())
    l.add_node(Node("LEGACY", D(5000)))
    l.record_trade("LEGACY", "G1", D(5000), T0)
    l.record_trade("G2", "G1", D(90000), T0)
    assert lim.effective_limit("G2", T0) == D(100000)
    assert lim.effective_limit("LEGACY", T0) == D(5000)


def test_new_member_capped_at_starter_limit():
    l, lim = book()
    assert lim.effective_limit("M", T0) == STARTER_LIMIT
    l.record_trade("M", "G1", D(200), T0)
    with pytest.raises(CreditLimitExceeded, match="starter cap"):
        l.record_trade("M", "G1", D("50.01"), T0)
    l.record_trade("M", "G1", D(50), T0)                 # exactly the starter limit
    assert l.node("M").current_balance == D(-250)


def test_starter_never_exceeds_configured_ceiling():
    l, lim = book(new_ceiling="100")
    assert lim.effective_limit("M", T0 + 400 * DAY) == D(100)


def test_growth_only_after_period_end_plus_maturity_and_capped_per_period():
    l, lim = book()
    for i, g in enumerate(GENESIS[:4]):                  # 4 x 62.5 = 250 credited (each at the 25% cap)
        repay(l, "M", g, "62.5", T0 + (i + 1) * DAY)
    assert lim.effective_limit("M", T0 + 59 * DAY) == STARTER_LIMIT
    # credited 250 * 0.5 = 125 = exactly the 50% period cap
    assert lim.effective_limit("M", T0 + 60 * DAY) == D("375")
    sched = lim.growth_schedule("M", T0 + 60 * DAY)
    assert sched == [(T0 + 30 * DAY, D(250), D("250.0"), D("125.00"))]


def test_period_cap_binds_even_with_huge_volume():
    l, lim = book()
    for g in GENESIS[:4]:
        for k in range(10):
            repay(l, "M", g, "60", T0 + DAY + k * timedelta(hours=3))
    assert lim.effective_limit("M", T0 + 60 * DAY) == D(250) + D(250) * PERIOD_GROWTH_CAP_RATIO


def test_single_counterparty_contributes_at_most_quarter_of_limit():
    l, lim = book()
    for k in range(20):
        repay(l, "M", "G1", "100", T0 + DAY + k * timedelta(hours=3))   # 2000 repaid to one member
    # credited = min(2000, 0.25*250) = 62.5 -> growth 31.25
    assert lim.effective_limit("M", T0 + 60 * DAY) == D("281.25")


def test_opening_debt_or_selling_to_non_creditor_earns_nothing():
    l, lim = book()
    l.record_trade("M", "G1", D(250), T0 + DAY)          # debt opened, never repaid
    for g in GENESIS[1:]:
        l.record_trade(g, "M", D(200), T0 + 2 * DAY)     # M sells to members it never owed
    assert l.repayments("M") == []
    assert lim.effective_limit("M", T0 + 90 * DAY) == STARTER_LIMIT


def test_sybil_ring_of_new_members_cannot_grow_each_other():
    sybils = ("M", "S1", "S2", "S3", "S4")
    l, lim = book(new=sybils)
    for k in range(5):
        for a in sybils:
            for b in sybils:
                if a != b:
                    repay(l, a, b, "50", T0 + DAY + k * DAY)
    assert l.repayments("M") and all(not r.counterparty_established for r in l.repayments("M"))
    for s in sybils:
        assert lim.effective_limit(s, T0 + 200 * DAY) == STARTER_LIMIT


def test_bad_standing_stops_growth():
    l, lim = book()
    l.record_trade("M", "G5", D(10), T0)                 # never repaid -> stale after 90 d
    for p in range(5):
        for g in GENESIS[:4]:
            repay(l, "M", g, "1", T0 + p * 30 * DAY + DAY)
    sched = lim.growth_schedule("M", T0 + 200 * DAY)
    grew = [g > 0 for (_, _, _, g) in sched]
    assert grew[:2] == [True, True] and not any(grew[3:])   # period ending day 90 onward: frozen


def test_geometric_growth_reaches_configured_ceiling_and_established():
    l, lim = book(new_ceiling="1500")
    L = STARTER_LIMIT
    for p in range(8):
        for g in GENESIS[:4]:
            repay(l, "M", g, str(L * COUNTERPARTY_CAP_RATIO), T0 + p * 30 * DAY + DAY)
        L = min(D(1500), L * D("1.5"))
    limits = [lim.effective_limit("M", T0 + (60 + 30 * p) * DAY) for p in range(6)]
    assert limits[:4] == [D("375"), D("562.5"), D("843.75"), D("1265.625")]
    assert limits[4] == D(1500) and limits[5] == D(1500)
    assert not lim.is_established("M", T0 + 119 * DAY)
    assert lim.is_established("M", T0 + 150 * DAY)


def test_cycle_clearing_counts_as_repayment_and_rolls_back_on_revert():
    w = make_world()
    lim = CreditLimits(w.ledger, V1)
    for n in ("A", "B", "C"):
        w.ledger.add_node(Node(n, D(1000)))
        w.node_keys.register(n, secret_for(n))
        lim.register(n, T0, genesis=(n != "A"))
    w.registry.register("S1", D(500), secret_for("S1"))
    now = w.clock.now()
    w.ledger.record_trade("A", "B", D(100), now)
    w.ledger.record_trade("B", "C", D(100), now)
    w.ledger.record_trade("C", "A", D(100), now)
    cand = CycleCandidate("c1", "S1", (Hop("A", "B", D(100)), Hop("B", "C", D(100)), Hop("C", "A", D(100))))
    w.publish(cand)
    bad = (Hop("A", "B", D(100)), Hop("B", "C", D(100)), Hop("C", "A", D(99)))
    w.engine.commit(w.submission(cand, legs=bad))           # mismatch -> revert + slash
    assert w.ledger.repayments() == []
    w.registry.register("S2", D(500), secret_for("S2"))
    cand2 = CycleCandidate("c2", "S2", cand.hops)
    w.publish(cand2)
    assert w.engine.commit(w.submission(cand2)).status.value == "committed"
    reps = w.ledger.repayments("A")
    assert [(r.counterparty, r.amount, r.via, r.counterparty_established) for r in reps] == [("B", D(100), "cycle", True)]


def test_vouch_stake_capped_by_vouchee_and_new_voucher_earned_limit():
    l, lim = book(new=("M", "N"), new_ceiling="20000")
    g = VouchGraph(l)
    e = g.vouch("G1", "M", T0)                      # 10% of 100000 = 10000, capped at M's 250
    assert e.delta_c == STARTER_LIMIT and l.node("G1").locked_vouch_stake == D(250)
    e2 = g.vouch("N", "G2", T0)                     # new voucher: 10% of its earned 250
    assert e2.delta_c == D("25.0")


def test_policy_validation_and_double_register():
    with pytest.raises(ValidationError):
        StarterPolicy(growth_rate=D(-1))
    with pytest.raises(ValidationError):
        StarterPolicy(period=timedelta(0))
    l, lim = book()
    with pytest.raises(ValidationError):
        lim.register("M", T0)


def test_custom_policy_respected():
    l = Ledger()
    lim = CreditLimits(l, StarterPolicy(starter_limit=D(50)))
    l.add_node(Node("M", D(1000)))
    l.add_node(Node("G", D(1000)))
    lim.register("M", T0)
    with pytest.raises(CreditLimitExceeded):
        l.record_trade("M", "G", D(51), T0)
