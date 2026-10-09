"""EXPERIMENT (V2, OFF by default): refundable admission bond against fake-identity armies.

A NEW member posts a bond ``amount`` when it joins. Nothing in the package attaches
or requires a bond; a caller has to construct ``AdmissionBonds`` and ``post`` one.

STUB-DEPENDENT: the bond is an external deposit (cash / sats held by an escrow).
How it is collected, held and paid out is NOT modelled; this module only keeps the
book (who posted what, and whether it is held, refunded or forfeited) so the rule
can be measured. It never touches ledger balances.

Rule (Decimal only, no wall clock: every check takes ``as_of``)
---------------------------------------------------------------
* forfeited, reason "default": the member holds a debit lot older than
  ``stale_debt_age`` AND has had no repayment (its debt going down) and no sale
  for ``default_idle``.
* forfeited, reason "wash": a ``StakeClawedBack`` event names it as vouchee.
* refunded: tenure >= ``refund_tenure`` AND earned limit >= ``refund_limit_ratio``
  x its starter limit AND no debit lot older than ``stale_debt_age``.
* Forfeit is checked before refund; both are terminal (first one reached wins,
  evaluated lazily at ``as_of``; callers should evaluate in time order).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional

from .events import StakeClawedBack
from .types import ZERO, D, ValidationError

BOND_AMOUNT = Decimal("0")                 # 0 = experiment off
BOND_DEFAULT_IDLE = timedelta(days=60)
BOND_REFUND_TENURE = timedelta(days=180)
BOND_REFUND_LIMIT_RATIO = Decimal("2")
BOND_STALE_DEBT_AGE = timedelta(days=90)


@dataclass(frozen=True)
class BondPolicy:
    amount: Decimal = BOND_AMOUNT
    default_idle: timedelta = BOND_DEFAULT_IDLE
    refund_tenure: timedelta = BOND_REFUND_TENURE
    refund_limit_ratio: Decimal = BOND_REFUND_LIMIT_RATIO
    stale_debt_age: timedelta = BOND_STALE_DEBT_AGE

    def __post_init__(self):
        object.__setattr__(self, "amount", D(self.amount))
        object.__setattr__(self, "refund_limit_ratio", D(self.refund_limit_ratio))
        if self.amount < 0 or self.refund_limit_ratio < 0:
            raise ValidationError("bond amount and refund ratio must be >= 0")
        if min(self.default_idle, self.refund_tenure, self.stale_debt_age) <= timedelta(0):
            raise ValidationError("bond durations must be > 0")


@dataclass
class Bond:
    node_id: str
    amount: Decimal
    posted_at: datetime
    state: str = "held"                    # held | refunded | forfeited
    reason: str = ""
    closed_at: Optional[datetime] = None


class AdmissionBonds:
    """EXPERIMENT. Bond book over a ledger + ``CreditLimits``."""

    def __init__(self, ledger, limits, policy: BondPolicy = BondPolicy()):
        self.ledger, self.limits, self.policy = ledger, limits, policy
        self._bonds: Dict[str, Bond] = {}

    def post(self, node_id: str, ts: datetime) -> Bond:
        if node_id in self._bonds:
            raise ValidationError(f"{node_id} already posted a bond")
        b = Bond(node_id, self.policy.amount, ts)
        self._bonds[node_id] = b
        return b

    def bond(self, node_id: str) -> Optional[Bond]:
        return self._bonds.get(node_id)

    def _last_activity(self, node_id: str, as_of: datetime) -> Optional[datetime]:
        ts = [r.ts for r in self.ledger.repayments(node_id) if r.ts is not None and r.ts <= as_of]
        ts += [t.ts for t in self.ledger.transfers_of(node_id, as_of - self.policy.default_idle, as_of)
               if t.payee == node_id]
        return max(ts, default=None)

    def evaluate(self, node_id: str, as_of: datetime) -> Bond:
        b = self._bonds[node_id]
        if b.state != "held":
            return b
        p = self.policy
        if any(e.vouchee == node_id for e in self.ledger.events.of_type(StakeClawedBack)):
            b.state, b.reason, b.closed_at = "forfeited", "wash", as_of
            return b
        stale = self.ledger.stale_balance(node_id, as_of, p.stale_debt_age) > ZERO
        if stale:
            last = self._last_activity(node_id, as_of) or b.posted_at
            if as_of - last >= p.default_idle:
                b.state, b.reason, b.closed_at = "forfeited", "default", as_of
            return b
        if (as_of - b.posted_at >= p.refund_tenure and
                self.limits.effective_limit(node_id, as_of) >=
                p.refund_limit_ratio * min(self.ledger.node(node_id).credit_ceiling, self.limits.policy.starter_limit)):
            b.state, b.reason, b.closed_at = "refunded", "good standing", as_of
        return b

    def totals(self) -> Dict[str, Decimal]:
        out = {"held": ZERO, "refunded": ZERO, "forfeited": ZERO}
        for b in self._bonds.values():
            out[b.state] += b.amount
        return out
