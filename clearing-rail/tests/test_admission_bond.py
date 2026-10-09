"""EXPERIMENT: admission bond book (off by default)."""
from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.admission import BOND_AMOUNT, AdmissionBonds, BondPolicy
from clearing_rail.events import StakeClawedBack
from clearing_rail.ledger import Ledger
from clearing_rail.limits import CreditLimits, StarterPolicy
from clearing_rail.types import Node, ValidationError
from conftest import T0

D = Decimal
DAY = timedelta(days=1)


def world(amount="125"):
    l = Ledger()
    lim = CreditLimits(l, StarterPolicy.v1())
    for g in ("G1", "G2", "G3", "G4"):
        l.add_node(Node(g, D(100000))); lim.register(g, T0, genesis=True)
    l.add_node(Node("M", D(20000))); lim.register("M", T0)
    bonds = AdmissionBonds(l, lim, BondPolicy(amount=D(amount)))
    bonds.post("M", T0)
    return l, lim, bonds


def test_off_by_default():
    assert BOND_AMOUNT == 0 and BondPolicy().amount == 0
    l = Ledger()
    assert not hasattr(l, "bonds")                       # nothing attaches a bond book


def test_default_forfeits_after_stale_and_idle():
    l, lim, bonds = world()
    l.record_trade("M", "G1", D(250), T0 + DAY)          # cash out, never come back
    assert bonds.evaluate("M", T0 + 80 * DAY).state == "held"
    assert bonds.evaluate("M", T0 + 92 * DAY).state == "forfeited"
    assert bonds.bond("M").reason == "default" and bonds.totals()["forfeited"] == D(125)


def test_active_member_with_old_debt_is_not_forfeited():
    l, lim, bonds = world()
    l.record_trade("M", "G1", D(250), T0 + DAY)
    l.record_trade("G2", "M", D(10), T0 + 85 * DAY)       # still selling (repaying) recently
    assert bonds.evaluate("M", T0 + 120 * DAY).state == "held"


def test_wash_forfeits():
    l, lim, bonds = world()
    l.events.append(StakeClawedBack(T0 + DAY, "G1", "M", D(10)))
    assert bonds.evaluate("M", T0 + 2 * DAY).reason == "wash"


def test_refund_after_tenure_and_growth():
    l, lim, bonds = world()
    for p in range(5):
        for i, g in enumerate(("G1", "G2", "G3", "G4")):
            t = T0 + p * 30 * DAY + (1 + 3 * i) * DAY
            L = lim.effective_limit("M", t)
            a = (L * D("0.2")).quantize(D("0.01"))
            l.record_trade("M", g, a, t)
            l.record_trade(g, "M", a, t + 2 * DAY)
    assert bonds.evaluate("M", T0 + 150 * DAY).state == "held"           # tenure not reached
    assert lim.effective_limit("M", T0 + 181 * DAY) >= 500
    assert bonds.evaluate("M", T0 + 181 * DAY).state == "refunded"


def test_validation():
    with pytest.raises(ValidationError):
        BondPolicy(amount=D(-1))
    l, lim, bonds = world()
    with pytest.raises(ValidationError):
        bonds.post("M", T0)
