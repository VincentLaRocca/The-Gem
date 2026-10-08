"""Solver Liveness & Debtor Retry State Machine.

The Solver committee finds cycles (that search is an INTERFACE — see
INTERFACES.md). This module only validates published candidates and drives
the execution clock.

State machine
-------------
::

    PROPOSED -> VALIDATED -> AWAITING_SIGNATURES
                                 |-> SIGNED -> SETTLED
                                 |-> ROUTER_TIMEOUT -> RERUN_REQUESTED
                                 |-> TARGET_TIMEOUT -> DROPPED
                                 |-> REVERTED

If a target and a router both miss inside the same tick, the target rule wins.
``drop_node_and_rerun`` excludes the late router and hands the residual graph
back to the Solver committee through :class:`SolverCommittee`. Core never
searches for a replacement cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Protocol, Set, Tuple

from .events import (
    ExecutionTransition,
    RouterDropped,
    TargetDebitClearedIntact,
    TargetDebitIdentified,
    TargetDropped,
    TargetFlagEvaluated,
)
from .crypto import cycle_hash, hop_message
from .ledger import Ledger
from .settlement import (
    CandidateBoard,
    CommitResult,
    CommitStatus,
    SettlementEngine,
    SettlementSubmission,
    validate_cycle_structure,
)
from .types import (
    ZERO,
    Clock,
    CycleCandidate,
    Hop,
    IllegalTransition,
    SignatureError,
    SignatureWindowClosed,
    ValidationError,
)

STALE_THRESHOLD = timedelta(days=90)
ROUTER_WINDOW = timedelta(hours=2)
TARGET_WINDOW = timedelta(hours=12)
FLOOR_RATIO = Decimal("0.25")
AGE_PENALTY = timedelta(0)   # stub; see INTERFACES.md


class ExecutionState(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    AWAITING_SIGNATURES = "AWAITING_SIGNATURES"
    SIGNED = "SIGNED"
    SETTLED = "SETTLED"
    ROUTER_TIMEOUT = "ROUTER_TIMEOUT"
    RERUN_REQUESTED = "RERUN_REQUESTED"
    TARGET_TIMEOUT = "TARGET_TIMEOUT"
    DROPPED = "DROPPED"
    REVERTED = "REVERTED"


# Legal edges of the state machine. Anything else raises IllegalTransition.
_TRANSITIONS = {
    ExecutionState.PROPOSED: {ExecutionState.VALIDATED, ExecutionState.REVERTED},
    ExecutionState.VALIDATED: {ExecutionState.AWAITING_SIGNATURES, ExecutionState.REVERTED},
    ExecutionState.AWAITING_SIGNATURES: {
        ExecutionState.SIGNED,
        ExecutionState.ROUTER_TIMEOUT,
        ExecutionState.TARGET_TIMEOUT,
        ExecutionState.REVERTED,
    },
    ExecutionState.SIGNED: {ExecutionState.SETTLED, ExecutionState.REVERTED},
    ExecutionState.ROUTER_TIMEOUT: {ExecutionState.RERUN_REQUESTED},
    ExecutionState.TARGET_TIMEOUT: {ExecutionState.DROPPED},
}


class SolverCommittee(Protocol):
    """INTERFACE: find a replacement cycle on the residual graph after a router drop."""

    def request_rerun(self, residual: "ResidualGraph") -> Optional[CycleCandidate]: ...


@dataclass(frozen=True)
class ResidualGraph:
    lineage_id: str
    nodes: FrozenSet[str]
    excluded: FrozenSet[str]
    parent_candidate_id: str
    rerun_depth: int


@dataclass(frozen=True)
class CompressionTarget:
    node_id: str
    stale_balance: Decimal
    debit_leg_amount: Decimal
    hop_index: int


@dataclass
class Execution:
    candidate: CycleCandidate
    state: ExecutionState
    opened_at: datetime
    targets: Tuple[CompressionTarget, ...] = ()
    signatures: Dict[str, bytes] = field(default_factory=dict)
    residual: Optional[ResidualGraph] = None
    commit_result: Optional[CommitResult] = None
    detail: str = ""

    @property
    def candidate_id(self) -> str:
        return self.candidate.candidate_id

    @property
    def signed(self) -> FrozenSet[str]:
        return frozenset(self.signatures)

    @property
    def target_ids(self) -> FrozenSet[str]:
        return frozenset(t.node_id for t in self.targets)

    @property
    def router_ids(self) -> FrozenSet[str]:
        return frozenset(self.candidate.nodes) - self.target_ids

    def window_for(self, node_id: str) -> timedelta:
        return TARGET_WINDOW if node_id in self.target_ids else ROUTER_WINDOW


# ------------------------------------------------------------------ pure helpers
def evaluate_targets(
    candidate: CycleCandidate,
    ledger: Ledger,
    as_of: datetime,
    threshold: timedelta = STALE_THRESHOLD,
) -> Tuple[Tuple[CompressionTarget, ...], List[TargetFlagEvaluated]]:
    """Evaluate every node whose debit balance is older than ``threshold``.

    Spec contradiction (called out in README): the original text requires
    ``urgency_boosts_used == 0`` to BE a CompressionTarget, which would make
    ``distress_discipline`` always 0. Resolution: every stale-node evaluation
    is logged; a node with prior boosts is flagged-but-rejected and does NOT
    receive the 12-hour target treatment.
    """
    flags: List[TargetFlagEvaluated] = []
    accepted: List[CompressionTarget] = []
    for i, hop in enumerate(candidate.hops):
        node = ledger.node(hop.debtor)
        stale = ledger.stale_balance(node.node_id, as_of, threshold)
        if stale <= 0:
            continue
        prior = node.urgency_boosts_used
        ok = prior == 0
        flags.append(TargetFlagEvaluated(as_of, candidate.candidate_id, node.node_id, stale, prior, ok))
        if ok:
            accepted.append(CompressionTarget(node.node_id, stale, hop.amount, i))
    return tuple(accepted), flags


def check_floor(targets: Tuple[CompressionTarget, ...], candidate: CycleCandidate) -> Optional[str]:
    """25% floor must hold for EVERY CompressionTarget. Uses the amount on the
    target's own debit leg (spec: 'the amount on the target's own debit leg'
    when per-hop amounts are present; hop amounts are uniform so this equals
    ``notional_clearance``)."""
    for t in targets:
        if t.debit_leg_amount < FLOOR_RATIO * t.stale_balance:
            return (f"25% floor failed for {t.node_id}: "
                    f"leg {t.debit_leg_amount} < 0.25 * stale {t.stale_balance}")
    return None


# ------------------------------------------------------------------ engine
class ClearingLoop:
    def __init__(
        self,
        ledger: Ledger,
        settlement: SettlementEngine,
        board: CandidateBoard,
        committee: SolverCommittee,
        clock: Clock,
        stale_threshold: timedelta = STALE_THRESHOLD,
        age_penalty: timedelta = AGE_PENALTY,
    ):
        self.ledger = ledger
        self.settlement = settlement
        self.board = board
        self.committee = committee
        self.clock = clock
        self.stale_threshold = stale_threshold
        self.age_penalty = age_penalty
        self._executions: Dict[str, Execution] = {}

    def get(self, candidate_id: str) -> Execution:
        return self._executions[candidate_id]

    # ---- transitions --------------------------------------------------------
    def _go(self, exe: Execution, to: ExecutionState, detail: str = "") -> None:
        allowed = _TRANSITIONS.get(exe.state, set())
        if to not in allowed:
            raise IllegalTransition(f"{exe.state.value} -> {to.value}")
        prev = exe.state
        exe.state = to
        exe.detail = detail
        self.ledger.events.append(
            ExecutionTransition(self.clock.now(), exe.candidate_id, prev.value, to.value, detail)
        )

    # ---- public API ---------------------------------------------------------
    def propose(self, candidate: CycleCandidate) -> Execution:
        now = self.clock.now()
        if candidate.candidate_id in self._executions:
            raise ValidationError(f"candidate {candidate.candidate_id} already proposed")
        validate_cycle_structure(candidate)
        for n in candidate.nodes:
            self.ledger.node(n)  # must exist
        exe = Execution(candidate, ExecutionState.PROPOSED, now)
        self._executions[candidate.candidate_id] = exe
        return exe

    def validate(self, candidate_id: str) -> Execution:
        exe = self.get(candidate_id)
        if exe.state is not ExecutionState.PROPOSED:
            raise IllegalTransition(f"validate from {exe.state.value}")
        now = self.clock.now()
        targets, flags = evaluate_targets(exe.candidate, self.ledger, now, self.stale_threshold)
        for f in flags:
            self.ledger.events.append(f)
        fail = check_floor(targets, exe.candidate)
        if fail:
            self._go(exe, ExecutionState.REVERTED, fail)
            return exe
        # every hop must have an obligation of at least its amount
        for hop in exe.candidate.hops:
            if self.ledger.outstanding(hop.debtor, hop.creditor) < hop.amount:
                self._go(exe, ExecutionState.REVERTED,
                         f"insufficient obligation {hop.debtor}->{hop.creditor}")
                return exe
        exe.targets = targets
        for t in targets:
            hop = exe.candidate.hops[t.hop_index]
            self.ledger.events.append(
                TargetDebitIdentified(now, exe.candidate.lineage_id, exe.candidate_id,
                                      t.node_id, hop.creditor, t.debit_leg_amount)
            )
        self._go(exe, ExecutionState.VALIDATED)
        return exe

    def open_signature_window(self, candidate_id: str) -> Execution:
        exe = self.get(candidate_id)
        if exe.state is not ExecutionState.VALIDATED:
            raise IllegalTransition(f"open_signature_window from {exe.state.value}")
        # publishing is the Solver's job; if the block is not on the board yet,
        # we still open the window — settlement.commit will reject later.
        self._go(exe, ExecutionState.AWAITING_SIGNATURES)
        return exe

    def submit_signature(self, candidate_id: str, node_id: str, signature: bytes) -> Execution:
        """Record ``node_id``'s signature over its own debit leg (verified now and again at commit)."""
        exe = self.get(candidate_id)
        if exe.state is not ExecutionState.AWAITING_SIGNATURES:
            raise IllegalTransition(f"submit_signature from {exe.state.value}")
        if node_id not in exe.candidate.nodes:
            raise ValidationError(f"{node_id} is not on the cycle")
        deadline = exe.opened_at + exe.window_for(node_id)
        if self.clock.now() > deadline:
            raise SignatureWindowClosed(f"{node_id} window closed at {deadline.isoformat()}")
        idx, hop = exe.candidate.debit_leg(node_id)
        chash = cycle_hash(exe.candidate.candidate_id, exe.candidate.solver_id, exe.candidate.hops)
        key = self.settlement.node_keys.get(node_id)
        if key is None or not self.settlement.verifier.verify(key, hop_message(chash, idx, hop), signature):
            raise SignatureError(f"bad signature from {node_id}")
        exe.signatures[node_id] = bytes(signature)
        if exe.signed >= set(exe.candidate.nodes):
            self._go(exe, ExecutionState.SIGNED)
        return exe

    def tick(self, candidate_id: str) -> Execution:
        """Advance the execution clock. Call after time has moved on the injected Clock."""
        exe = self.get(candidate_id)
        if exe.state is not ExecutionState.AWAITING_SIGNATURES:
            raise IllegalTransition(f"tick from {exe.state.value}")
        now = self.clock.now()
        late_targets = [n for n in exe.target_ids if n not in exe.signed and now > exe.opened_at + TARGET_WINDOW]
        late_routers = [n for n in exe.router_ids if n not in exe.signed and now > exe.opened_at + ROUTER_WINDOW]
        # target rule wins if both miss
        if late_targets:
            self._go(exe, ExecutionState.TARGET_TIMEOUT, f"late targets: {sorted(late_targets)}")
            return self._drop_cycle_and_age(exe, late_targets)
        if late_routers:
            self._go(exe, ExecutionState.ROUTER_TIMEOUT, f"late routers: {sorted(late_routers)}")
            return self._drop_node_and_rerun(exe, late_routers[0])
        return exe

    def settle(self, candidate_id: str, submitter_id: Optional[str] = None) -> Execution:
        exe = self.get(candidate_id)
        if exe.state is not ExecutionState.SIGNED:
            raise IllegalTransition(f"settle from {exe.state.value}")
        sub = SettlementSubmission(
            candidate_id=exe.candidate_id,
            submitter_id=submitter_id or exe.candidate.solver_id,
            legs=exe.candidate.hops,
            signatures=tuple(exe.signatures[h.debtor] for h in exe.candidate.hops),
        )
        result = self.settlement.commit(sub)
        exe.commit_result = result
        if result.status is CommitStatus.COMMITTED:
            self._go(exe, ExecutionState.SETTLED)
            # "intact" = settled in a single cycle, never dropped/rerun
            for t in (exe.targets if exe.candidate.rerun_depth == 0 else ()):
                self.ledger.events.append(
                    TargetDebitClearedIntact(
                        self.clock.now(), exe.candidate.lineage_id, exe.candidate_id,
                        t.node_id, t.debit_leg_amount,
                    )
                )
        else:
            self._go(exe, ExecutionState.REVERTED, result.detail or result.status.value)
        return exe

    def force_revert(self, candidate_id: str, reason: str) -> Execution:
        """Explicit revert from any non-terminal state that allows it."""
        exe = self.get(candidate_id)
        if ExecutionState.REVERTED not in _TRANSITIONS.get(exe.state, set()):
            raise IllegalTransition(f"force_revert from {exe.state.value}")
        self._go(exe, ExecutionState.REVERTED, reason)
        return exe

    # ---- private outcomes ---------------------------------------------------
    def _drop_cycle_and_age(self, exe: Execution, late_targets: List[str]) -> Execution:
        for n in late_targets:
            retry = self.ledger.age_balance(n, self.age_penalty)
            self.ledger.events.append(TargetDropped(self.clock.now(), exe.candidate_id, n, retry))
        self._go(exe, ExecutionState.DROPPED, f"aged: {sorted(late_targets)}")
        return exe

    def _drop_node_and_rerun(self, exe: Execution, late_router: str) -> Execution:
        self.ledger.events.append(RouterDropped(self.clock.now(), exe.candidate_id, late_router))
        residual = ResidualGraph(
            lineage_id=exe.candidate.lineage_id,
            nodes=frozenset(n for n in exe.candidate.nodes if n != late_router),
            excluded=frozenset({late_router}),
            parent_candidate_id=exe.candidate_id,
            rerun_depth=exe.candidate.rerun_depth + 1,
        )
        exe.residual = residual
        # INTERFACE call — committee may return None; core does not search.
        self.committee.request_rerun(residual)
        self._go(exe, ExecutionState.RERUN_REQUESTED, f"excluded {late_router}")
        return exe
