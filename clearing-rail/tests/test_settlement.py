from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.crypto import (
    Ed25519Verifier,
    KeyRegistry,
    block_message,
    canonical_amount,
    cycle_hash,
    hop_message,
)
from clearing_rail.events import CycleReverted, CycleSettled, DeviationFlagged, SolverSlashed
from clearing_rail.ledger import Ledger
from clearing_rail.settlement import (
    CandidateBoard,
    CommitStatus as C,
    PublishedCandidateBlock,
    SettlementEngine,
    SettlementSubmission,
    SolverRegistry,
)
from clearing_rail.types import CycleCandidate, Hop, ManualClock, Node, ValidationError
from conftest import T0, hsign, make_world, secret_for, seed_triangle, triangle


def setup(world, bond="500"):
    world.add_nodes("A", "B", "C")
    world.add_solver("S1", bond)
    world.add_solver("S2", bond)
    seed_triangle(world, amount="100")
    cand = triangle("c1", "S1", "60")
    world.publish(cand)
    return cand


def test_matching_vector_commits(world):
    cand = setup(world)
    r = world.engine.commit(world.submission(cand))
    assert r.status is C.COMMITTED and r.committed
    assert world.ledger.outstanding("A", "B") == Decimal("40")
    assert world.ledger.total_balance() == 0
    assert len(world.ledger.events.of_type(CycleSettled)) == 1


def test_mismatch_from_publisher_reverts_ledger_and_slashes(world):
    cand = setup(world)
    before = world.ledger.state_fingerprint()
    tampered = tuple(Hop(h.debtor, h.creditor, Decimal("70")) for h in cand.hops)  # applies cleanly, then mismatches
    r = world.engine.commit(world.submission(cand, legs=tampered))
    assert r.status is C.REVERTED_SLASHED and r.slashed == Decimal("500")
    assert world.ledger.state_fingerprint() == before            # fully reverted
    acct = world.registry.account("S1")
    assert acct.bond == 0 and acct.slashed_total == Decimal("500")   # slash survives rollback
    assert not world.registry.is_registered("S1")
    assert [e.solver_id for e in world.ledger.events.of_type(SolverSlashed)] == ["S1"]
    assert world.ledger.events.of_type(CycleReverted)[0].candidate_id == "c1"


def test_reordered_legs_are_a_mismatch(world):
    cand = setup(world)
    rotated = cand.hops[1:] + cand.hops[:1]
    r = world.engine.commit(world.submission(cand, legs=rotated))
    assert r.status is C.REVERTED_SLASHED


def test_mismatch_that_also_breaks_ledger_still_slashes(world):
    cand = setup(world)
    before = world.ledger.state_fingerprint()
    too_big = tuple(Hop(h.debtor, h.creditor, Decimal("1000")) for h in cand.hops)
    r = world.engine.commit(world.submission(cand, legs=too_big))
    assert r.status is C.REVERTED_SLASHED
    assert world.ledger.state_fingerprint() == before


def test_partial_slash_fraction():
    w = make_world(slash_fraction=Decimal("0.25"))
    cand = setup(w, bond="400")
    tampered = tuple(Hop(h.debtor, h.creditor, Decimal("50")) for h in cand.hops)
    r = w.engine.commit(w.submission(cand, legs=tampered))
    assert r.slashed == Decimal("100")
    assert w.registry.account("S1").bond == Decimal("300")
    assert w.registry.is_registered("S1")


def test_non_publisher_mismatch_rejected_without_slash(world):
    cand = setup(world)
    before = world.ledger.state_fingerprint()
    tampered = tuple(Hop(h.debtor, h.creditor, Decimal("70")) for h in cand.hops)
    r = world.engine.commit(world.submission(cand, legs=tampered, submitter="S2"))
    assert r.status is C.REJECTED_NOT_PUBLISHER and r.slashed == 0
    assert world.registry.account("S1").bond == Decimal("500")
    assert world.registry.account("S2").bond == Decimal("500")
    assert world.ledger.state_fingerprint() == before
    assert world.ledger.events.of_type(SolverSlashed) == []
    assert "not the publishing solver" in world.ledger.events.of_type(DeviationFlagged)[0].reason
    # honest publisher can still settle afterwards
    assert world.engine.commit(world.submission(cand)).committed


def test_unregistered_solver_rejected(world):
    cand = setup(world)
    r = world.engine.commit(world.submission(cand, submitter="ROGUE"))
    assert r.status is C.REJECTED_UNREGISTERED_SOLVER
    assert world.ledger.events.of_type(SolverSlashed) == []


def test_unregistered_solver_cannot_publish(world):
    world.add_nodes("A", "B", "C")
    cand = triangle("c9", "ROGUE")
    with pytest.raises(ValidationError):
        world.publish(cand)


def test_bad_solver_block_signature_rejected(world):
    world.add_nodes("A", "B", "C")
    world.add_solver("S1")
    cand = triangle("c1", "S1")
    with pytest.raises(ValidationError):
        world.board.publish(PublishedCandidateBlock(cand, b"forged", T0))


def test_duplicate_publish_rejected(world):
    cand = setup(world)
    with pytest.raises(ValidationError):
        world.publish(cand)


def test_no_published_block_rejected(world):
    setup(world)
    other = triangle("never", "S1", "60")
    r = world.engine.commit(world.submission(other))
    assert r.status is C.REJECTED_NO_PUBLISHED_BLOCK


def test_bad_hop_signatures_rejected_without_slash(world):
    cand = setup(world)
    sigs = list(world.sign_legs(cand.candidate_id, "S1", cand.hops))
    sigs[1] = b"\x00" * 32
    r = world.engine.commit(world.submission(cand, sigs=tuple(sigs)))
    assert r.status is C.REJECTED_BAD_SIGNATURES
    r = world.engine.commit(world.submission(cand, sigs=tuple(sigs[:2])))
    assert r.status is C.REJECTED_BAD_SIGNATURES
    assert world.registry.account("S1").bond == Decimal("500")


def test_replay_after_commit_rejected(world):
    cand = setup(world)
    assert world.engine.commit(world.submission(cand)).committed
    assert world.engine.commit(world.submission(cand)).status is C.REJECTED_ALREADY_SETTLED
    assert world.ledger.outstanding("A", "B") == Decimal("40")


def test_matching_vector_but_ledger_cannot_apply_reverts_without_slash(world):
    cand = setup(world)
    world.ledger.clear_leg(Hop("B", "C", Decimal("50")))   # obligation shrank after publication
    before = world.ledger.state_fingerprint()
    r = world.engine.commit(world.submission(cand))
    assert r.status is C.REVERTED_LEDGER
    assert world.ledger.state_fingerprint() == before
    assert world.registry.account("S1").bond == Decimal("500")


def test_canonical_amount_encoding():
    assert canonical_amount(Decimal("10.0")) == canonical_amount(Decimal("1E+1")) == "10"
    h1 = (Hop("A", "B", Decimal("10.0")),)
    h2 = (Hop("A", "B", Decimal("10")),)
    assert cycle_hash("c", "s", h1) == cycle_hash("c", "s", h2)
    assert cycle_hash("c", "s", h1) != cycle_hash("c", "s2", h1)


def test_ed25519_end_to_end():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    def raw(pk):
        return pk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    clock = ManualClock(T0)
    ledger = Ledger()
    keys = KeyRegistry()
    priv = {}
    for n in ("A", "B", "C"):
        ledger.add_node(Node(n, Decimal(1000)))
        priv[n] = Ed25519PrivateKey.generate()
        keys.register(n, raw(priv[n]))
    v = Ed25519Verifier()
    reg = SolverRegistry()
    spriv = Ed25519PrivateKey.generate()
    reg.register("S1", Decimal(100), raw(spriv))
    board = CandidateBoard(reg, v)
    eng = SettlementEngine(ledger, reg, board, keys, v, clock)
    for a, b in (("A", "B"), ("B", "C"), ("C", "A")):
        ledger.record_trade(a, b, Decimal(10), T0)
    cand = triangle("e1", "S1", "10")
    ch = cycle_hash("e1", "S1", cand.hops)
    board.publish(PublishedCandidateBlock(cand, spriv.sign(block_message(ch)), T0))
    sigs = tuple(priv[h.debtor].sign(hop_message(ch, i, h)) for i, h in enumerate(cand.hops))
    assert eng.commit(SettlementSubmission("e1", "S1", cand.hops, sigs)).committed
    assert not v.verify(b"\x00" * 32, b"m", b"\x00" * 64)
