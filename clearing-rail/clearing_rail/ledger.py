"""Double-entry mutual-credit ledger.

Model
-----
* Every confirmed trade creates a bilateral obligation ``debtor -> creditor``
  (an obligation *lot* with an origination time) and moves net balances:
  debtor -= amount, creditor += amount. Net balances always sum to zero.
* Opposite-direction obligations between the same pair are netted bilaterally
  (oldest lot first) before a new lot is opened.
* Cycle clearing reduces obligations along a closed cycle. With a uniform
  amount it leaves every net balance unchanged and compresses gross debt.
* Every trade is also recorded as a directional ``Transfer`` so the ledger can
  answer rolling 30-day bilateral volume queries.
* Balance age is tracked per lot. A node's *stale balance* is the sum of its
  debit lots whose effective age is strictly greater than the threshold.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from .events import EventLog, TradeConfirmed
from .types import (
    ZERO,
    CreditLimitExceeded,
    D,
    Hop,
    Initiator,
    LedgerError,
    Node,
    ValidationError,
)

ROLLING_WINDOW = timedelta(days=30)


@dataclass
class Lot:
    lot_id: int
    debtor: str
    creditor: str
    amount: Decimal          # remaining
    originated_at: datetime
    age_bump: timedelta = timedelta(0)

    def age(self, as_of: datetime) -> timedelta:
        return as_of - self.originated_at + self.age_bump


@dataclass(frozen=True)
class Transfer:
    transfer_id: int
    payer: str               # the debtor side of the trade ("outbound" for payer)
    payee: str
    amount: Decimal
    ts: datetime


class Ledger:
    def __init__(self, events: Optional[EventLog] = None):
        self.nodes: Dict[str, Node] = {}
        self.events = events if events is not None else EventLog()
        self._lots: Dict[Tuple[str, str], List[Lot]] = {}
        self._transfers: List[Transfer] = []
        self._retries: Dict[str, int] = {}
        self._next_lot = 1
        self._next_transfer = 1

    # ------------------------------------------------------------------ nodes
    def add_node(self, node: Node) -> Node:
        if node.node_id in self.nodes:
            raise ValidationError(f"duplicate node {node.node_id}")
        if node.current_balance != 0:
            raise ValidationError("nodes join with a zero balance (keeps the ledger summing to zero)")
        self.nodes[node.node_id] = node
        return node

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise ValidationError(f"unknown node {node_id}") from None

    def total_balance(self) -> Decimal:
        return sum((n.current_balance for n in self.nodes.values()), ZERO)

    # ------------------------------------------------------------------ trades
    def record_trade(
        self,
        debtor: str,
        creditor: str,
        amount,
        ts: datetime,
        initiated_by: Initiator = Initiator.HUMAN,
    ) -> Transfer:
        """Post a confirmed offer: ``debtor`` takes value from ``creditor`` on credit."""
        amount = D(amount)
        if amount <= 0:
            raise ValidationError("trade amount must be > 0")
        if debtor == creditor:
            raise ValidationError("self-trade")
        d, c = self.node(debtor), self.node(creditor)
        if d.current_balance - amount < d.credit_floor:
            raise CreditLimitExceeded(
                f"{debtor}: balance {d.current_balance} - {amount} < floor {d.credit_floor}"
            )
        # bilateral netting against any obligation creditor already owes debtor
        remaining = amount
        for lot in self._lots.get((creditor, debtor), []):
            if remaining == 0:
                break
            take = min(lot.amount, remaining)
            lot.amount -= take
            remaining -= take
        self._prune((creditor, debtor))
        if remaining > 0:
            self._lots.setdefault((debtor, creditor), []).append(
                Lot(self._next_lot, debtor, creditor, remaining, ts)
            )
            self._next_lot += 1
        d.current_balance -= amount
        c.current_balance += amount
        t = Transfer(self._next_transfer, debtor, creditor, amount, ts)
        self._next_transfer += 1
        self._transfers.append(t)
        self.events.append(TradeConfirmed(ts, t.transfer_id, debtor, creditor, amount, initiated_by))
        return t

    # ------------------------------------------------------------------ obligations
    def outstanding(self, debtor: str, creditor: str) -> Decimal:
        return sum((l.amount for l in self._lots.get((debtor, creditor), [])), ZERO)

    def debit_lots(self, node_id: str) -> List[Lot]:
        out = [l for (d, _), lots in self._lots.items() if d == node_id for l in lots]
        return sorted(out, key=lambda l: (l.originated_at, l.lot_id))

    def stale_balance(self, node_id: str, as_of: datetime, threshold: timedelta) -> Decimal:
        """Sum of the node's debit lots whose age is strictly greater than ``threshold``."""
        return sum((l.amount for l in self.debit_lots(node_id) if l.age(as_of) > threshold), ZERO)

    def clear_leg(self, hop: Hop) -> None:
        """Reduce obligation ``hop.debtor -> hop.creditor`` by ``hop.amount`` (oldest lot first)."""
        if hop.amount <= 0:
            raise LedgerError("clearing amount must be > 0")
        key = (hop.debtor, hop.creditor)
        if self.outstanding(*key) < hop.amount:
            raise LedgerError(
                f"obligation {hop.debtor}->{hop.creditor} is {self.outstanding(*key)}, cannot clear {hop.amount}"
            )
        remaining = hop.amount
        for lot in sorted(self._lots[key], key=lambda l: (l.originated_at, l.lot_id)):
            if remaining == 0:
                break
            take = min(lot.amount, remaining)
            lot.amount -= take
            remaining -= take
        self._prune(key)
        self.node(hop.debtor).current_balance += hop.amount
        self.node(hop.creditor).current_balance -= hop.amount

    def apply_cycle(self, hops: Iterable[Hop]) -> None:
        for h in hops:
            self.clear_leg(h)

    def age_balance(self, node_id: str, penalty: timedelta = timedelta(0)) -> int:
        """Keep the node's balance aging (its clock is NOT reset), apply an
        optional age penalty to its debit lots, and bump its retry counter."""
        self.node(node_id)
        for lot in self.debit_lots(node_id):
            lot.age_bump += penalty
        self._retries[node_id] = self._retries.get(node_id, 0) + 1
        return self._retries[node_id]

    def retry_count(self, node_id: str) -> int:
        return self._retries.get(node_id, 0)

    def _prune(self, key) -> None:
        lots = [l for l in self._lots.get(key, []) if l.amount > 0]
        if lots:
            self._lots[key] = lots
        else:
            self._lots.pop(key, None)

    # ------------------------------------------------------------------ volume
    def transfers_between(self, a: str, b: str, start: datetime, end: datetime) -> List[Transfer]:
        """Transfers in either direction between a and b with start < ts <= end."""
        return [
            t for t in self._transfers
            if {t.payer, t.payee} == {a, b} and start < t.ts <= end
        ]

    def bilateral_volume(self, a: str, b: str, as_of: datetime,
                         window: timedelta = ROLLING_WINDOW) -> Tuple[Decimal, Decimal]:
        """(outbound a->b, inbound b->a) over the rolling window ending at ``as_of``."""
        ts = self.transfers_between(a, b, as_of - window, as_of)
        out = sum((t.amount for t in ts if t.payer == a), ZERO)
        inn = sum((t.amount for t in ts if t.payer == b), ZERO)
        return out, inn

    def bilateral_net(self, a: str, b: str, as_of: datetime,
                      window: timedelta = ROLLING_WINDOW) -> Decimal:
        out, inn = self.bilateral_volume(a, b, as_of, window)
        return out - inn

    # ------------------------------------------------------------------ atomicity
    def snapshot(self):
        return (
            {k: n.current_balance for k, n in self.nodes.items()},
            copy.deepcopy(self._lots),
            len(self._transfers),
            dict(self._retries),
            self._next_lot,
            self._next_transfer,
        )

    def restore(self, snap) -> None:
        balances, lots, n_transfers, retries, next_lot, next_transfer = snap
        for k, bal in balances.items():
            self.nodes[k].current_balance = bal
        self._lots = copy.deepcopy(lots)
        del self._transfers[n_transfers:]
        self._retries = dict(retries)
        self._next_lot, self._next_transfer = next_lot, next_transfer

    @contextmanager
    def transaction(self):
        """All-or-nothing: any exception inside restores ledger state exactly."""
        snap = self.snapshot()
        try:
            yield self
        except BaseException:
            self.restore(snap)
            raise

    def state_fingerprint(self):
        """Comparable view of ledger state (used by tests to prove reverts)."""
        return (
            tuple(sorted((k, n.current_balance) for k, n in self.nodes.items())),
            tuple(sorted((k, tuple((l.lot_id, l.amount, l.originated_at, l.age_bump) for l in v))
                         for k, v in self._lots.items())),
            len(self._transfers),
        )
