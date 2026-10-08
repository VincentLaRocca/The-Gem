"""Core data structures, clock, and errors for the Edge-Native Clearing Rail.

All money is ``decimal.Decimal``. Floats are refused at the boundary.
No function in this package reads the wall clock: time always comes from an
injected :class:`Clock` or an explicit ``as_of`` / ``ts`` argument.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional, Protocol, Tuple

ZERO = Decimal(0)


def D(value) -> Decimal:
    """Coerce to Decimal. Floats are rejected (they are not exact)."""
    if isinstance(value, bool):
        raise TypeError("bool is not a money amount")
    if isinstance(value, float):
        raise TypeError("floats are not accepted for money; pass str, int or Decimal")
    d = value if isinstance(value, Decimal) else Decimal(value)
    if not d.is_finite():
        raise ValueError(f"non-finite amount: {value!r}")
    return d


# --------------------------------------------------------------------------- clock
class Clock(Protocol):
    def now(self) -> datetime: ...


class ManualClock:
    """Deterministic clock. Time only moves when a caller moves it."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("clock start must be timezone-aware")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("clock cannot move backwards")
        self._now = self._now + delta
        return self._now

    def set(self, when: datetime) -> datetime:
        if when < self._now:
            raise ValueError("clock cannot move backwards")
        self._now = when
        return self._now


# --------------------------------------------------------------------------- errors
class ClearingRailError(Exception):
    """Base error."""


class ValidationError(ClearingRailError):
    pass


class CreditLimitExceeded(ClearingRailError):
    pass


class LedgerError(ClearingRailError):
    pass


class IllegalTransition(ClearingRailError):
    pass


class SignatureError(ClearingRailError):
    pass


class SignatureWindowClosed(ClearingRailError):
    pass


# --------------------------------------------------------------------------- enums
class Initiator(str, Enum):
    """Who originated a confirmed offer."""

    AGENT = "agent"   # the operator's edge agent proposed it, the human confirmed
    HUMAN = "human"   # the human operator proposed it directly


# --------------------------------------------------------------------------- primitives
@dataclass
class Node:
    """A human operator plus their edge agent.

    ``current_balance`` is the node's net mutual-credit position (owed to it
    minus owed by it). It is maintained by the :class:`~clearing_rail.ledger.Ledger`;
    do not mutate it directly.
    """

    node_id: str
    credit_ceiling: Decimal
    locked_vouch_stake: Decimal = ZERO
    current_balance: Decimal = ZERO
    urgency_boosts_used: int = 0
    operator_id: str = ""
    agent_id: str = ""

    def __post_init__(self):
        self.credit_ceiling = D(self.credit_ceiling)
        self.locked_vouch_stake = D(self.locked_vouch_stake)
        self.current_balance = D(self.current_balance)
        if self.credit_ceiling < 0:
            raise ValidationError("credit_ceiling must be >= 0")
        if self.locked_vouch_stake < 0 or self.locked_vouch_stake > self.credit_ceiling:
            raise ValidationError("locked_vouch_stake must be within [0, credit_ceiling]")

    @property
    def available_credit(self) -> Decimal:
        """Ceiling minus stake locked in outgoing vouches (ASSUMPTION, see README)."""
        return self.credit_ceiling - self.locked_vouch_stake

    @property
    def credit_floor(self) -> Decimal:
        """Lowest balance a debit leg may push this node to."""
        return -self.available_credit


@dataclass
class VouchEdge:
    """Directional vouch A -> B. Locks ``delta_c`` (a fixed slice of A's ceiling)."""

    voucher: str
    vouchee: str
    delta_c: Decimal
    created_at: datetime
    released: Decimal = ZERO

    @property
    def key(self) -> Tuple[str, str]:
        return (self.voucher, self.vouchee)

    @property
    def remaining(self) -> Decimal:
        return self.delta_c - self.released


@dataclass(frozen=True)
class Hop:
    """One leg of a cycle: ``debtor`` owes ``creditor``; clearing reduces it by ``amount``."""

    debtor: str
    creditor: str
    amount: Decimal

    def __post_init__(self):
        object.__setattr__(self, "amount", D(self.amount))


@dataclass(frozen=True)
class CycleCandidate:
    """Multi-hop array of nodes and clearing amounts published by a Solver.

    ``hops[i].creditor`` must equal ``hops[i+1].debtor`` and the last creditor
    must be the first debtor (a closed simple cycle). ``lineage_id`` ties a
    rerun back to the candidate it replaced; ``rerun_depth`` is 0 for an
    original candidate.
    """

    candidate_id: str
    solver_id: str
    hops: Tuple[Hop, ...]
    lineage_id: Optional[str] = None
    rerun_depth: int = 0

    def __post_init__(self):
        object.__setattr__(self, "hops", tuple(self.hops))
        if self.lineage_id is None:
            object.__setattr__(self, "lineage_id", self.candidate_id)

    @property
    def nodes(self) -> Tuple[str, ...]:
        return tuple(h.debtor for h in self.hops)

    @property
    def notional_clearance(self) -> Decimal:
        """Uniform clearing amount of the cycle (min over hops)."""
        return min(h.amount for h in self.hops)

    def debit_leg(self, node_id: str) -> Tuple[int, Hop]:
        for i, h in enumerate(self.hops):
            if h.debtor == node_id:
                return i, h
        raise KeyError(node_id)
