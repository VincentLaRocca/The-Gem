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
    def __setstate__(self, state):
        """v2: restore a Ledger pickled by the pre-v2 package (e.g. the pilot's
        state file). Fields added since then get their empty defaults; the
        per-node transfer index is rebuilt. Old history has no repayment log."""
        self.__dict__.update(state)
        self.__dict__.setdefault("_reservations", {})
        self.__dict__.setdefault("limits", None)
        self.__dict__.setdefault("approvals", None)
        self.__dict__.setdefault("_repayments", [])
        self.__dict__.setdefault("_repay_by_node", {})
        self.__dict__.setdefault("_repay_to", {})
        if "_by_node" not in self.__dict__:
            self._by_node = {}
            for t in self._transfers:
                self._by_node.setdefault(t.payer, []).append(t)
                self._by_node.setdefault(t.payee, []).append(t)

    def __init__(self, events: Optional[EventLog] = None):
        self.nodes: Dict[str, Node] = {}
        self.events = events if events is not None else EventLog()
        self._lots: Dict[Tuple[str, str], List[Lot]] = {}
        self._transfers: List[Transfer] = []
        self._retries: Dict[str, int] = {}
        self._next_lot = 1
        self._next_transfer = 1
        # FIX (S4): signed-hop reservations, res_id -> ((debtor, creditor), amount).
        # Deliberately outside snapshot/restore, like solver bonds.
        self._reservations: Dict[str, Tuple[Tuple[str, str], Decimal]] = {}
        # STARTER CAP: optional earned-limit book (clearing_rail.limits.CreditLimits
        # attaches itself here) and the log of debt actually repaid (cleared).
        self.limits = None
        self.approvals = None          # TIERED APPROVAL: clearing_rail.approvals.ApprovalBook
        self._by_node: Dict[str, List[Transfer]] = {}   # per-node transfer index (perf only)
        self._repayments: List = []
        self._repay_by_node: Dict[str, List] = {}
        self._repay_to: Dict[str, List] = {}          # V2: index by counterparty

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
        if self.limits is not None and self.limits.is_new(debtor):
            floor = self.limits.spendable_floor(debtor, ts)
            if d.current_balance - amount < floor:
                raise CreditLimitExceeded(
                    f"{debtor}: balance {d.current_balance} - {amount} < earned-limit floor {floor} (starter cap)"
                )
        if self.approvals is not None:
            # TIERED APPROVAL (+ risk escalation): large or suspect exposure needs a live grant
            self.approvals.check_credit(debtor, d.current_balance - amount, ts)
        # bilateral netting against any obligation creditor already owes debtor.
        # FIX (S4): never net into the reserved part of that obligation (a signed
        # cycle hop); the excess opens a reverse lot instead.
        nettable = max(ZERO, self.outstanding(creditor, debtor) - self.reserved(creditor, debtor))
        to_net = min(amount, nettable)
        for lot in self._lots.get((creditor, debtor), []):
            if to_net == 0:
                break
            take = min(lot.amount, to_net)
            lot.amount -= take
            to_net -= take
            if take > 0:   # STARTER CAP: the seller (creditor) repaid its debt to the buyer
                self._log_repayment(creditor, debtor, take, ts, "netting", lot.originated_at)
        remaining = amount - min(amount, nettable)
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
        self._by_node.setdefault(debtor, []).append(t)
        self._by_node.setdefault(creditor, []).append(t)
        self.events.append(TradeConfirmed(ts, t.transfer_id, debtor, creditor, amount, initiated_by))
        return t

    # ------------------------------------------------------------------ obligations
    def outstanding(self, debtor: str, creditor: str) -> Decimal:
        return sum((l.amount for l in self._lots.get((debtor, creditor), [])), ZERO)

    # ------------------------------------------------------------------ reservations (FIX S4)
    def reserved(self, debtor: str, creditor: str) -> Decimal:
        return sum((amt for key, amt in self._reservations.values() if key == (debtor, creditor)), ZERO)

    def reserve(self, res_id: str, debtor: str, creditor: str, amount) -> None:
        """Reserve ``amount`` of obligation debtor->creditor so later trades cannot net it away."""
        amount = D(amount)
        if amount <= 0:
            raise LedgerError("reservation must be > 0")
        if res_id in self._reservations:
            raise LedgerError(f"reservation {res_id} already exists")
        free = self.outstanding(debtor, creditor) - self.reserved(debtor, creditor)
        if free < amount:
            raise LedgerError(f"obligation {debtor}->{creditor} has only {free} unreserved, cannot reserve {amount}")
        self._reservations[res_id] = ((debtor, creditor), amount)

    def release_reservation(self, res_id: str) -> None:
        self._reservations.pop(res_id, None)

    def net_pair(self, a: str, b: str, ts: Optional[datetime] = None) -> Decimal:
        """V2 (invariant fix): net opposite-direction obligations between a and b that a
        reservation kept apart. Only the unreserved parts net (oldest lots first). Net
        balances do not change (both sides shrink by the same amount); each side's
        reduction is logged as a repayment. Returns the amount netted."""
        x = min(self.outstanding(a, b) - self.reserved(a, b), self.outstanding(b, a) - self.reserved(b, a))
        if x <= 0:
            return ZERO
        for key in ((a, b), (b, a)):
            left = x
            for lot in sorted(self._lots.get(key, []), key=lambda l: (l.originated_at, l.lot_id)):
                if left == 0:
                    break
                take = min(lot.amount, left)
                lot.amount -= take
                left -= take
                if take > 0:
                    self._log_repayment(key[0], key[1], take, ts, "netting", lot.originated_at)
            self._prune(key)
        return x

    def debit_lots(self, node_id: str) -> List[Lot]:
        out = [l for (d, _), lots in self._lots.items() if d == node_id for l in lots]
        return sorted(out, key=lambda l: (l.originated_at, l.lot_id))

    def stale_balance(self, node_id: str, as_of: datetime, threshold: timedelta) -> Decimal:
        """Sum of the node's debit lots whose age is strictly greater than ``threshold``."""
        return sum((l.amount for l in self.debit_lots(node_id) if l.age(as_of) > threshold), ZERO)

    # ------------------------------------------------------------------ repayments (STARTER CAP)
    def _log_repayment(self, repayer: str, counterparty: str, amount: Decimal, ts, via: str,
                       opened_at: Optional[datetime] = None) -> None:
        from .limits import Repayment
        if self.limits is None or ts is None:
            est, sea = True, False
        else:
            est, sea = self.limits.standing(counterparty, ts)
        r = Repayment(repayer, counterparty, amount, ts, via, est, sea, opened_at)
        self._repayments.append(r)
        self._repay_by_node.setdefault(repayer, []).append(r)
        self._repay_to.setdefault(counterparty, []).append(r)
        if self.limits is not None and ts is not None:
            self.limits._note_repayment_ts(ts)

    def repayments_to(self, node_id: str) -> List:
        """V2: repayments whose counterparty is ``node_id``."""
        return list(self._repay_to.get(node_id, ()))

    def repayments(self, node_id: Optional[str] = None) -> List:
        if node_id is None:
            return list(self._repayments)
        return list(self._repay_by_node.get(node_id, ()))

    def clear_leg(self, hop: Hop, ts: Optional[datetime] = None) -> None:
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
            if take > 0:
                self._log_repayment(hop.debtor, hop.creditor, take, ts, "cycle", lot.originated_at)
        self._prune(key)
        self.node(hop.debtor).current_balance += hop.amount
        self.node(hop.creditor).current_balance -= hop.amount

    def apply_cycle(self, hops: Iterable[Hop], ts: Optional[datetime] = None) -> None:
        for h in hops:
            self.clear_leg(h, ts)

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

    def transfers_of(self, node: str, start: datetime, end: datetime) -> List[Transfer]:
        """FIX (S1): every transfer where ``node`` is payer or payee, start < ts <= end."""
        return [t for t in self._by_node.get(node, ()) if start < t.ts <= end]

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
            len(self._repayments),
        )

    def restore(self, snap) -> None:
        balances, lots, n_transfers, retries, next_lot, next_transfer, n_repay = snap
        if len(self._repayments) > n_repay:
            del self._repayments[n_repay:]
            self._repay_by_node = {}
            self._repay_to = {}
            for r in self._repayments:
                self._repay_by_node.setdefault(r.repayer, []).append(r)
                self._repay_to.setdefault(r.counterparty, []).append(r)
            if self.limits is not None:
                self.limits._invalidate()
        for k, bal in balances.items():
            self.nodes[k].current_balance = bal
        self._lots = copy.deepcopy(lots)
        del self._transfers[n_transfers:]
        keep = {t.transfer_id for t in self._transfers}
        for k in list(self._by_node):
            self._by_node[k] = [t for t in self._by_node[k] if t.transfer_id in keep]
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
