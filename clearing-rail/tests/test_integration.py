"""End-to-end: real engine events feed all four telemetry ratios; no wall clock in core."""
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from clearing_rail.amortization import AmortizationEngine
from clearing_rail.crypto import cycle_hash, hop_message
from clearing_rail.telemetry import compute
from clearing_rail.types import Initiator
from clearing_rail.vouch import VouchGraph
from conftest import hsign, make_world, secret_for, triangle

PKG = Path(__file__).resolve().parents[1] / "clearing_rail"


def test_no_wall_clock_reads_in_package():
    pat = re.compile(r"datetime\.now\(|datetime\.utcnow\(|date\.today\(|time\.time\(|time\.monotonic\(")
    for f in PKG.glob("*.py"):
        assert not pat.search(f.read_text()), f"wall-clock read in {f.name}"


def test_no_float_literals_in_money_paths():
    for f in PKG.glob("*.py"):
        assert "float(" not in f.read_text(), f.name


def test_end_to_end_telemetry():
    w = make_world()
    w.add_nodes("A", "B", "C", "V", "Q")
    w.add_solver("S1")
    now = w.clock.now()
    L = w.ledger
    # trades: 2 agent-initiated of 4
    L.record_trade("A", "B", Decimal("400"), now - timedelta(days=91), Initiator.AGENT)
    L.record_trade("B", "C", Decimal("100"), now, Initiator.HUMAN)
    L.record_trade("C", "A", Decimal("100"), now, Initiator.AGENT)
    L.record_trade("V", "Q", Decimal("100"), now - timedelta(days=5), Initiator.HUMAN)
    # vouch: Q vouches V (ΔC=100); V's net-leg volume with outside C... C is outside the tree
    g = VouchGraph(L)
    edge = g.vouch("Q", "V", now)
    L.record_trade("V", "C", Decimal("60"), now)
    # FIX-ADJUST: was as_of=now; the V->C leg must be matured (30 d) before it backs an unlock
    assert AmortizationEngine(L, g).amortize("Q", "V", "C", now + timedelta(days=30)).unlocked == Decimal("60")
    g.release(edge, Decimal("20"), now, via_outside_volume=False, reason="governance")
    # clearing: A is a stale target, settles intact
    cand = triangle("c1", "S1", "100")
    w.publish(cand)
    w.loop.propose(cand)
    w.loop.validate("c1")
    w.loop.open_signature_window("c1")
    ch = cycle_hash("c1", "S1", cand.hops)
    for i, h in enumerate(cand.hops):
        w.loop.submit_signature("c1", h.debtor, hsign(secret_for(h.debtor), hop_message(ch, i, h)))
    w.loop.settle("c1")
    # a boosted stale node in another candidate -> flagged but rejected
    L.node("V").urgency_boosts_used = 1
    L.record_trade("Q", "C", Decimal("10"), now)
    L.record_trade("C", "V", Decimal("10"), now)
    c2 = triangle("c2", "S1", "10", nodes=("V", "Q", "C"))
    w.clock.advance(timedelta(days=90))   # V's day-5 lot is now 95 days old
    w.loop.propose(c2)
    w.loop.validate("c2")

    snap = compute(L.events, w.clock.now())
    # agent: 400 + 100 = 500 of 400+100+100+100+60+10+10 = 780
    assert Decimal(snap.extraction_autonomy) == Decimal(500) / Decimal(780)
    assert Decimal(snap.true_vouch_integrity) == Decimal("0.75")
    assert Decimal(snap.distress_discipline) == Decimal("0.5")
    assert Decimal(snap.debtor_clearance_share) == Decimal(1)
    assert L.total_balance() == 0
