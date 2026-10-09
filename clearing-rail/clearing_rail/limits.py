"""STARTER CAP: a new member's credit limit starts small and grows only with
cleared, matured, counterparty-diverse repayment to established members.

Rule (all Decimal, no wall clock; time comes only from ``as_of`` / ``ts``)
-------------------------------------------------------------------------
For a node registered as NEW at ``joined_at`` with configured ceiling ``C``::

    L_0 = min(C, STARTER_LIMIT)
    for each growth period i = [joined_at + i*P, joined_at + (i+1)*P)
        whose end is at least MATURITY before as_of:
        credited_i = sum over counterparties y of
                     min(repaid_to_y_in_period_i, COUNTERPARTY_CAP_RATIO * L_i)
                     (only repayments to counterparties that were ESTABLISHED
                      at the moment of repayment; self never counts)
        growth_i   = min(PERIOD_GROWTH_CAP_RATIO * L_i, GROWTH_RATE * credited_i)
                     (0 if the node was in bad standing at the period end)
        L_{i+1}    = min(C, L_i + growth_i)
    effective_limit(node, as_of) = L_k  (k = number of matured periods)

* A *repayment* is the node's own debt actually going down: either the node
  sold to someone it owed (bilateral netting in ``Ledger.record_trade``) or a
  committed cycle cleared one of its debit legs (``Ledger.clear_leg`` via
  ``SettlementEngine.commit``). Opening new debt, or merely selling to a node
  you do not owe, is volume, not clearing, and earns nothing.
* *Established* = registered GENESIS, unregistered (legacy / unrestricted),
  or a NEW node whose own effective limit has reached ESTABLISHED_LIMIT.
  The flag is frozen when the repayment is recorded, so there is no recursion.
  A ring of fresh sybils therefore cannot grow each other: none of them is
  established, so their mutual "repayments" are worth 0.
* *Bad standing* = the node still holds a debit lot that was older than
  STALE_DEBT_AGE at the end of that period (checked on the current lots).
* Enforcement: the ledger refuses a debit leg that would take a NEW node's
  balance below ``-min(available_credit, effective_limit)``; the vouch graph
  caps a new edge's stake ΔC at ``min(slice * voucher ceiling, slice *
  voucher's effective limit, vouchee's effective limit)``.
* Nodes never registered with the book are unrestricted (backward compatible).

V2 (fixed-starter-v2) additions -- see starter-cap-v2.md
----------------------------------------------------------
* *Seasoned* counterparty: a NEW node that is not (yet) established but has
  tenure >= SEASONED_MIN_TENURE, an effective limit above the starter (it has
  earned growth, i.e. done real work with established members) and no open
  debt older than STALE_DEBT_AGE, all at the moment of repayment (frozen).
  Repayment to a seasoned counterparty counts at weight SEASONED_WEIGHT.
* Matching cap ("conservation of trust"): per period the seasoned part of the
  credit can add at most SEASONED_MATCH x the established part. A ring of
  seasoned sybils therefore cannot grow on its own: with zero repayment to
  established members, it earns zero.
* Non-wash: a repaid amount counts only if the lot it repaid was at least
  MIN_REPAID_AGE old (quick buy-then-repay round trips earn nothing).
* Promoted members (NEW nodes that reached ESTABLISHED_LIMIT) are *peers*, not
  anchors, when ``promoted_as_anchor`` is False (the V2 default): their
  repayments go to the matched peer bucket. Reason: under v1 a ring of sybils
  that each paid (1000-250)/0.5 = 1500 of honest work to reach 1000 could then
  grow each other with no further work (S34 measured honest work per growth
  credit down to 1.01 under v1). Anchors are genesis / unregistered members.
* So credited_i = E_i + min(S_i, SEASONED_MATCH * E_i), where
  E_i = sum_y min(repaid_to_anchor_y, cp_cap * L_i) and
  S_i = sum_y min(SEASONED_WEIGHT * repaid_to_peer_y, cp_cap * L_i).
  An attacker therefore needs at least 1 / (GROWTH_RATE * (1 + SEASONED_MATCH))
  credits of repayment to established members per credit of growth.
* ``StarterPolicy.v1()`` reproduces the fixed-starter rule exactly.

Constants are module-level so callers/tests can read them; ``StarterPolicy``
carries the values actually used so experiments can vary them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .types import ZERO, D, ValidationError

STARTER_LIMIT = Decimal("250")          # credits a brand-new member may owe
GROWTH_RATE = Decimal("0.5")            # limit gained per credit of credited repayment
GROWTH_PERIOD = timedelta(days=30)      # growth is computed per 30-day period
PERIOD_GROWTH_CAP_RATIO = Decimal("0.5")  # limit can grow at most 50 % per period (V2 sweep: 0.75 bought honest nothing)
COUNTERPARTY_CAP_RATIO = Decimal("0.25")  # one counterparty credits <= 25 % of L_i per period
STARTER_MATURITY = timedelta(days=30)   # a period's repayments count 30 d after it ends
ESTABLISHED_LIMIT = Decimal("1000")     # a new member counts as established at L >= 1000
STALE_DEBT_AGE = timedelta(days=90)     # same threshold as the compression target
SEASONED_WEIGHT = Decimal("1")          # V2: weight of repayment to a peer (v1: 0 = ignored)
SEASONED_MATCH = Decimal("0")           # V2: seasoned credit <= MATCH x established credit
SEASONED_MIN_TENURE = timedelta(days=60)  # V2: a peer must be at least 60 days old
MIN_REPAID_AGE = timedelta(days=1)      # V2: repaid lot must be >= 1 day old to count (v1: 0)
PROMOTED_AS_ANCHOR = False             # V2: promoted NEW members are peers (v1: True = like genesis)
PEER_MODE = "budget"                    # V2: "budget" (cap by the peer's own anchor work) | "match"
PEER_BUDGET_RATIO = Decimal("0.33")     # V2 budget mode: a peer confers <= 0.33 x its own anchor repayment
SEASONED_STRICT = False                 # V2: True = a peer must also have grown and hold no stale debt
STALE_FREEZE_RATIO = Decimal("0.5")     # V2: freeze only if 90-day-old debt > 0.5 x L (v1: 0 = any)


@dataclass(frozen=True)
class StarterPolicy:
    starter_limit: Decimal = STARTER_LIMIT
    growth_rate: Decimal = GROWTH_RATE
    period: timedelta = GROWTH_PERIOD
    period_growth_cap_ratio: Decimal = PERIOD_GROWTH_CAP_RATIO
    counterparty_cap_ratio: Decimal = COUNTERPARTY_CAP_RATIO
    maturity: timedelta = STARTER_MATURITY
    established_limit: Decimal = ESTABLISHED_LIMIT
    stale_debt_age: timedelta = STALE_DEBT_AGE
    seasoned_weight: Decimal = SEASONED_WEIGHT
    seasoned_match: Decimal = SEASONED_MATCH
    seasoned_min_tenure: timedelta = SEASONED_MIN_TENURE
    min_repaid_age: timedelta = MIN_REPAID_AGE
    promoted_as_anchor: bool = PROMOTED_AS_ANCHOR
    peer_mode: str = PEER_MODE
    peer_budget_ratio: Decimal = PEER_BUDGET_RATIO
    seasoned_strict: bool = SEASONED_STRICT
    stale_freeze_ratio: Decimal = STALE_FREEZE_RATIO

    @classmethod
    def v1(cls) -> "StarterPolicy":
        """The fixed-starter (v1) rule: established-only, no age filter."""
        return cls(Decimal("250"), Decimal("0.5"), timedelta(days=30), Decimal("0.5"), Decimal("0.25"),
                   timedelta(days=30), Decimal("1000"), timedelta(days=90),
                   Decimal("0"), Decimal("0"), timedelta(days=60), timedelta(0), True,
                   "match", Decimal("0"), True, Decimal("0"))

    def __post_init__(self):
        for name in ("starter_limit", "growth_rate", "period_growth_cap_ratio",
                     "counterparty_cap_ratio", "established_limit",
                     "seasoned_weight", "seasoned_match", "peer_budget_ratio", "stale_freeze_ratio"):
            v = getattr(self, name)
            object.__setattr__(self, name, D(v))
            if getattr(self, name) < 0:
                raise ValidationError(f"{name} must be >= 0")
        if self.period <= timedelta(0):
            raise ValidationError("period must be > 0")
        if self.peer_mode not in ("match", "budget"):
            raise ValidationError("peer_mode must be 'match' or 'budget'")
        if self.seasoned_weight > 1:
            raise ValidationError("seasoned_weight must be <= 1")
        if self.min_repaid_age < timedelta(0) or self.seasoned_min_tenure < timedelta(0):
            raise ValidationError("min_repaid_age and seasoned_min_tenure must be >= 0")
        if self.maturity < timedelta(0) or self.stale_debt_age <= timedelta(0):
            raise ValidationError("maturity must be >= 0 and stale_debt_age > 0")


@dataclass(frozen=True)
class Repayment:
    """``repayer``'s debt to ``counterparty`` went down by ``amount`` at ``ts``."""

    repayer: str
    counterparty: str
    amount: Decimal
    ts: Optional[datetime]
    via: str                      # "netting" | "cycle"
    counterparty_established: bool
    counterparty_seasoned: bool = False      # V2: frozen at record time
    opened_at: Optional[datetime] = None     # V2: origination of the lot this repaid


@dataclass
class _Member:
    joined_at: datetime
    genesis: bool


class CreditLimits:
    """Per-node earned limit book. Attaches itself to ``ledger`` (``ledger.limits``)."""

    def __init__(self, ledger, policy: StarterPolicy = StarterPolicy()):
        self.ledger = ledger
        self.policy = policy
        self._members: Dict[str, _Member] = {}
        self._budget_cache: Dict[Tuple[str, datetime, datetime], Tuple[Decimal, Decimal]] = {}
        self._max_ts: Optional[datetime] = None
        self._washed: set = set()
        self._wash_seen = 0
        ledger.limits = self

    # ------------------------------------------------------------ V2 caches
    def _invalidate(self) -> None:
        self._budget_cache.clear()
        self._washed.clear()
        self._wash_seen = 0

    def _note_repayment_ts(self, ts: datetime) -> None:
        if self._max_ts is not None and ts < self._max_ts:
            self._budget_cache.clear()              # out-of-order history: recompute budgets
        if self._max_ts is None or ts > self._max_ts:
            self._max_ts = ts

    def is_washed(self, node_id: str) -> bool:
        """V2 non-wash test: named (voucher or vouchee) in a StakeClawedBack event."""
        from .events import StakeClawedBack
        ev = self.ledger.events._events
        for e in ev[self._wash_seen:]:
            if isinstance(e, StakeClawedBack):
                self._washed.update((e.voucher, e.vouchee))
        self._wash_seen = len(ev)
        return node_id in self._washed

    # ------------------------------------------------------------ membership
    def register(self, node_id: str, joined_at: datetime, *, genesis: bool = False) -> None:
        self.ledger.node(node_id)
        if node_id in self._members:
            raise ValidationError(f"{node_id} already registered with the limit book")
        if joined_at.tzinfo is None:
            raise ValidationError("joined_at must be timezone-aware")
        self._members[node_id] = _Member(joined_at, genesis)

    def is_new(self, node_id: str) -> bool:
        m = self._members.get(node_id)
        return m is not None and not m.genesis

    def is_established(self, node_id: str, as_of: datetime) -> bool:
        if not self.is_new(node_id):
            return True
        return self.effective_limit(node_id, as_of) >= self.policy.established_limit

    def standing(self, node_id: str, as_of: datetime) -> Tuple[bool, bool]:
        """(established, seasoned) of a counterparty at ``as_of`` (V2)."""
        if not self.is_new(node_id):
            return True, False
        p = self.policy
        L = self.effective_limit(node_id, as_of)
        if L >= p.established_limit:
            return True, False
        if p.seasoned_weight <= 0:
            return False, False
        m = self._members[node_id]
        if as_of - m.joined_at < p.seasoned_min_tenure or self.is_washed(node_id):
            return False, False
        if p.seasoned_strict:
            if L <= min(self.ledger.node(node_id).credit_ceiling, p.starter_limit):
                return False, False
            if self.ledger.stale_balance(node_id, as_of, p.stale_debt_age) > 0:
                return False, False
        return False, True

    def _eligible(self, r) -> bool:
        p = self.policy
        return (r.ts is not None and r.counterparty != r.repayer
                and (r.opened_at is None or r.ts - r.opened_at >= p.min_repaid_age))

    def _is_anchor_rep(self, r) -> bool:
        return r.counterparty_established and (self.policy.promoted_as_anchor or not self.is_new(r.counterparty))

    def _peer_budget(self, peer: str, start: datetime, end: datetime) -> Tuple[Decimal, Decimal]:
        """(budget, demand) of ``peer`` over [start, end): budget = ratio x the peer's own
        eligible repayment to anchors (uncapped); demand = weighted eligible repayment
        that NEW members made to it while it counted as a peer."""
        key = (peer, start, end)
        hit = self._budget_cache.get(key)
        if hit is not None:
            return hit
        p = self.policy
        own = sum((r.amount for r in self.ledger.repayments(peer)
                   if self._eligible(r) and start <= r.ts < end and self._is_anchor_rep(r)), ZERO)
        dem = sum((p.seasoned_weight * r.amount for r in self.ledger.repayments_to(peer)
                   if self._eligible(r) and start <= r.ts < end and self.is_new(r.repayer)
                   and not self._is_anchor_rep(r) and (r.counterparty_established or r.counterparty_seasoned)), ZERO)
        out = (p.peer_budget_ratio * own, dem)
        if self._max_ts is not None and end <= self._max_ts:
            self._budget_cache[key] = out        # window closed: safe to memoise
        return out

    # ------------------------------------------------------------ the rule
    def growth_schedule(self, node_id: str, as_of: datetime) -> List[Tuple[datetime, Decimal, Decimal, Decimal]]:
        """[(period_end, limit_at_start, credited, growth)] for every matured period."""
        node = self.ledger.node(node_id)
        m = self._members[node_id]
        p = self.policy
        cap = node.credit_ceiling
        L = min(cap, p.starter_limit)
        reps = [r for r in self.ledger.repayments(node_id)
                if self._eligible(r) and (r.counterparty_established or r.counterparty_seasoned)]
        lots = self.ledger.debit_lots(node_id)
        oldest_open = min((l.originated_at for l in lots), default=None)
        out = []
        start = m.joined_at
        while start + p.period + p.maturity <= as_of:
            end = start + p.period
            per_cp: Dict[str, Decimal] = {}
            per_sp: Dict[str, Decimal] = {}
            for r in reps:   # anchor bucket (per_cp) vs peer bucket (per_sp, weighted, matched)
                if start <= r.ts < end:
                    if self._is_anchor_rep(r):
                        per_cp[r.counterparty] = per_cp.get(r.counterparty, ZERO) + r.amount
                    else:
                        per_sp[r.counterparty] = per_sp.get(r.counterparty, ZERO) + p.seasoned_weight * r.amount
            cp_cap = p.counterparty_cap_ratio * L
            est = sum((min(v, cp_cap) for v in per_cp.values()), ZERO)
            if p.peer_mode == "match":
                sea = sum((min(v, cp_cap) for v in per_sp.values()), ZERO)
                credited = est + min(sea, p.seasoned_match * est)
            else:                                     # budget: each peer shares out its own budget
                sea = ZERO
                for y, v in per_sp.items():
                    budget, demand = self._peer_budget(y, start, end)
                    share = v if demand <= budget else (v * budget / demand if demand > 0 else ZERO)
                    sea += min(share, cp_cap)
                credited = est + sea
            growth = min(p.period_growth_cap_ratio * L, p.growth_rate * credited)
            if oldest_open is not None and oldest_open + p.stale_debt_age <= end:
                if p.stale_freeze_ratio <= 0:
                    growth = ZERO                     # bad standing: a 90-day-old debt still open
                else:
                    stale = sum((l.amount for l in lots if l.originated_at + p.stale_debt_age <= end), ZERO)
                    if stale > p.stale_freeze_ratio * L:
                        growth = ZERO                 # V2: freeze only on material stale debt
            out.append((end, L, credited, growth))
            L = min(cap, L + growth)
            start = end
        return out

    def effective_limit(self, node_id: str, as_of: datetime) -> Decimal:
        """Most this node may owe. Unregistered / genesis: its configured ceiling."""
        node = self.ledger.node(node_id)
        if not self.is_new(node_id):
            return node.credit_ceiling
        sched = self.growth_schedule(node_id, as_of)
        if not sched:
            return min(node.credit_ceiling, self.policy.starter_limit)
        _, L, _, g = sched[-1]
        return min(node.credit_ceiling, L + g)

    def spendable_floor(self, node_id: str, as_of: datetime) -> Decimal:
        """Lowest balance a debit leg may push the node to under the starter rule."""
        node = self.ledger.node(node_id)
        return -min(node.available_credit, self.effective_limit(node_id, as_of))
