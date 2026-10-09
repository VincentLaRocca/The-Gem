"""Settlement Exclusivity (Slash Condition).

A cycle may only settle against a PublishedCandidateBlock from a registered
Solver. The submitted n-hop signature array is checked hop by hop, then the
settlement vector is compared with the published vector inside an atomic
ledger transaction. On mismatch the ledger is fully reverted and the solver's
stake is slashed; the slash lives outside the ledger, so it survives the
rollback.

This is a reference ledger, NOT a deployed smart contract (no chain was
specified). See INTERFACES.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple

from .crypto import KeyRegistry, Verifier, block_message, cycle_hash, hop_message
from .events import CycleReverted, CycleSettled, DeviationFlagged, SolverSlashed
from .ledger import Ledger
from .types import ZERO, Clock, CycleCandidate, D, Hop, LedgerError, ValidationError


# FIX (hardening): smallest bond a solver may register with or keep operating on.
# A slash that leaves the bond below this deregisters the solver.
MIN_SOLVER_BOND = Decimal("100")


# ------------------------------------------------------------------ structural rules
def validate_cycle_structure(candidate: CycleCandidate) -> None:
    hops = candidate.hops
    if len(hops) < 2:
        raise ValidationError("a cycle needs at least 2 hops")
    for h in hops:
        if h.amount <= 0:
            raise ValidationError("hop amounts must be > 0")
        if h.debtor == h.creditor:
            raise ValidationError("self-hop")
    for i, h in enumerate(hops):
        nxt = hops[(i + 1) % len(hops)]
        if h.creditor != nxt.debtor:
            raise ValidationError(f"hop {i} creditor {h.creditor} != hop {i+1} debtor {nxt.debtor}")
    if len(set(candidate.nodes)) != len(hops):
        raise ValidationError("not a simple cycle (node repeated)")
    if len({h.amount for h in hops}) != 1:
        raise ValidationError("non-uniform hop amounts would move net balances; clearing must be uniform")


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class SettlementVector:
    cycle_hash: bytes
    legs: Tuple[Hop, ...]

    @classmethod
    def build(cls, candidate_id: str, solver_id: str, legs) -> "SettlementVector":
        legs = tuple(legs)
        return cls(cycle_hash(candidate_id, solver_id, legs), legs)

    def matches(self, other: "SettlementVector") -> bool:
        """Same ordered nodes, same amounts, same cycle hash."""
        return (
            self.cycle_hash == other.cycle_hash
            and len(self.legs) == len(other.legs)
            and all(a.debtor == b.debtor and a.creditor == b.creditor and a.amount == b.amount
                    for a, b in zip(self.legs, other.legs))
        )


@dataclass(frozen=True)
class PublishedCandidateBlock:
    candidate: CycleCandidate
    solver_signature: bytes
    published_at: datetime

    @property
    def solver_id(self) -> str:
        return self.candidate.solver_id

    @property
    def published_vector(self) -> SettlementVector:
        c = self.candidate
        return SettlementVector.build(c.candidate_id, c.solver_id, c.hops)


@dataclass(frozen=True)
class SettlementSubmission:
    candidate_id: str
    submitter_id: str
    legs: Tuple[Hop, ...]
    signatures: Tuple[bytes, ...]

    @property
    def settlement_vector(self) -> SettlementVector:
        return SettlementVector.build(self.candidate_id, self.submitter_id, self.legs)


class CommitStatus(str, Enum):
    COMMITTED = "committed"
    REVERTED_SLASHED = "reverted_slashed"
    REVERTED_LEDGER = "reverted_ledger"
    REJECTED_UNREGISTERED_SOLVER = "rejected_unregistered_solver"
    REJECTED_NO_PUBLISHED_BLOCK = "rejected_no_published_block"
    REJECTED_NOT_PUBLISHER = "rejected_not_publisher"
    REJECTED_BAD_SIGNATURES = "rejected_bad_signatures"
    REJECTED_ALREADY_SETTLED = "rejected_already_settled"


@dataclass(frozen=True)
class CommitResult:
    status: CommitStatus
    candidate_id: str
    slashed: Decimal = ZERO
    detail: str = ""

    @property
    def committed(self) -> bool:
        return self.status is CommitStatus.COMMITTED


@dataclass
class SolverAccount:
    solver_id: str
    bond: Decimal
    slashed_total: Decimal = ZERO

    @property
    def active(self) -> bool:
        return self.bond >= MIN_SOLVER_BOND


# ------------------------------------------------------------------ registries
class SolverRegistry:
    """Solver bonds. Deliberately separate from the Ledger so slashes survive ledger rollbacks."""

    def __init__(self, keys: Optional[KeyRegistry] = None):
        self.keys = keys if keys is not None else KeyRegistry()
        self._accounts: Dict[str, SolverAccount] = {}

    def register(self, solver_id: str, bond, public_key: bytes) -> SolverAccount:
        bond = D(bond)
        if bond < MIN_SOLVER_BOND:
            raise ValidationError(f"solver bond must be >= MIN_SOLVER_BOND ({MIN_SOLVER_BOND})")
        if solver_id in self._accounts:
            raise ValidationError(f"solver {solver_id} already registered")
        self.keys.register(solver_id, public_key)
        acct = SolverAccount(solver_id, bond)
        self._accounts[solver_id] = acct
        return acct

    def is_registered(self, solver_id: str) -> bool:
        a = self._accounts.get(solver_id)
        return a is not None and a.active

    def account(self, solver_id: str) -> SolverAccount:
        return self._accounts[solver_id]

    def slash_stake(self, solver_id: str, fraction: Decimal = Decimal(1)) -> Decimal:
        acct = self._accounts[solver_id]
        amount = acct.bond * D(fraction)
        acct.bond -= amount
        acct.slashed_total += amount
        return amount


class CandidateBoard:
    """Where registered solvers publish candidate blocks (the solver committee's output)."""

    def __init__(self, registry: SolverRegistry, verifier: Verifier):
        self.registry = registry
        self.verifier = verifier
        self._blocks: Dict[str, PublishedCandidateBlock] = {}

    def publish(self, block: PublishedCandidateBlock) -> PublishedCandidateBlock:
        c = block.candidate
        if not self.registry.is_registered(c.solver_id):
            raise ValidationError(f"solver {c.solver_id} is not registered")
        validate_cycle_structure(c)
        if c.candidate_id in self._blocks:
            raise ValidationError(f"candidate {c.candidate_id} already published")
        key = self.registry.keys.get(c.solver_id)
        if not self.verifier.verify(key, block_message(block.published_vector.cycle_hash), block.solver_signature):
            raise ValidationError("bad solver signature on candidate block")
        self._blocks[c.candidate_id] = block
        return block

    def get(self, candidate_id: str) -> Optional[PublishedCandidateBlock]:
        return self._blocks.get(candidate_id)


# ------------------------------------------------------------------ engine
class SettlementEngine:
    def __init__(self, ledger: Ledger, registry: SolverRegistry, board: CandidateBoard,
                 node_keys: KeyRegistry, verifier: Verifier, clock: Clock,
                 slash_fraction: Decimal = Decimal(1)):
        self.ledger, self.registry, self.board = ledger, registry, board
        self.node_keys, self.verifier, self.clock = node_keys, verifier, clock
        self.slash_fraction = D(slash_fraction)
        self._settled: set = set()

    def _signatures_ok(self, sub: SettlementSubmission) -> bool:
        if len(sub.signatures) != len(sub.legs) or not sub.legs:
            return False
        chash = sub.settlement_vector.cycle_hash
        for i, (leg, sig) in enumerate(zip(sub.legs, sub.signatures)):
            key = self.node_keys.get(leg.debtor)
            if key is None or not self.verifier.verify(key, hop_message(chash, i, leg), sig):
                return False
        return True

    def commit(self, sub: SettlementSubmission) -> CommitResult:
        now = self.clock.now()
        ev = self.ledger.events
        if not self.registry.is_registered(sub.submitter_id):
            ev.append(DeviationFlagged(now, sub.candidate_id, sub.submitter_id, "unregistered solver"))
            return CommitResult(CommitStatus.REJECTED_UNREGISTERED_SOLVER, sub.candidate_id)
        block = self.board.get(sub.candidate_id)
        if block is None:
            ev.append(DeviationFlagged(now, sub.candidate_id, sub.submitter_id, "no published block"))
            return CommitResult(CommitStatus.REJECTED_NO_PUBLISHED_BLOCK, sub.candidate_id)
        if sub.submitter_id != block.solver_id:
            # never slash the honest publisher because someone else submitted garbage
            ev.append(DeviationFlagged(now, sub.candidate_id, sub.submitter_id, "submitter is not the publishing solver"))
            return CommitResult(CommitStatus.REJECTED_NOT_PUBLISHER, sub.candidate_id)
        if sub.candidate_id in self._settled:
            return CommitResult(CommitStatus.REJECTED_ALREADY_SETTLED, sub.candidate_id)
        if not self._signatures_ok(sub):
            ev.append(DeviationFlagged(now, sub.candidate_id, sub.submitter_id, "signature array invalid"))
            return CommitResult(CommitStatus.REJECTED_BAD_SIGNATURES, sub.candidate_id)

        matches = sub.settlement_vector.matches(block.published_vector)
        try:
            with self.ledger.transaction():
                self.ledger.apply_cycle(sub.legs, now)
                if not matches:
                    raise _VectorMismatch()
        except _VectorMismatch:
            return self._slash(block, now, "settlement_vector != published_vector")
        except LedgerError as e:
            if not matches:
                return self._slash(block, now, f"settlement_vector != published_vector ({e})")
            ev.append(CycleReverted(now, sub.candidate_id, block.solver_id, f"ledger: {e}"))
            return CommitResult(CommitStatus.REVERTED_LEDGER, sub.candidate_id, detail=str(e))

        self._settled.add(sub.candidate_id)
        ev.append(CycleSettled(now, sub.candidate_id, block.solver_id, block.candidate.notional_clearance))
        return CommitResult(CommitStatus.COMMITTED, sub.candidate_id)

    def _slash(self, block: PublishedCandidateBlock, now: datetime, reason: str) -> CommitResult:
        amount = self.registry.slash_stake(block.solver_id, self.slash_fraction)
        ev = self.ledger.events
        ev.append(CycleReverted(now, block.candidate.candidate_id, block.solver_id, reason))
        ev.append(SolverSlashed(now, block.solver_id, block.candidate.candidate_id, amount))
        return CommitResult(CommitStatus.REVERTED_SLASHED, block.candidate.candidate_id, amount, reason)


class _VectorMismatch(Exception):
    pass
