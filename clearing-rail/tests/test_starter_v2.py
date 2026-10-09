"""V2 growth rule: anchors vs peers, peer budget (conservation), match mode, min repaid
age, non-wash peers, softer stale freeze, promoted members as peers."""
from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.events import StakeClawedBack
from clearing_rail.ledger import Ledger
from clearing_rail.limits import CreditLimits, StarterPolicy
from clearing_rail.types import Node, ValidationError
from conftest import T0

D = Decimal
DAY = timedelta(days=1)
GEN = ("G1", "G2", "G3", "G4", "G5")

BUDGET = StarterPolicy(promoted_as_anchor=False, peer_mode="budget", peer_budget_ratio=D("0.33"),
                       seasoned_weight=D(1), seasoned_strict=False, counterparty_cap_ratio=D("0.25"),
                       period_growth_cap_ratio=D("0.5"), stale_freeze_ratio=D("0.5"),
                       min_repaid_age=DAY)


def book(new=("M",), policy=BUDGET, new_ceiling="20000"):
    l = Ledger()
    lim = CreditLimits(l, policy)
    for g in GEN:
        l.add_node(Node(g, D(100000))); lim.register(g, T0, genesis=True)
    for n in new:
        l.add_node(Node(n, D(new_ceiling))); lim.register(n, T0)
    return l, lim


def repay(l, node, cp, amount, ts, gap=2 * DAY):
    l.record_trade(node, cp, D(amount), ts)
    l.record_trade(cp, node, D(amount), ts + gap)


def test_v1_policy_matches_fixed_starter_constants():
    v1 = StarterPolicy.v1()
    assert (v1.starter_limit, v1.growth_rate, v1.period_growth_cap_ratio, v1.counterparty_cap_ratio,
            v1.established_limit) == (D(250), D("0.5"), D("0.5"), D("0.25"), D(1000))
    assert v1.maturity == 30 * DAY and v1.min_repaid_age == timedelta(0) and v1.promoted_as_anchor
    assert v1.seasoned_weight == 0 and v1.stale_freeze_ratio == 0 and v1.peer_mode == "match"


def test_quick_round_trip_earns_nothing_but_slow_repayment_does():
    l, lim = book()
    for i, g in enumerate(GEN[:4]):
        repay(l, "M", g, "62.5", T0 + (1 + 3 * i) * DAY, gap=timedelta(hours=23))   # < 1 day: excluded
    assert lim.effective_limit("M", T0 + 60 * DAY) == D(250)
    l2, lim2 = book()
    for i, g in enumerate(GEN[:4]):
        repay(l2, "M", g, "62.5", T0 + (1 + 3 * i) * DAY, gap=DAY)
    assert lim2.effective_limit("M", T0 + 60 * DAY) == D("375")


def test_peer_ring_with_no_anchor_work_earns_nothing():
    sybils = ("M", "S1", "S2", "S3", "S4")
    l, lim = book(new=sybils)
    for p in range(6):
        for a in sybils:
            for j, b in enumerate(sybils):
                if a != b:
                    repay(l, a, b, "20", T0 + p * 30 * DAY + (1 + j) * DAY)
    for s in sybils:
        assert lim.effective_limit(s, T0 + 240 * DAY) == D(250)


def test_peer_budget_bounds_ring_growth_by_anchor_work():
    """P (peer) does anchor work A in a period; newcomer N repays only to P.
    N's credit from P is at most 0.33 x A (conservation), split among P's repayers."""
    l, lim = book(new=("P", "N", "N2"))
    t = T0 + 61 * DAY                                     # P has tenure >= 60 d: a peer
    for i, g in enumerate(GEN[:4]):
        repay(l, "P", g, "40", t + i * DAY)               # A = 160 -> budget 52.8
    repay(l, "N", "P", "200", t + 5 * DAY)                # demand 200 -> N gets 52.8 (cp cap 62.5)
    end = T0 + 150 * DAY
    sched = {e: (L, cr) for e, L, cr, g in lim.growth_schedule("N", end)}
    assert sched[T0 + 90 * DAY][1] == D("52.80")
    repay(l, "N2", "P", "200", t + 6 * DAY)               # second repayer: budget shared pro rata
    sched = {e: (L, cr) for e, L, cr, g in lim.growth_schedule("N", end)}
    assert sched[T0 + 90 * DAY][1] == D("26.40")


def test_match_mode_caps_peer_credit_by_own_anchor_credit():
    pol = StarterPolicy(promoted_as_anchor=False, peer_mode="match", seasoned_match=D("0.5"),
                        seasoned_weight=D(1), seasoned_strict=False)
    l, lim = book(new=("P", "N"), policy=pol)
    repay(l, "N", "G1", "40", T0 + 61 * DAY)              # anchor credit 40
    repay(l, "N", "P", "60", T0 + 65 * DAY)               # peer 60 -> counts at most 0.5 x 40
    sched = {e: cr for e, L, cr, g in lim.growth_schedule("N", T0 + 150 * DAY)}
    assert sched[T0 + 90 * DAY] == D("60.0")


def test_young_or_washed_counterparty_is_not_a_peer():
    l, lim = book(new=("P", "N"))
    repay(l, "N", "P", "50", T0 + 10 * DAY)               # P only 10 days old
    assert not any(r.counterparty_seasoned for r in l.repayments("N"))
    l.events.append(StakeClawedBack(T0 + 70 * DAY, "G1", "P", D(1)))
    repay(l, "N", "P", "50", T0 + 75 * DAY)
    assert not any(r.counterparty_seasoned for r in l.repayments("N"))


def test_promoted_member_is_a_peer_not_an_anchor():
    pol = StarterPolicy(promoted_as_anchor=False, peer_mode="budget", peer_budget_ratio=D(0),
                        seasoned_weight=D(1), seasoned_strict=False, established_limit=D(250))
    l, lim = book(new=("P", "N"), policy=pol)
    assert lim.is_established("P", T0)                    # threshold at the starter: promoted at once
    repay(l, "N", "P", "50", T0 + 70 * DAY)
    r = l.repayments("N")[0]
    assert r.counterparty_established and not lim._is_anchor_rep(r)
    assert all(cr == 0 for _, _, cr, _ in lim.growth_schedule("N", T0 + 200 * DAY))   # zero budget


def test_soft_stale_freeze_only_on_material_stale_debt():
    l, lim = book()
    l.record_trade("M", "G5", D(10), T0)                  # 10 stale after 90 d: < 0.5 x L
    for p in range(5):
        for i, g in enumerate(GEN[:4]):
            repay(l, "M", g, "5", T0 + p * 30 * DAY + (1 + 3 * i) * DAY)
    grew = [g > 0 for (_, _, _, g) in lim.growth_schedule("M", T0 + 200 * DAY)]
    assert all(grew)
    l2, lim2 = book()
    l2.record_trade("M", "G5", D(200), T0)                # 200 stale > 0.5 x 250: frozen
    for p in range(5):
        for i, g in enumerate(GEN[:4]):
            repay(l2, "M", g, "5", T0 + p * 30 * DAY + (1 + 3 * i) * DAY)
    grew = [g > 0 for (_, _, _, g) in lim2.growth_schedule("M", T0 + 200 * DAY)]
    assert grew[:2] == [True, True] and not any(grew[3:])


def test_budget_cache_survives_rollback():
    l, lim = book(new=("P", "N"))
    t = T0 + 61 * DAY
    repay(l, "P", "G1", "40", t)
    repay(l, "N", "P", "60", t + 3 * DAY)
    before = lim.growth_schedule("N", T0 + 150 * DAY)
    try:
        with l.transaction():
            repay(l, "N", "P", "10", T0 + 149 * DAY)
            raise RuntimeError
    except RuntimeError:
        pass
    assert lim.growth_schedule("N", T0 + 150 * DAY) == before


def test_policy_validation_v2():
    with pytest.raises(ValidationError):
        StarterPolicy(peer_mode="other")
    with pytest.raises(ValidationError):
        StarterPolicy(seasoned_weight=D(2))
    with pytest.raises(ValidationError):
        StarterPolicy(min_repaid_age=-DAY)


def test_v2_defaults_are_the_chosen_constants():
    from clearing_rail import limits as m
    p = StarterPolicy()
    assert p == BUDGET                                    # the tests above exercise the defaults
    assert (m.STARTER_LIMIT, m.GROWTH_RATE, m.PERIOD_GROWTH_CAP_RATIO, m.COUNTERPARTY_CAP_RATIO) == \
        (D(250), D("0.5"), D("0.5"), D("0.25"))
    assert (m.STARTER_MATURITY, m.MIN_REPAID_AGE, m.SEASONED_MIN_TENURE) == (30 * DAY, DAY, 60 * DAY)
    assert (m.PEER_MODE, m.PEER_BUDGET_RATIO, m.SEASONED_WEIGHT, m.STALE_FREEZE_RATIO) == \
        ("budget", D("0.33"), D(1), D("0.5"))
    assert m.PROMOTED_AS_ANCHOR is False and m.SEASONED_STRICT is False and m.ESTABLISHED_LIMIT == D(1000)
    # design floor: honest work per credit of growth >= 1 / (rate x (1 + budget ratio)) ~= 1.50
    assert 1 / (p.growth_rate * (1 + p.peer_budget_ratio)) > D("1.5")
