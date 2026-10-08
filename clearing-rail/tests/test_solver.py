from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.crypto import cycle_hash, hop_message
from clearing_rail.events import (
    RouterDropped,
    TargetDebitClearedIntact,
    TargetDebitIdentified,
    TargetDropped,
    TargetFlagEvaluated,
)
from clearing_rail.settlement import CommitStatus
from clearing_rail.solver import ExecutionState as X
from clearing_rail.types import (
    CycleCandidate,
    Hop,
    IllegalTransition,
    SignatureError,
    SignatureWindowClosed,
    ValidationError,
)
from conftest import hsign, make_world, secret_for, seed_triangle, triangle

DAY = timedelta(days=1)
STALE = timedelta(days=91)


def ready(world, a_amount="400", a_age=STALE, amount="100", cid="c1"):
    world.add_nodes("A", "B", "C")
    world.add_solver("S1")
    seed_triangle(world, amount=amount, a_amount=a_amount, a_age=a_age)
    cand = triangle(cid, "S1", amount)
    world.publish(cand)
    return cand


def sig(world, cand, node):
    i, hop = cand.debit_leg(node)
    return hsign(secret_for(node), hop_message(cycle_hash(cand.candidate_id, cand.solver_id, cand.hops), i, hop))


def start(world, cand):
    world.loop.propose(cand)
    world.loop.validate(cand.candidate_id)
    return world.loop.open_signature_window(cand.candidate_id)


def sign(world, cand, *nodes):
    for n in nodes:
        world.loop.submit_signature(cand.candidate_id, n, sig(world, cand, n))


# --------------------------------------------------------------- targets
def test_90_day_boundary_exactly_90_is_not_stale(world):
    cand = ready(world, a_age=timedelta(days=90))
    exe = start(world, cand)
    assert exe.target_ids == frozenset()
    assert world.ledger.events.of_type(TargetFlagEvaluated) == []


def test_just_over_90_days_is_compression_target(world):
    cand = ready(world, a_age=timedelta(days=90, seconds=1))
    exe = start(world, cand)
    assert exe.target_ids == {"A"}
    (flag,) = world.ledger.events.of_type(TargetFlagEvaluated)
    assert flag.accepted and flag.prior_boosts == 0 and flag.stale_balance == Decimal("400")
    (ident,) = world.ledger.events.of_type(TargetDebitIdentified)
    assert (ident.node_id, ident.creditor, ident.amount) == ("A", "B", Decimal("100"))


def test_boosted_stale_node_is_flagged_but_not_targeted(world):
    cand = ready(world)
    world.ledger.node("A").urgency_boosts_used = 1
    exe = start(world, cand)
    assert exe.target_ids == frozenset()
    (flag,) = world.ledger.events.of_type(TargetFlagEvaluated)
    assert flag.prior_boosts == 1 and not flag.accepted
    assert exe.window_for("A") == timedelta(hours=2)   # router treatment
    assert world.ledger.events.of_type(TargetDebitIdentified) == []


def test_25pct_floor_exact_boundary_passes(world):
    cand = ready(world, a_amount="400")          # 0.25 * 400 == 100 == leg
    world.loop.propose(cand)
    assert world.loop.validate("c1").state is X.VALIDATED


def test_25pct_floor_just_below_reverts(world):
    cand = ready(world, a_amount="400.04")       # 0.25 * 400.04 = 100.01 > 100
    world.loop.propose(cand)
    exe = world.loop.validate("c1")
    assert exe.state is X.REVERTED and "25% floor" in exe.detail
    assert world.ledger.events.of_type(TargetDebitIdentified) == []
    assert len(world.ledger.events.of_type(TargetFlagEvaluated)) == 1


def test_floor_must_hold_for_every_target(world):
    world.add_nodes("A", "B", "C")
    world.add_solver("S1")
    now = world.clock.now()
    world.ledger.record_trade("A", "B", Decimal("400"), now - STALE)   # floor 100: ok
    world.ledger.record_trade("B", "C", Decimal("800"), now - STALE)   # floor 200: fails
    world.ledger.record_trade("C", "A", Decimal("100"), now)
    cand = triangle("c1", "S1", "100")
    world.loop.propose(cand)
    exe = world.loop.validate("c1")
    assert exe.state is X.REVERTED and "for B" in exe.detail


def test_insufficient_obligation_reverts(world):
    cand = ready(world, a_age=timedelta(0), amount="100")
    big = triangle("c2", "S1", "150")
    world.loop.propose(big)
    assert world.loop.validate("c2").state is X.REVERTED


@pytest.mark.parametrize("hops", [
    (Hop("A", "B", Decimal(1)), Hop("B", "C", Decimal(1))),                                   # not closed
    (Hop("A", "B", Decimal(1)), Hop("B", "C", Decimal(2)), Hop("C", "A", Decimal(1))),        # non-uniform
    (Hop("A", "B", Decimal(1)), Hop("B", "A", Decimal(1)), Hop("A", "B", Decimal(1))),        # repeated / broken
    (Hop("A", "B", Decimal(1)),),                                                             # too short
    (Hop("A", "B", Decimal(0)), Hop("B", "A", Decimal(0))),                                  # zero amount
])
def test_structural_validation(world, hops):
    world.add_nodes("A", "B", "C")
    with pytest.raises(ValidationError):
        world.loop.propose(CycleCandidate("bad", "S1", hops))


# --------------------------------------------------------------- happy path
def test_full_settlement_path(world):
    cand = ready(world)
    before = {k: n.current_balance for k, n in world.ledger.nodes.items()}
    start(world, cand)
    sign(world, cand, "A", "B", "C")
    assert world.loop.get("c1").state is X.SIGNED
    exe = world.loop.settle("c1")
    assert exe.state is X.SETTLED and exe.commit_result.status is CommitStatus.COMMITTED
    assert {k: n.current_balance for k, n in world.ledger.nodes.items()} == before
    assert world.ledger.outstanding("A", "B") == Decimal("300")
    assert world.ledger.total_balance() == 0
    (c,) = world.ledger.events.of_type(TargetDebitClearedIntact)
    assert c.node_id == "A"


def test_bad_signature_rejected_at_submit(world):
    cand = ready(world)
    start(world, cand)
    with pytest.raises(SignatureError):
        world.loop.submit_signature("c1", "A", b"\x00" * 32)
    with pytest.raises(ValidationError):
        world.loop.submit_signature("c1", "Z", b"")


# --------------------------------------------------------------- execution clock
def test_router_2h_window_boundary_then_rerun(world):
    cand = ready(world)
    start(world, cand)
    sign(world, cand, "A", "B")
    world.clock.advance(timedelta(hours=2))
    assert world.loop.tick("c1").state is X.AWAITING_SIGNATURES      # exactly 2h: still open
    world.clock.advance(timedelta(seconds=1))
    exe = world.loop.tick("c1")
    assert exe.state is X.RERUN_REQUESTED
    (req,) = world.committee.requests
    assert req.excluded == {"C"} and req.nodes == {"A", "B"}
    assert req.lineage_id == "c1" and req.rerun_depth == 1 and req.parent_candidate_id == "c1"
    assert [e.node_id for e in world.ledger.events.of_type(RouterDropped)] == ["C"]
    assert world.ledger.retry_count("A") == 0


def test_router_signature_at_exact_deadline_accepted_after_rejected(world):
    cand = ready(world)
    start(world, cand)
    world.clock.advance(timedelta(hours=2))
    sign(world, cand, "B")
    world.clock.advance(timedelta(seconds=1))
    with pytest.raises(SignatureWindowClosed):
        sign(world, cand, "C")
    sign(world, cand, "A")    # target still has its 12h window


def test_target_12h_timeout_drops_cycle_and_ages_balance():
    w = make_world(age_penalty=timedelta(days=3))
    cand = ready(w)
    start(w, cand)
    sign(w, cand, "B", "C")
    w.clock.advance(timedelta(hours=12))
    assert w.loop.tick("c1").state is X.AWAITING_SIGNATURES
    w.clock.advance(timedelta(seconds=1))
    exe = w.loop.tick("c1")
    assert exe.state is X.DROPPED
    assert w.ledger.retry_count("A") == 1
    (d,) = w.ledger.events.of_type(TargetDropped)
    assert (d.node_id, d.retry_count) == ("A", 1)
    lot = w.ledger.debit_lots("A")[0]
    assert lot.age_bump == timedelta(days=3)
    assert w.ledger.outstanding("A", "B") == Decimal("400")       # nothing cleared
    assert w.committee.requests == []


def test_target_rule_wins_when_both_miss(world):
    cand = ready(world)
    start(world, cand)
    world.clock.advance(timedelta(hours=12, seconds=1))
    exe = world.loop.tick("c1")
    assert exe.state is X.DROPPED
    assert world.committee.requests == []
    assert world.ledger.events.of_type(RouterDropped) == []


def test_rerun_settlement_is_not_cleared_intact(world):
    world.add_nodes("A", "B", "C", "D")
    world.add_solver("S1")
    now = world.clock.now()
    world.ledger.record_trade("A", "B", Decimal("400"), now - STALE)
    world.ledger.record_trade("B", "C", Decimal("100"), now)
    world.ledger.record_trade("C", "A", Decimal("100"), now)
    world.ledger.record_trade("B", "D", Decimal("100"), now)
    world.ledger.record_trade("D", "C", Decimal("100"), now)
    original = CycleCandidate("c1", "S1", (Hop("A", "B", Decimal(100)), Hop("B", "D", Decimal(100)),
                                          Hop("D", "C", Decimal(100)), Hop("C", "A", Decimal(100))))
    world.publish(original)
    start(world, original)
    sign(world, original, "A", "B", "C")
    world.clock.advance(timedelta(hours=2, seconds=1))
    assert world.loop.tick("c1").state is X.RERUN_REQUESTED
    rerun = CycleCandidate("c1-r1", "S1", triangle().hops, lineage_id="c1", rerun_depth=1)
    world.publish(rerun)
    start(world, rerun)
    sign(world, rerun, "A", "B", "C")
    assert world.loop.settle("c1-r1").state is X.SETTLED
    assert world.ledger.events.of_type(TargetDebitClearedIntact) == []
    assert {e.lineage_id for e in world.ledger.events.of_type(TargetDebitIdentified)} == {"c1"}


# --------------------------------------------------------------- illegal transitions
def test_illegal_transitions_raise(world):
    cand = ready(world)
    world.loop.propose(cand)
    with pytest.raises(IllegalTransition):
        world.loop.open_signature_window("c1")
    with pytest.raises(IllegalTransition):
        world.loop.tick("c1")
    with pytest.raises(IllegalTransition):
        world.loop.settle("c1")
    world.loop.validate("c1")
    with pytest.raises(IllegalTransition):
        world.loop.validate("c1")
    world.loop.open_signature_window("c1")
    with pytest.raises(IllegalTransition):
        world.loop.settle("c1")
    sign(world, cand, "A", "B", "C")
    with pytest.raises(IllegalTransition):
        world.loop.tick("c1")
    world.loop.settle("c1")
    for fn in (lambda: world.loop.settle("c1"),
               lambda: world.loop.submit_signature("c1", "A", b""),
               lambda: world.loop.force_revert("c1", "x"),
               lambda: world.loop.tick("c1")):
        with pytest.raises(IllegalTransition):
            fn()


def test_terminal_states_have_no_exits(world):
    cand = ready(world)
    start(world, cand)
    world.clock.advance(timedelta(hours=12, seconds=1))
    world.loop.tick("c1")
    with pytest.raises(IllegalTransition):
        world.loop.force_revert("c1", "late")
    with pytest.raises(ValidationError):
        world.loop.propose(cand)


def test_settle_reverts_when_block_never_published(world):
    world.add_nodes("A", "B", "C")
    world.add_solver("S1")
    seed_triangle(world)
    cand = triangle("unpub", "S1")
    start(world, cand)
    sign(world, cand, "A", "B", "C")
    exe = world.loop.settle("unpub")
    assert exe.state is X.REVERTED
    assert exe.commit_result.status is CommitStatus.REJECTED_NO_PUBLISHED_BLOCK
