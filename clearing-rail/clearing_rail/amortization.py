"""Amortization Engine (Net-Leg Wash Filter).

Unlocks voucher A's locked stake on edge A->B based on vouchee B's real
economic activity with an outside third party C.

C1 Outside Vouch : C must not be in A's or B's immediate vouch tree.
C2 Net-Leg Only  : unlock <= min(matured outbound(B->C) legs, B's AGGREGATE net
                   outbound over the observation span).
C3 Wash Filter   : B's AGGREGATE inbound (from ANY counterparty) > 0.33 * B's
                   AGGREGATE outbound (to ANY counterparty) over the span -> 0.

FIX (Satoshi-mode S1, approved): the original checked only the B<->C pair and
only at call time, so value returning through a third node (rings), a pay-back
after the check, or a pay-back after the window rolled over all passed.
Now:
  * Maturity  : an outbound leg can back an unlock only once it is
                MATURITY_PERIOD (30 d) old, i.e. after its own window has passed.
                Eligible legs have ts in (as_of - MATURITY_PERIOD - window,
                as_of - MATURITY_PERIOD].
  * Aggregate : C2/C3 use B's flows with every counterparty over the span
                (first eligible leg - window, as_of].
  * Clawback  : each release stays provisional until CLAWBACK_HORIZON (60 d)
                after its latest backing leg. ``recheck`` (run automatically at
                the start of every ``amortize`` for that vouchee, and callable
                directly) re-runs C3 on (first leg - window, min(as_of, last
                leg + CLAWBACK_HORIZON)] and re-locks the whole release on a wash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from .ledger import ROLLING_WINDOW, Ledger
from .types import ZERO, D, ValidationError

MATURITY_PERIOD = ROLLING_WINDOW          # leg must be this old before it can back an unlock
CLAWBACK_HORIZON = 2 * ROLLING_WINDOW     # release stays provisional this long after its last leg
from .vouch import VouchGraph


class AmortizationStatus(str, Enum):
    UNLOCKED = "unlocked"
    INSIDE_VOUCH_TREE = "inside_vouch_tree"     # C1 failed
    WASH_DISQUALIFIED = "wash_disqualified"     # C3 failed
    NO_NET_TRANSFER = "no_net_transfer"         # C2 gave <= 0 (incl. no volume)
    EDGE_EXHAUSTED = "edge_exhausted"           # nothing left locked on A->B
    NOT_MATURED = "not_matured"                 # FIX: outbound exists but is < MATURITY_PERIOD old
    NEEDS_APPROVAL = "needs_approval"           # TIERED APPROVAL: unlock above tier / suspect, no grant yet


@dataclass(frozen=True)
class AmortizationPolicy:
    wash_ratio: Decimal = Decimal("0.33")
    unlock_ratio: Decimal = Decimal("1.0")
    window: timedelta = ROLLING_WINDOW
    tree_depth: Optional[int] = None   # None -> VouchGraph policy (default 1)


@dataclass(frozen=True)
class AmortizationResult:
    status: AmortizationStatus
    unlocked: Decimal = ZERO
    outbound: Decimal = ZERO
    inbound: Decimal = ZERO
    net_transfer: Decimal = ZERO
    consumed_transfer_ids: FrozenSet[int] = frozenset()
    clawed_back: Decimal = ZERO        # FIX: stake re-locked by the recheck run at the start of this call


@dataclass
class Release:
    """FIX (S1): bookkeeping for one provisional release."""

    release_id: int
    voucher: str
    vouchee: str
    counterparty: str
    amount: Decimal
    backing: Tuple[Tuple[int, Decimal], ...]
    first_leg_ts: datetime
    last_leg_ts: datetime
    released_at: datetime
    clawed_back: bool = False
    final: bool = False


# ----------------------------------------------------------------- pure rules
def is_wash(outbound: Decimal, inbound: Decimal, wash_ratio: Decimal = Decimal("0.33")) -> bool:
    """C3. True -> disqualify entirely. Exactly at the ratio is NOT a wash.
    outbound == 0 with any inbound > 0 is a wash; both zero is not."""
    return D(inbound) > D(wash_ratio) * D(outbound)


def net_leg(outbound: Decimal, inbound: Decimal) -> Decimal:
    """C2."""
    return D(outbound) - D(inbound)


def unlock_amount(outbound, inbound, remaining_stake, *, wash_ratio=Decimal("0.33"),
                  unlock_ratio=Decimal("1.0")) -> Tuple[AmortizationStatus, Decimal]:
    """Pure decision: wash check first, then net-leg, then cap at remaining ΔC."""
    if is_wash(outbound, inbound, wash_ratio):
        return AmortizationStatus.WASH_DISQUALIFIED, ZERO
    net = net_leg(outbound, inbound)
    if net <= 0:
        return AmortizationStatus.NO_NET_TRANSFER, ZERO
    if D(remaining_stake) <= 0:
        return AmortizationStatus.EDGE_EXHAUSTED, ZERO
    return AmortizationStatus.UNLOCKED, min(net * D(unlock_ratio), D(remaining_stake))


# ----------------------------------------------------------------- engine
class AmortizationEngine:
    def __setstate__(self, state):
        """v2: accept an engine pickled by the pre-v2 package (no release records)."""
        self.__dict__.update(state)
        self.__dict__.setdefault("_releases", [])

    def __init__(self, ledger: Ledger, graph: VouchGraph, policy: AmortizationPolicy = AmortizationPolicy()):
        self.ledger = ledger
        self.graph = graph
        self.policy = policy
        # (voucher, vouchee, counterparty) -> transfer ids already used to amortize
        self._consumed: Dict[Tuple[str, str, str], Set[int]] = {}
        self._releases: List[Release] = []

    def consumed(self, voucher: str, vouchee: str, counterparty: str) -> FrozenSet[int]:
        return frozenset(self._consumed.get((voucher, vouchee, counterparty), set()))

    def releases(self) -> List[Release]:
        return list(self._releases)

    # ---- aggregate flows ----------------------------------------------------
    def _aggregate(self, node: str, start: datetime, end: datetime) -> Tuple[Decimal, Decimal]:
        """(outbound, inbound) of ``node`` with EVERY counterparty, start < ts <= end."""
        out = inn = ZERO
        for t in self.ledger.transfers_of(node, start, end):
            if t.payer == node:
                out += t.amount
            else:
                inn += t.amount
        return out, inn

    # ---- clawback -----------------------------------------------------------
    def recheck(self, as_of: datetime, vouchee: Optional[str] = None) -> Decimal:
        """Re-run the aggregate wash check on every provisional release (optionally
        only ``vouchee``'s). A wash re-locks the whole release. Returns the total re-locked."""
        total = ZERO
        w = self.policy.window
        for r in self._releases:
            if r.clawed_back or r.final or (vouchee is not None and r.vouchee != vouchee):
                continue
            horizon_end = r.last_leg_ts + CLAWBACK_HORIZON
            end = min(as_of, horizon_end)
            out, inn = self._aggregate(r.vouchee, r.first_leg_ts - w, end)
            if is_wash(out, inn, self.policy.wash_ratio):
                edge = self.graph.edge(r.voucher, r.vouchee)
                amt = min(r.amount, edge.released)
                if amt > 0:
                    self.graph.relock(edge, amt, as_of, release_id=r.release_id, reason="wash_clawback")
                    total += amt
                r.clawed_back = True
            elif as_of > horizon_end:
                r.final = True
        return total

    # ---- main ---------------------------------------------------------------
    def amortize(self, voucher: str, vouchee: str, counterparty: str, as_of: datetime) -> AmortizationResult:
        edge = self.graph.edge(voucher, vouchee)
        clawed = self.recheck(as_of, vouchee)
        if counterparty in (voucher, vouchee):
            return AmortizationResult(AmortizationStatus.INSIDE_VOUCH_TREE, clawed_back=clawed)
        # C1 outside vouch
        tree = self.graph.vouch_tree((voucher, vouchee), self.policy.tree_depth)
        if counterparty in tree:
            return AmortizationResult(AmortizationStatus.INSIDE_VOUCH_TREE, clawed_back=clawed)
        if edge.remaining <= 0:
            return AmortizationResult(AmortizationStatus.EDGE_EXHAUSTED, clawed_back=clawed)

        w = self.policy.window
        mature_end = as_of - MATURITY_PERIOD
        used = self._consumed.setdefault((voucher, vouchee, counterparty), set())
        pair = self.ledger.transfers_between(vouchee, counterparty, mature_end - w, as_of)
        eligible = [t for t in pair if t.payer == vouchee and t.ts <= mature_end and t.transfer_id not in used]
        immature = [t for t in pair if t.payer == vouchee and t.ts > mature_end]
        span_start = (min(t.ts for t in eligible) if eligible else mature_end) - w
        full_out, full_in = self._aggregate(vouchee, span_start, as_of)
        # C3 aggregate wash filter (consumed volume still counts)
        if is_wash(full_out, full_in, self.policy.wash_ratio):
            return AmortizationResult(AmortizationStatus.WASH_DISQUALIFIED, outbound=full_out, inbound=full_in,
                                      net_transfer=net_leg(full_out, full_in), clawed_back=clawed)
        elig_out = sum((t.amount for t in eligible), ZERO)
        if elig_out <= 0:
            status = AmortizationStatus.NOT_MATURED if immature else AmortizationStatus.NO_NET_TRANSFER
            return AmortizationResult(status, outbound=elig_out, inbound=full_in,
                                      net_transfer=net_leg(full_out, full_in), clawed_back=clawed)
        # C2 net-leg: bounded by both the eligible matured legs and B's aggregate net outbound
        net = min(elig_out, net_leg(full_out, full_in))
        if net <= 0:
            return AmortizationResult(AmortizationStatus.NO_NET_TRANSFER, outbound=elig_out, inbound=full_in,
                                      net_transfer=net, clawed_back=clawed)
        unlock = min(net * self.policy.unlock_ratio, edge.remaining)
        appr = getattr(self.ledger, "approvals", None)
        if appr is not None:
            from .approvals import ApprovalRequired
            try:
                appr.check_unlock(voucher, vouchee, edge.released + unlock, as_of)
            except ApprovalRequired:
                return AmortizationResult(AmortizationStatus.NEEDS_APPROVAL, outbound=elig_out, inbound=full_in,
                                          net_transfer=net, clawed_back=clawed)
        backing = tuple((t.transfer_id, t.amount) for t in eligible)
        rid = len(self._releases) + 1
        self.graph.release(edge, unlock, as_of, via_outside_volume=True, counterparty=counterparty,
                           reason="net_leg_amortization", backing=backing, release_id=rid)
        self._releases.append(Release(rid, voucher, vouchee, counterparty, unlock, backing,
                                      min(t.ts for t in eligible), max(t.ts for t in eligible), as_of))
        ids = frozenset(t.transfer_id for t in eligible)
        used |= ids
        return AmortizationResult(AmortizationStatus.UNLOCKED, unlock, elig_out, full_in, net, ids, clawed)
