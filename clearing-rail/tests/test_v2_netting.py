"""V2: a reservation must not leave both directions of a pair outstanding once it is gone."""
from datetime import timedelta
from decimal import Decimal

from clearing_rail.ledger import Ledger
from clearing_rail.solver import ExecutionState as X
from clearing_rail.types import Node
from conftest import T0, hsign, secret_for, triangle
from clearing_rail.crypto import cycle_hash, hop_message

D = Decimal
DAY = timedelta(days=1)


def ready(w, amount="100"):
    w.add_nodes("A", "B", "C")
    w.add_solver("S1")
    now = w.clock.now()
    for d, c in (("A", "B"), ("B", "C"), ("C", "A")):
        w.ledger.record_trade(d, c, D(amount), now - DAY)


def run(w, cid, signers=("A", "B", "C")):
    cand = triangle(cid, "S1", "100")
    w.publish(cand)
    w.loop.propose(cand); w.loop.validate(cid); w.loop.open_signature_window(cid)
    ch = cycle_hash(cid, "S1", cand.hops)
    for i, h in enumerate(cand.hops):
        if h.debtor in signers:
            w.loop.submit_signature(cid, h.debtor, hsign(secret_for(h.debtor), hop_message(ch, i, h)))
    return cand


def two_way(l):
    return [(d, c) for (d, c), lots in l._lots.items() if lots and l._lots.get((c, d))]


def test_net_pair_nets_only_unreserved_and_keeps_balances():
    l = Ledger()
    l.add_node(Node("A", D(1000))); l.add_node(Node("B", D(1000)))
    l.record_trade("A", "B", D(100), T0)
    l.reserve("r", "A", "B", D(60))
    l.record_trade("B", "A", D(70), T0 + DAY)        # nets the free 40, opens a reverse 30
    assert l.outstanding("A", "B") == D(60) and l.outstanding("B", "A") == D(30)
    bal = (l.node("A").current_balance, l.node("B").current_balance)
    assert l.net_pair("A", "B", T0 + DAY) == 0         # still reserved: nothing to net
    l.release_reservation("r")
    assert l.net_pair("A", "B", T0 + 2 * DAY) == D(30)
    assert l.outstanding("A", "B") == D(30) and l.outstanding("B", "A") == 0
    assert (l.node("A").current_balance, l.node("B").current_balance) == bal
    reps = [(r.repayer, r.counterparty, r.amount, r.via) for r in l.repayments() if r.ts == T0 + 2 * DAY]
    assert reps == [("A", "B", D(30), "netting"), ("B", "A", D(30), "netting")]
    assert two_way(l) == []


def test_net_pair_rolls_back_with_transaction():
    l = Ledger()
    l.add_node(Node("A", D(1000))); l.add_node(Node("B", D(1000)))
    l.record_trade("A", "B", D(100), T0)
    l.reserve("r", "A", "B", D(100))
    l.record_trade("B", "A", D(30), T0)
    l.release_reservation("r")
    try:
        with l.transaction():
            l.net_pair("A", "B", T0)
            raise RuntimeError
    except RuntimeError:
        pass
    assert l.outstanding("B", "A") == D(30) and len(l.repayments()) == 0


def test_router_timeout_renets_the_pair_the_reservation_split(world):
    ready(world)
    run(world, "c1", signers=("A", "B"))              # C withholds
    world.ledger.record_trade("B", "A", D(40), world.clock.now())
    assert len(two_way(world.ledger)) == 2              # reserved A->B 100, reverse B->A 40
    world.clock.advance(timedelta(hours=2, seconds=1))
    assert world.loop.tick("c1").state is X.RERUN_REQUESTED
    assert two_way(world.ledger) == []
    assert world.ledger.outstanding("A", "B") == D(60) and world.ledger.total_balance() == 0


def test_force_revert_renets(world):
    ready(world)
    run(world, "c1")
    world.ledger.record_trade("B", "A", D(40), world.clock.now())
    world.loop.force_revert("c1", "test")
    assert two_way(world.ledger) == [] and world.ledger.outstanding("A", "B") == D(60)


def test_settle_still_consumes_reserved_hops_and_leaves_no_two_way(world):
    ready(world, "150")
    run(world, "c1")
    world.ledger.record_trade("B", "A", D(80), world.clock.now())   # nets free 50, reverse 30
    exe = world.loop.settle("c1")
    assert exe.state is X.SETTLED and world.registry.account("S1").slashed_total == 0
    assert two_way(world.ledger) == [] and world.ledger.outstanding("B", "A") == D(30)
