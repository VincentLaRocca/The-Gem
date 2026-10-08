"""Amortization Engine (Net-Leg Wash Filter).

Unlocks voucher A's locked stake on edge A->B based on vouchee B's real
economic activity with an outside third party C over the rolling 30-day window.

C1 Outside Vouch : C must not be in A's or B's immediate vouch tree.
C2 Net-Leg Only  : net_transfer = outbound(B->C) - inbound(C->B).
C3 Wash Filter   : inbound(C->B) > 0.33 * outbound(B->C)  ->  0 (disqualified).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, FrozenSet, Optional, Set, Tuple

from .ledger import ROLLING_WINDOW, Ledger
from .types import ZERO, D, ValidationError
from .vouch import VouchGraph


class AmortizationStatus(str, Enum):
    UNLOCKED = "unlocked"
    INSIDE_VOUCH_TREE = "inside_vouch_tree"     # C1 failed
    WASH_DISQUALIFIED = "wash_disqualified"     # C3 failed
    NO_NET_TRANSFER = "no_net_transfer"         # C2 gave <= 0 (incl. no volume)
    EDGE_EXHAUSTED = "edge_exhausted"           # nothing left locked on A->B


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
    def __init__(self, ledger: Ledger, graph: VouchGraph, policy: AmortizationPolicy = AmortizationPolicy()):
        self.ledger = ledger
        self.graph = graph
        self.policy = policy
        # (voucher, vouchee, counterparty) -> transfer ids already used to amortize
        self._consumed: Dict[Tuple[str, str, str], Set[int]] = {}

    def consumed(self, voucher: str, vouchee: str, counterparty: str) -> FrozenSet[int]:
        return frozenset(self._consumed.get((voucher, vouchee, counterparty), set()))

    def amortize(self, voucher: str, vouchee: str, counterparty: str, as_of: datetime) -> AmortizationResult:
        edge = self.graph.edge(voucher, vouchee)
        if counterparty in (voucher, vouchee):
            return AmortizationResult(AmortizationStatus.INSIDE_VOUCH_TREE)
        # C1 outside vouch
        tree = self.graph.vouch_tree((voucher, vouchee), self.policy.tree_depth)
        if counterparty in tree:
            return AmortizationResult(AmortizationStatus.INSIDE_VOUCH_TREE)
        if edge.remaining <= 0:
            return AmortizationResult(AmortizationStatus.EDGE_EXHAUSTED)

        window = self.ledger.transfers_between(vouchee, counterparty, as_of - self.policy.window, as_of)
        full_out = sum((t.amount for t in window if t.payer == vouchee), ZERO)
        full_in = sum((t.amount for t in window if t.payer == counterparty), ZERO)
        # C3 wash filter on the WHOLE window (consumed volume still counts toward wash)
        if is_wash(full_out, full_in, self.policy.wash_ratio):
            return AmortizationResult(AmortizationStatus.WASH_DISQUALIFIED, outbound=full_out, inbound=full_in,
                                      net_transfer=net_leg(full_out, full_in))
        # C2 net-leg on volume not yet used by THIS edge with THIS counterparty
        used = self._consumed.setdefault((voucher, vouchee, counterparty), set())
        fresh = [t for t in window if t.transfer_id not in used]
        out = sum((t.amount for t in fresh if t.payer == vouchee), ZERO)
        inn = sum((t.amount for t in fresh if t.payer == counterparty), ZERO)
        net = net_leg(out, inn)
        if net <= 0:
            # leave inbound unconsumed so it keeps offsetting future outbound
            return AmortizationResult(AmortizationStatus.NO_NET_TRANSFER, outbound=out, inbound=inn, net_transfer=net)
        unlock = min(net * self.policy.unlock_ratio, edge.remaining)
        self.graph.release(edge, unlock, as_of, via_outside_volume=True, counterparty=counterparty,
                           reason="net_leg_amortization")
        ids = frozenset(t.transfer_id for t in fresh)
        used |= ids
        return AmortizationResult(AmortizationStatus.UNLOCKED, unlock, out, inn, net, ids)
