import hashlib
import hmac
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clearing_rail.crypto import KeyRegistry, block_message, cycle_hash, hop_message  # noqa: E402
from clearing_rail.ledger import Ledger  # noqa: E402
from clearing_rail.settlement import (  # noqa: E402
    CandidateBoard,
    PublishedCandidateBlock,
    SettlementEngine,
    SettlementSubmission,
    SolverRegistry,
)
from clearing_rail.solver import ClearingLoop, ResidualGraph  # noqa: E402
from clearing_rail.types import CycleCandidate, Hop, ManualClock, Node  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class HmacVerifier:
    """TEST DOUBLE. 'public key' == secret. Never use outside tests."""

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        if public_key is None:
            return False
        return hmac.compare_digest(hmac.new(public_key, message, hashlib.sha256).digest(), signature)


def hsign(secret: bytes, message: bytes) -> bytes:
    return hmac.new(secret, message, hashlib.sha256).digest()


def secret_for(entity: str) -> bytes:
    return hashlib.sha256(b"test-secret:" + entity.encode()).digest()


class RecordingCommittee:
    """TEST DOUBLE solver committee: records residual graphs, never searches."""

    def __init__(self):
        self.requests: List[ResidualGraph] = []

    def request_rerun(self, residual):
        self.requests.append(residual)
        return None


@dataclass
class World:
    clock: ManualClock
    ledger: Ledger
    node_keys: KeyRegistry
    registry: SolverRegistry
    board: CandidateBoard
    engine: SettlementEngine
    committee: RecordingCommittee
    loop: ClearingLoop
    verifier: HmacVerifier

    def add_nodes(self, *ids, ceiling="1000"):
        for i in ids:
            self.ledger.add_node(Node(i, Decimal(ceiling)))
            self.node_keys.register(i, secret_for(i))

    def add_solver(self, sid, bond="500"):
        self.registry.register(sid, Decimal(bond), secret_for(sid))

    def publish(self, cand: CycleCandidate) -> PublishedCandidateBlock:
        chash = cycle_hash(cand.candidate_id, cand.solver_id, cand.hops)
        block = PublishedCandidateBlock(cand, hsign(secret_for(cand.solver_id), block_message(chash)), self.clock.now())
        return self.board.publish(block)

    def sign_legs(self, candidate_id, solver_id, legs):
        chash = cycle_hash(candidate_id, solver_id, legs)
        return tuple(hsign(secret_for(l.debtor), hop_message(chash, i, l)) for i, l in enumerate(legs))

    def submission(self, cand: CycleCandidate, legs=None, submitter=None, sigs=None) -> SettlementSubmission:
        legs = tuple(legs if legs is not None else cand.hops)
        submitter = submitter or cand.solver_id
        if sigs is None:
            sigs = self.sign_legs(cand.candidate_id, submitter, legs)
        return SettlementSubmission(cand.candidate_id, submitter, legs, sigs)


def make_world(start=T0, age_penalty=timedelta(0), slash_fraction=Decimal(1)) -> World:
    clock = ManualClock(start)
    ledger = Ledger()
    node_keys = KeyRegistry()
    verifier = HmacVerifier()
    registry = SolverRegistry()
    board = CandidateBoard(registry, verifier)
    engine = SettlementEngine(ledger, registry, board, node_keys, verifier, clock, slash_fraction)
    committee = RecordingCommittee()
    loop = ClearingLoop(ledger, engine, board, committee, clock, age_penalty=age_penalty)
    return World(clock, ledger, node_keys, registry, board, engine, committee, loop, verifier)


@pytest.fixture
def world():
    return make_world()


def triangle(cid="c1", solver="S1", amount="100", nodes=("A", "B", "C")):
    a, b, c = nodes
    return CycleCandidate(cid, solver, (Hop(a, b, Decimal(amount)), Hop(b, c, Decimal(amount)), Hop(c, a, Decimal(amount))))


def seed_triangle(w: World, amount="100", a_age=timedelta(0), others_age=timedelta(0), a_amount=None):
    """A owes B, B owes C, C owes A. A's lot originates ``a_age`` before now."""
    now = w.clock.now()
    w.ledger.record_trade("A", "B", Decimal(a_amount or amount), now - a_age)
    w.ledger.record_trade("B", "C", Decimal(amount), now - others_age)
    w.ledger.record_trade("C", "A", Decimal(amount), now - others_age)
