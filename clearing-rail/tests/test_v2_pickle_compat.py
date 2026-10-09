"""v2: objects pickled by the pre-v2 package (no reservation / repayment / limit fields,
no router-miss counters, no release records) still load and keep working."""
import pickle
from datetime import timedelta
from decimal import Decimal

from clearing_rail.amortization import AmortizationEngine
from clearing_rail.ledger import Ledger
from clearing_rail.solver import ClearingLoop
from clearing_rail.types import Node
from clearing_rail.vouch import VouchGraph
from conftest import T0

D = Decimal
PRE_V2_LEDGER = ("_reservations", "limits", "approvals", "_repayments", "_repay_by_node", "_repay_to", "_by_node")


def _old(obj, drop):
    """Build what unpickling a pre-v2 object yields: its __dict__ minus the new fields."""
    st = {k: v for k, v in obj.__dict__.items() if k not in drop}
    new = obj.__class__.__new__(obj.__class__)
    new.__setstate__(st)
    return new


def test_pre_v2_ledger_state_loads_and_trades():
    l = Ledger()
    for n in ("A", "B"):
        l.add_node(Node(n, D(1000)))
    l.record_trade("A", "B", D(100), T0)
    old = _old(l, PRE_V2_LEDGER)
    assert old.limits is None and old.approvals is None and old.reserved("A", "B") == 0
    assert [t.amount for t in old.transfers_of("A", T0 - timedelta(days=1), T0)] == [D(100)]
    old.record_trade("B", "A", D(40), T0 + timedelta(days=1))           # nets and logs a repayment
    assert old.outstanding("A", "B") == D(60) and old.repayments("A")[0].amount == D(40)
    assert pickle.loads(pickle.dumps(old)).outstanding("A", "B") == D(60)


def test_pre_v2_loop_and_amortization_state_load(world):
    world.add_nodes("A", "B", "C")
    loop = _old(world.loop, ("_router_misses", "_last_miss"))
    assert loop.router_misses("A") == 0 and loop.barred_until("A") is None
    eng = AmortizationEngine(world.ledger, VouchGraph(world.ledger))
    assert _old(eng, ("_releases",)).releases() == []
