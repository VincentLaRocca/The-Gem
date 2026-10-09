"""Append-only event log. Telemetry is computed purely from these events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Iterator, List, Optional, Tuple, Type, TypeVar

from .types import Initiator


@dataclass(frozen=True)
class Event:
    ts: datetime


@dataclass(frozen=True)
class TradeConfirmed(Event):
    """A confirmed offer posted to the ledger. Posts exactly one debit leg."""

    trade_id: int
    debtor: str
    creditor: str
    amount: Decimal
    initiated_by: Initiator


@dataclass(frozen=True)
class StakeReleased(Event):
    voucher: str
    vouchee: str
    amount: Decimal
    via_outside_volume: bool
    counterparty: Optional[str] = None
    reason: str = ""
    # FIX (telemetry): matured outbound transfers that backed this release, as
    # (transfer_id, amount) pairs, and the engine's release id (for clawbacks).
    backing: Tuple[Tuple[int, Decimal], ...] = ()
    release_id: Optional[int] = None


@dataclass(frozen=True)
class StakeClawedBack(Event):
    """FIX (S1): a released amount re-locked because a later check found a wash."""

    voucher: str
    vouchee: str
    amount: Decimal
    release_id: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class RepeatWithholderFlagged(Event):
    """FIX (S4): a router reached ROUTER_MISS_THRESHOLD signature misses (across lineages)."""

    candidate_id: str
    node_id: str
    misses: int
    barred_until: datetime


@dataclass(frozen=True)
class TargetFlagEvaluated(Event):
    """Logged for EVERY stale node seen in a candidate, boosted or not."""

    candidate_id: str
    node_id: str
    stale_balance: Decimal
    prior_boosts: int
    accepted: bool


@dataclass(frozen=True)
class TargetDebitIdentified(Event):
    lineage_id: str
    candidate_id: str
    node_id: str
    creditor: str
    amount: Decimal


@dataclass(frozen=True)
class TargetDebitClearedIntact(Event):
    lineage_id: str
    candidate_id: str
    node_id: str
    amount: Decimal


@dataclass(frozen=True)
class ExecutionTransition(Event):
    candidate_id: str
    from_state: str
    to_state: str
    detail: str = ""


@dataclass(frozen=True)
class RouterDropped(Event):
    candidate_id: str
    node_id: str


@dataclass(frozen=True)
class TargetDropped(Event):
    candidate_id: str
    node_id: str
    retry_count: int


@dataclass(frozen=True)
class CycleSettled(Event):
    candidate_id: str
    solver_id: str
    notional: Decimal


@dataclass(frozen=True)
class CycleReverted(Event):
    candidate_id: str
    solver_id: str
    reason: str


@dataclass(frozen=True)
class SolverSlashed(Event):
    solver_id: str
    candidate_id: str
    amount: Decimal


@dataclass(frozen=True)
class DeviationFlagged(Event):
    candidate_id: str
    submitter_id: str
    reason: str


E = TypeVar("E", bound=Event)


class EventLog:
    def __init__(self, events: Iterable[Event] = ()):
        self._events: List[Event] = list(events)

    def append(self, event: Event) -> Event:
        self._events.append(event)
        return event

    def __iter__(self) -> Iterator[Event]:
        return iter(list(self._events))

    def __len__(self) -> int:
        return len(self._events)

    def of_type(self, cls: Type[E]) -> List[E]:
        return [e for e in self._events if isinstance(e, cls)]
