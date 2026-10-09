"""Regression tests for the Satoshi-mode adversarial findings (S1 wash rings, S4
withholding, S6 rate-card splitting) and the hardening items. Worst-case seeds
from /workspace/mc-satoshi/adversarial.md are pinned with their exact parameters."""
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING

import pytest

from clearing_rail.amortization import (
    CLAWBACK_HORIZON,
    MATURITY_PERIOD,
    AmortizationEngine,
    AmortizationStatus as S,
)
from clearing_rail.events import RepeatWithholderFlagged, StakeClawedBack
from clearing_rail.ledger import Ledger
from clearing_rail.legal import (
    MIN_RECOVERY_RATIO,
    ClaimBook,
    SettlementDecision as SD,
    alpha,
    evaluate_settlement,
)
from clearing_rail.settlement import MIN_SOLVER_BOND
from clearing_rail.solver import ROUTER_MISS_BAR, ROUTER_MISS_THRESHOLD, ExecutionState as X
from clearing_rail.telemetry import true_vouch_integrity
from clearing_rail.types import CreditLimitExceeded, CycleCandidate, Hop, Node, ValidationError
from clearing_rail.vouch import VouchGraph
from conftest import T0, hsign, make_world, secret_for, triangle
from clearing_rail.crypto import cycle_hash, hop_message

H = timedelta(hours=1)
DAY = timedelta(days=1)
D = Decimal


def amort_world(ceil_a="1000", others=("B", "C", "D", "E", "F", "G", "H", "I"), ceiling="100000"):
    l = Ledger()
    l.add_node(Node("A", D(ceil_a)))
    for n in others:
        l.add_node(Node(n, D(ceiling)))
    g = VouchGraph(l)
    edge = g.vouch("A", "B", T0)
    return l, g, edge, AmortizationEngine(l, g)


# =============================================================== S1 wash rings
def test_constants():
    assert MATURITY_PERIOD == timedelta(days=30) and CLAWBACK_HORIZON == timedelta(days=60)
    assert MIN_RECOVERY_RATIO == D("0.10") and MIN_SOLVER_BOND == D("100")
    assert ROUTER_MISS_THRESHOLD == 2 and ROUTER_MISS_BAR == timedelta(days=30)


@pytest.mark.parametrize("k", [3, 4, 5, 6, 7, 8])
def test_ring_of_k_is_aggregate_wash_before_and_after_maturity(k):
    l, g, edge, eng = amort_world()
    members = ["B", "C", "D", "E", "F", "G", "H", "I"][:k]
    t = T0 + H
    for i in range(k):
        l.record_trade(members[i], members[(i + 1) % k], D("100"), t + i * H)
    early = eng.amortize("A", "B", "C", t + 9 * H)      # old attack point: aggregate inbound already visible
    assert early.status is S.WASH_DISQUALIFIED and early.unlocked == 0
    r = eng.amortize("A", "B", "C", t + MATURITY_PERIOD + 9 * H)
    assert r.status is S.WASH_DISQUALIFIED and r.unlocked == 0       # inbound came back via the ring
    assert edge.released == 0 and l.node("A").locked_vouch_stake == D("100")


def test_ring_then_cleared_by_cycle_still_disqualified():
    """Clearing the ring's obligations does not erase the transfers the wash check reads."""
    w = make_world()
    w.add_nodes("A", "B", "C", "D")
    w.add_solver("S1")
    g = VouchGraph(w.ledger)
    g.vouch("A", "B", T0)
    now = w.clock.now()
    for d_, c_ in (("B", "C"), ("C", "D"), ("D", "B")):
        w.ledger.record_trade(d_, c_, D("100"), now)
    cand = CycleCandidate("ring", "S1", (Hop("B", "C", D(100)), Hop("C", "D", D(100)), Hop("D", "B", D(100))))
    w.publish(cand)
    w.loop.propose(cand); w.loop.validate("ring"); w.loop.open_signature_window("ring")
    ch = cycle_hash("ring", "S1", cand.hops)
    for i, h in enumerate(cand.hops):
        w.loop.submit_signature("ring", h.debtor, hsign(secret_for(h.debtor), hop_message(ch, i, h)))
    assert w.loop.settle("ring").state is X.SETTLED
    r = AmortizationEngine(w.ledger, g).amortize("A", "B", "C", now + MATURITY_PERIOD + H)
    assert r.status is S.WASH_DISQUALIFIED


def test_timing_payback_after_check_is_clawed_back():
    l, g, edge, eng = amort_world()
    t = T0 + H
    l.record_trade("B", "C", D("100"), t)
    assert eng.amortize("A", "B", "C", t + timedelta(minutes=1)).status is S.NOT_MATURED   # old attack point
    r = eng.amortize("A", "B", "C", t + MATURITY_PERIOD)       # adaptive attacker: right at maturity
    assert r.status is S.UNLOCKED and r.unlocked == D("100")
    l.record_trade("C", "B", D("100"), t + MATURITY_PERIOD + H)  # pay-back after the check
    r2 = eng.amortize("A", "B", "C", t + MATURITY_PERIOD + 2 * H)
    assert r2.clawed_back == D("100") and edge.released == 0
    assert l.node("A").locked_vouch_stake == D("100")
    (cb,) = l.events.of_type(StakeClawedBack)
    assert cb.amount == D("100") and cb.release_id == 1
    assert true_vouch_integrity(l.events) == 0


def test_rollover_payback_inside_horizon_is_clawed_back():
    l, g, edge, eng = amort_world()
    t = T0 + H
    l.record_trade("B", "C", D("100"), t)
    assert eng.amortize("A", "B", "C", t + MATURITY_PERIOD).unlocked == D("100")
    l.record_trade("C", "B", D("100"), t + CLAWBACK_HORIZON)     # exactly at the horizon: still caught
    assert eng.recheck(t + CLAWBACK_HORIZON + H) == D("100")
    assert edge.released == 0


def test_release_is_final_after_horizon():
    """Documented limit: value returned after CLAWBACK_HORIZON is not treated as a wash."""
    l, g, edge, eng = amort_world()
    t = T0 + H
    l.record_trade("B", "C", D("100"), t)
    assert eng.amortize("A", "B", "C", t + MATURITY_PERIOD).unlocked == D("100")
    assert eng.recheck(t + CLAWBACK_HORIZON + H) == 0             # finalised
    l.record_trade("C", "B", D("100"), t + CLAWBACK_HORIZON + 2 * H)
    assert eng.recheck(t + CLAWBACK_HORIZON + 3 * H) == 0
    assert edge.released == D("100")


def test_clawback_relocks_and_blocks_new_debits_when_freed_credit_was_spent():
    l, g, edge, eng = amort_world()
    t = T0 + H
    l.record_trade("B", "C", D("100"), t)
    eng.amortize("A", "B", "C", t + MATURITY_PERIOD)
    l.record_trade("A", "C", D("1000"), t + MATURITY_PERIOD + H)  # A spends the whole freed ceiling
    l.record_trade("C", "B", D("100"), t + MATURITY_PERIOD + 2 * H)
    assert eng.recheck(t + MATURITY_PERIOD + 3 * H) == D("100")
    assert l.node("A").current_balance < l.node("A").credit_floor
    with pytest.raises(CreditLimitExceeded):
        l.record_trade("A", "C", D("0.01"), t + MATURITY_PERIOD + 4 * H)


def test_honest_matured_outbound_unlocks_and_scores_full_integrity():
    l, g, edge, eng = amort_world()
    l.record_trade("B", "C", D("100"), T0 + H)
    assert eng.amortize("A", "B", "C", T0 + H + MATURITY_PERIOD).unlocked == D("100")
    assert eng.recheck(T0 + H + CLAWBACK_HORIZON + H) == 0
    assert true_vouch_integrity(l.events) == 1


def test_seed_20261008128_amortize_before_reverse():
    """Worst S1 case (gain was 1999.1380): A ceiling 19991.38, x 2719.27,
    amortize 0:42:41 after the leg, reverse leg 5d21:45:11 after it."""
    l, g, edge, eng = amort_world(ceil_a="19991.38")
    assert edge.delta_c == D("1999.1380")
    t = T0 + H
    l.record_trade("B", "C", D("2719.27"), t)
    assert eng.amortize("A", "B", "C", t + timedelta(minutes=42, seconds=41)).status is S.NOT_MATURED
    rev = t + timedelta(days=5, hours=21, minutes=45, seconds=11)
    l.record_trade("C", "B", D("2719.27"), rev)
    eng.amortize("A", "B", "C", rev + H)                         # the battery's later sweep
    assert eng.amortize("A", "B", "C", t + MATURITY_PERIOD).status is S.WASH_DISQUALIFIED
    assert l.node("A").locked_vouch_stake == D("1999.1380")


# =============================================================== S4 withholding
def ready(w, amount="100"):
    w.add_nodes("A", "B", "C")
    w.add_solver("S1")
    now = w.clock.now()
    w.ledger.record_trade("A", "B", D(amount), now - DAY)
    w.ledger.record_trade("B", "C", D(amount), now - DAY)
    w.ledger.record_trade("C", "A", D(amount), now - DAY)


def run(w, cid, nodes=("A", "B", "C"), amount="100", signers=("A", "B", "C")):
    cand = triangle(cid, "S1", amount, nodes=nodes)
    w.publish(cand)
    w.loop.propose(cand); w.loop.validate(cid); w.loop.open_signature_window(cid)
    ch = cycle_hash(cid, "S1", cand.hops)
    for i, h in enumerate(cand.hops):
        if h.debtor in signers:
            w.loop.submit_signature(cid, h.debtor, hsign(secret_for(h.debtor), hop_message(ch, i, h)))
    return cand


def test_signed_hop_is_reserved_so_a_later_trade_cannot_force_revert(world):
    ready(world)
    run(world, "c1")
    assert world.ledger.reserved("A", "B") == D("100")
    world.ledger.record_trade("B", "A", D("40"), world.clock.now())   # old attack: nets A->B to 60
    assert world.ledger.outstanding("A", "B") == D("100")            # reserved part untouched
    assert world.ledger.outstanding("B", "A") == D("40")             # opened a reverse lot instead
    exe = world.loop.settle("c1")
    assert exe.state is X.SETTLED and world.registry.account("S1").slashed_total == 0
    assert world.ledger.reserved("A", "B") == 0 and world.ledger.total_balance() == 0


def test_shrink_before_own_signature_blocks_that_signature(world):
    ready(world)
    run(world, "c1", signers=("B", "C"))
    world.ledger.record_trade("B", "A", D("40"), world.clock.now())
    cand = world.loop.get("c1").candidate
    ch = cycle_hash("c1", "S1", cand.hops)
    i, h = cand.debit_leg("A")
    with pytest.raises(ValidationError):
        world.loop.submit_signature("c1", "A", hsign(secret_for("A"), hop_message(ch, i, h)))


def test_double_reservation_of_one_obligation_is_refused(world):
    ready(world)
    run(world, "c1", signers=("A",))
    cand2 = triangle("c2", "S1", "100")
    world.publish(cand2)
    world.loop.propose(cand2); world.loop.validate("c2"); world.loop.open_signature_window("c2")
    i, h = cand2.debit_leg("A")
    with pytest.raises(ValidationError):
        world.loop.submit_signature("c2", "A", hsign(secret_for("A"),
                                                     hop_message(cycle_hash("c2", "S1", cand2.hops), i, h)))


def test_reservations_released_on_router_timeout_and_target_drop(world):
    ready(world)
    run(world, "c1", signers=("A", "B"))
    world.clock.advance(timedelta(hours=2, seconds=1))
    assert world.loop.tick("c1").state is X.RERUN_REQUESTED
    assert world.ledger.reserved("A", "B") == 0 and world.ledger.reserved("B", "C") == 0


def test_router_misses_counted_across_lineages_then_aged_and_barred(world):
    ready(world)
    for r in range(ROUTER_MISS_THRESHOLD):
        run(world, f"r{r}", signers=("A", "B"))                     # C withholds every time
        world.clock.advance(timedelta(hours=2, seconds=1))
        assert world.loop.tick(f"r{r}").state is X.RERUN_REQUESTED
    assert world.loop.router_misses("C") == 2
    assert world.ledger.retry_count("C") == 1                        # aged once, at the threshold
    (flag,) = world.ledger.events.of_type(RepeatWithholderFlagged)
    assert flag.node_id == "C" and flag.misses == 2
    with pytest.raises(ValidationError):
        world.loop.propose(triangle("r9", "S1", "100"))
    world.clock.advance(ROUTER_MISS_BAR)
    world.loop.propose(triangle("r10", "S1", "100"))                 # bar has expired


def test_single_miss_is_free(world):
    ready(world)
    run(world, "c1", signers=("A", "B"))
    world.clock.advance(timedelta(hours=2, seconds=1))
    world.loop.tick("c1")
    assert world.loop.router_misses("C") == 1 and world.ledger.retry_count("C") == 0
    assert world.loop.barred_until("C") is None


def test_seed_20261008210_router_withhold_capped():
    """Worst S4 case (18 h honest lock over 6 rounds): k=7, a=3171.62, target N6
    (lot 256 d old), router N4 withholds every round, tick every 1 h."""
    w = make_world()
    nodes = [f"N{i}" for i in range(7)]
    w.add_nodes(*nodes, ceiling="1000000")
    w.add_solver("S")
    ages = [timedelta(days=28, seconds=5486), timedelta(days=6, seconds=19581), timedelta(days=13, seconds=66617),
            timedelta(days=10, seconds=73361), timedelta(days=29, seconds=79900), timedelta(days=7, seconds=27133),
            timedelta(days=256, seconds=10388)]
    a = D("3171.62")
    for i in range(7):
        w.ledger.record_trade(nodes[i], nodes[(i + 1) % 7], a, T0 - ages[i])
    hops = tuple(Hop(nodes[i], nodes[(i + 1) % 7], a) for i in range(7))
    opened = w.clock.now()
    rounds_run = 0
    for r in range(6):
        cand = CycleCandidate(f"r{r}", "S", hops)
        try:
            w.loop.propose(cand)
        except ValidationError:
            break
        rounds_run += 1
        w.loop.validate(cand.candidate_id); w.loop.open_signature_window(cand.candidate_id)
        ch = cycle_hash(cand.candidate_id, "S", hops)
        for i, h in enumerate(hops):
            if h.debtor != "N4":
                w.loop.submit_signature(cand.candidate_id, h.debtor, hsign(secret_for(h.debtor), hop_message(ch, i, h)))
        while w.loop.get(cand.candidate_id).state is X.AWAITING_SIGNATURES:
            w.clock.advance(H)
            w.loop.tick(cand.candidate_id)
        assert w.loop.get(cand.candidate_id).state is X.RERUN_REQUESTED
    assert rounds_run == ROUTER_MISS_THRESHOLD
    assert w.clock.now() - opened == timedelta(hours=6)              # was 18 h
    assert w.loop.router_misses("N4") == 2 and w.ledger.retry_count("N4") == 1


# =============================================================== S6 rate card
def _min_offer(fn, face):
    lo, hi = 1, int(face * 100)
    while lo < hi:
        mid = (lo + hi) // 2
        if fn(D(mid) / 100).decision is SD.ACCEPT:
            hi = mid
        else:
            lo = mid + 1
    return D(lo) / 100


def test_min_recovery_on_negative_floor():
    assert evaluate_settlement(D(100), D(500), 10, D("9.99")).decision is SD.REJECT
    assert evaluate_settlement(D(100), D(500), 10, D("1E-28")).decision is SD.REJECT
    assert evaluate_settlement(D(100), D(500), 10, D(10)).decision is SD.ACCEPT
    assert evaluate_settlement(D(100), D(500), 10, D(0)).decision is SD.WRITE_OFF


def test_min_recovery_on_positive_floor_below_it():
    # floor = 100*0.70 - 65 = 5 < min recovery 10
    assert evaluate_settlement(D(100), D(65), 0, D(5)).decision is SD.REJECT
    assert evaluate_settlement(D(100), D(65), 0, D(10)).decision is SD.ACCEPT


def test_exposure_prorates_filing_cost():
    # alone: 1000*0.7 - 1000 < 0; inside 10000 exposure: 700 - 100 = 600
    r = evaluate_settlement(D(1000), D(1000), 0, D(600), exposure=D(10000))
    assert r.floor_price == D("600.0") and r.decision is SD.ACCEPT
    assert evaluate_settlement(D(1000), D(1000), 0, D("599.99"), exposure=D(10000)).decision is SD.REJECT
    with pytest.raises(ValidationError):
        evaluate_settlement(D(1000), D(1), 0, D(1), exposure=D(999))


def test_claimbook_aggregates_and_closes():
    b = ClaimBook()
    ids = [b.add_claim("dbt", "cr", D(1000)) for _ in range(10)]
    b.add_claim("dbt", "other", D(5))
    assert b.exposure("dbt", "cr") == D(10000)
    assert b.evaluate(ids[0], D(1000), 0, D("0.01")).decision is SD.REJECT
    assert b.evaluate(ids[0], D(1000), 0, D(600)).decision is SD.ACCEPT
    with pytest.raises(ValidationError):
        b.evaluate(ids[0], D(1000), 0, D(600))
    assert b.exposure("dbt", "cr") == D(10000)                        # basis does not shrink


def test_seed_20261008136_split_claims():
    """Worst S6 case (gain was 99993.22): face 99993.67, cost 1776.63, 272 days,
    split into 45 pieces under cost/alpha, 0.01 offered per piece (0.45 total)."""
    face, cost, days = D("99993.67"), D("1776.63"), 272
    a = alpha(days)
    single = _min_offer(lambda o: evaluate_settlement(face, cost, days, o), face)
    assert single == D("77418.36")
    piece_max = ((cost / a) - D("0.01")).quantize(D("0.01"))
    n, rem = divmod(face, piece_max)
    pieces = [piece_max] * int(n) + ([rem] if rem else [])
    assert len(pieces) == 45
    book = ClaimBook()
    ids = [book.add_claim("dbt", "cr", p) for p in pieces]
    assert all(book.evaluate(i, cost, days, D("0.01")).decision is SD.REJECT for i in ids)
    paid = D(0)
    cache = {}
    for i, p in zip(ids, pieces):
        if p not in cache:
            cache[p] = _min_offer(lambda o: evaluate_settlement(p, cost, days, o, exposure=face), p)
        r = book.evaluate(i, cost, days, cache[p])
        assert r.decision is SD.ACCEPT
        paid += r.recovered
    assert paid >= single
    # without the book, the per-piece floor is negative but MIN_RECOVERY still binds
    direct = sum((_min_offer(lambda o: evaluate_settlement(p, cost, days, o), p) for p in set(pieces)), D(0))
    assert all(_min_offer(lambda o: evaluate_settlement(p, cost, days, o), p) >= MIN_RECOVERY_RATIO * p
               for p in set(pieces))


# =============================================================== hardening
def test_min_solver_bond(world):
    with pytest.raises(ValidationError):
        world.add_solver("S0", "99.99")
    world.add_solver("S1", "100")
    world.add_solver("S2", "150")
    world.registry.slash_stake("S2", D("0.5"))                        # 75 left < minimum
    assert not world.registry.is_registered("S2")
    assert world.registry.is_registered("S1")


def test_negative_urgency_boosts_rejected():
    with pytest.raises(ValidationError):
        Node("n", D(100), urgency_boosts_used=-1)
    with pytest.raises(ValidationError):
        Node("n", D(100), urgency_boosts_used=True)


def test_negative_boost_mutation_caught_at_validate(world):
    world.add_nodes("A", "B", "C")
    world.add_solver("S1")
    now = world.clock.now()
    world.ledger.record_trade("A", "B", D(400), now - timedelta(days=91))
    world.ledger.record_trade("B", "C", D(100), now)
    world.ledger.record_trade("C", "A", D(100), now)
    world.ledger.node("A").urgency_boosts_used = -1
    world.loop.propose(triangle("c1", "S1", "100"))
    with pytest.raises(ValidationError):
        world.loop.validate("c1")


def test_vouch_integrity_not_maxed_by_one_small_trade():
    l = Ledger()
    l.add_node(Node("T", D(1000)))
    l.add_node(Node("O", D(10 ** 6)))
    g = VouchGraph(l)
    for i in range(40):
        l.add_node(Node(f"S{i}", D(170)))
        g.vouch(f"S{i}", "T", T0)                                   # ΔC = 17 each
    l.record_trade("T", "O", D(17), T0 + H)
    eng = AmortizationEngine(l, g)
    released = sum((eng.amortize(f"S{i}", "T", "O", T0 + H + MATURITY_PERIOD).unlocked for i in range(40)), D(0))
    assert released == D(680)
    assert true_vouch_integrity(l.events) == D(17) / D(680)         # was 1
