"""Vouch graph: directional vouches that lock a fixed slice of the voucher's ceiling."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .events import StakeReleased
from .ledger import Ledger
from .types import CreditLimitExceeded, D, ValidationError, VouchEdge


@dataclass(frozen=True)
class VouchPolicy:
    slice_fraction: Decimal = Decimal("0.10")  # ΔC = slice_fraction * voucher ceiling
    tree_depth: int = 1                        # "immediate" vouch tree = 1 hop


class VouchGraph:
    def __init__(self, ledger: Ledger, policy: VouchPolicy = VouchPolicy()):
        if not (Decimal(0) < policy.slice_fraction <= Decimal(1)):
            raise ValidationError("slice_fraction must be in (0, 1]")
        self.ledger = ledger
        self.policy = policy
        self._edges: Dict[Tuple[str, str], VouchEdge] = {}

    def vouch(self, voucher: str, vouchee: str, ts: datetime) -> VouchEdge:
        if voucher == vouchee:
            raise ValidationError("self-vouch")
        if (voucher, vouchee) in self._edges:
            raise ValidationError("duplicate vouch edge")
        a = self.ledger.node(voucher)
        self.ledger.node(vouchee)
        delta = a.credit_ceiling * self.policy.slice_fraction
        if a.locked_vouch_stake + delta > a.credit_ceiling:
            raise CreditLimitExceeded(f"{voucher}: not enough unlocked ceiling for ΔC={delta}")
        if a.current_balance < -(a.available_credit - delta):
            raise CreditLimitExceeded(f"{voucher}: locking ΔC would put the balance below the credit floor")
        a.locked_vouch_stake += delta
        edge = VouchEdge(voucher, vouchee, delta, ts)
        self._edges[edge.key] = edge
        return edge

    def edge(self, voucher: str, vouchee: str) -> VouchEdge:
        try:
            return self._edges[(voucher, vouchee)]
        except KeyError:
            raise ValidationError(f"no vouch edge {voucher}->{vouchee}") from None

    def edges(self) -> List[VouchEdge]:
        return list(self._edges.values())

    def neighbors(self, node_id: str) -> Set[str]:
        """Nodes with a direct vouch edge to OR from ``node_id``."""
        out = set()
        for (a, b) in self._edges:
            if a == node_id:
                out.add(b)
            elif b == node_id:
                out.add(a)
        return out

    def vouch_tree(self, roots: Iterable[str], depth: Optional[int] = None) -> Set[str]:
        """Roots plus every node within ``depth`` vouch hops (either direction)."""
        depth = self.policy.tree_depth if depth is None else depth
        if depth < 0:
            raise ValidationError("depth must be >= 0")
        seen = set(roots)
        frontier = set(seen)
        for _ in range(depth):
            nxt = set()
            for n in frontier:
                nxt |= self.neighbors(n)
            nxt -= seen
            seen |= nxt
            frontier = nxt
        return seen

    def release(self, edge: VouchEdge, amount, ts: datetime, *, via_outside_volume: bool,
                counterparty: Optional[str] = None, reason: str = "") -> Decimal:
        amount = D(amount)
        if amount <= 0:
            raise ValidationError("release amount must be > 0")
        if amount > edge.remaining:
            raise ValidationError(f"release {amount} exceeds remaining {edge.remaining}")
        edge.released += amount
        self.ledger.node(edge.voucher).locked_vouch_stake -= amount
        self.ledger.events.append(
            StakeReleased(ts, edge.voucher, edge.vouchee, amount, via_outside_volume, counterparty, reason)
        )
        return amount
