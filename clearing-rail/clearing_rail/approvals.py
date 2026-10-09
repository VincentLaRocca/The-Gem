"""TIERED APPROVAL with RISK-BASED ESCALATION (k-of-n sign-off on large or suspect
credit extensions and stake unlocks).

STUB-DEPENDENT: signatures go through the package's ``Verifier`` / ``KeyRegistry``
interface (INTERFACES #2). In tests and the Monte Carlo battery that is an HMAC test
double. This is a *tiered approval rule*, NOT Bitcoin multisig: nothing here is a
script, a key aggregation scheme, or an on-chain spend condition. Panel selection
(which n members are asked) is also a deterministic stand-in for a committee
process (sha256 ordering), and *whether* an honest signer should approve is a human
judgement the code does not model.

Rule (all Decimal, no wall clock)
---------------------------------
* ``exposure`` = what the subject would owe after the debit (credit) or the edge's
  cumulative released stake after the unlock (unlock).
* size tier:  exposure <= TIER_THRESHOLDS[0] -> 0 (no sign-off)
              <= TIER_THRESHOLDS[1]          -> 1 (2-of-3)
              above                          -> 2 (3-of-5)
* suspicion score (deterministic, from existing data, weights/thresholds tunable,
  see SIGNAL_WEIGHTS): young account, low counterparty diversity, round-trip
  (wash-like) repayments near-miss / hit, router misses / router bar, sudden
  limit-maxing, recent stake clawback.
* bump = 0 if score < BUMP_SCORES[0]; 1 if < BUMP_SCORES[1]; else 2.
* required tier = size tier + bump; above the top tier -> REFUSED outright.
* A request picks a panel of n eligible signers (established, not the subject or
  the parties, not barred): members who vouch for the subject (they carry the risk)
  first, then other established members in sha256(request_id|node) order.
* Signer timeout, same handling as solver routers: a request not reaching k within
  APPROVAL_WINDOW expires; each silent panel member gets a miss; at
  SIGNER_MISS_THRESHOLD misses it is barred from panels for SIGNER_MISS_BAR.
  The requester may open a new request (fresh panel, barred signers excluded).
* The panel seed is the book's own counter, so a requester cannot grind request ids
  for a friendly panel; a request declined by the panel puts that (kind, key) on a
  REJECT_COOLDOWN before it can be asked again (a timeout does not).
* An approval yields a grant valid for GRANT_VALIDITY covering exposure up to the
  approved amount at the approved tier.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from .crypto import canonical_amount
from .events import Event, StakeClawedBack
from .types import ZERO, CreditLimitExceeded, D, IllegalTransition, SignatureError, ValidationError

TIER_THRESHOLDS = (Decimal("1000"), Decimal("5000"))      # exposure above -> tier 1 / tier 2
TIER_QUORUM = {1: (2, 3), 2: (3, 5)}                      # tier -> (k, n)
APPROVAL_WINDOW = timedelta(hours=12)                     # same as the target signature window
SIGNER_MISS_THRESHOLD = 2                                 # same as ROUTER_MISS_THRESHOLD
SIGNER_MISS_BAR = timedelta(days=30)                      # same as ROUTER_MISS_BAR
GRANT_VALIDITY = timedelta(days=30)
REJECT_COOLDOWN = timedelta(days=7)      # after a declined request, the same (kind, key) waits 7 d

# ---- suspicion signals (ALL TUNABLE) ---------------------------------------------
YOUNG_ACCOUNT_AGE = timedelta(days=90)
DIVERSITY_LOOKBACK = timedelta(days=90)
MIN_DISTINCT_COUNTERPARTIES = 3
ROUNDTRIP_LOOKBACK = timedelta(days=60)
ROUNDTRIP_GAP = timedelta(hours=24)       # repayment within 24 h of borrowing from the same member
ROUNDTRIP_TOLERANCE = Decimal("0.05")     # ... of an amount within +-5 %
ROUNDTRIP_NEAR = Decimal("0.25")          # share of repaid volume that is round-trip: near-miss
ROUNDTRIP_HIT = Decimal("0.50")           # ... hit
LIMIT_MAX_RATIO = Decimal("0.90")         # exposure >= 90 % of the effective limit ...
PRIOR_PEAK_RATIO = Decimal("0.50")        # ... while the 90-day peak debt was < 50 % of the exposure
PEAK_LOOKBACK = timedelta(days=90)
RAMP_EXCLUDE = timedelta(days=7)        # debt built in the last 7 days is not 'history'
CLAWBACK_LOOKBACK = timedelta(days=90)
ROUTER_MISS_LOOKBACK = timedelta(days=90)
SIGNAL_WEIGHTS = {
    "young_account": 1, "low_diversity": 1, "roundtrip_near": 1, "roundtrip_hit": 2,
    "router_miss": 1, "router_barred": 2, "limit_maxing": 1, "recent_clawback": 2,
}
BUMP_SCORES = (3, 5)                      # score >= 3 -> +1 tier, >= 5 -> +2 tiers
MAX_TIER = 2


@dataclass(frozen=True)
class ApprovalPolicy:
    """V2 config flags. Both OFF by default: an ApprovalBook built with the default
    policy never blocks a debit or an unlock (the code stays, the gate is disabled)."""

    tiers: bool = False          # k-of-n sign-off on large exposure
    escalation: bool = False     # risk-score tier bump (only meaningful with tiers on)


def size_tier(exposure: Decimal) -> int:
    exposure = D(exposure)
    if exposure <= TIER_THRESHOLDS[0]:
        return 0
    if exposure <= TIER_THRESHOLDS[1]:
        return 1
    return 2


def bump_for(score: int) -> int:
    return 0 if score < BUMP_SCORES[0] else (1 if score < BUMP_SCORES[1] else 2)


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EXPIRED = "expired"
    REJECTED = "rejected"


_TRANSITIONS = {
    ApprovalState.PENDING: {ApprovalState.APPROVED, ApprovalState.EXPIRED, ApprovalState.REJECTED},
    ApprovalState.APPROVED: set(), ApprovalState.EXPIRED: set(), ApprovalState.REJECTED: set(),
}


class ApprovalRequired(CreditLimitExceeded):
    """Debit refused until a k-of-n grant covers it. ``tier`` and ``signals`` say why."""

    def __init__(self, msg, subject, exposure, tier, signals, refused=False):
        super().__init__(msg)
        self.subject, self.exposure, self.tier, self.signals, self.refused = subject, exposure, tier, signals, refused


@dataclass(frozen=True)
class ApprovalRequested(Event):
    request_id: str
    subject: str
    kind: str
    exposure: Decimal
    tier: int
    panel: Tuple[str, ...]
    signals: Tuple[str, ...]


@dataclass(frozen=True)
class ApprovalResolved(Event):
    request_id: str
    state: str
    signers: Tuple[str, ...]


@dataclass(frozen=True)
class SignerMissFlagged(Event):
    request_id: str
    node_id: str
    misses: int
    barred_until: datetime


@dataclass(frozen=True)
class Assessment:
    exposure: Decimal
    size_tier: int
    score: int
    signals: Tuple[str, ...]
    required_tier: int
    refused: bool


@dataclass
class ApprovalRequest:
    request_id: str
    subject: str
    kind: str                     # "credit" | "unlock"
    key: Tuple[str, ...]          # (subject,) for credit, (voucher, vouchee) for unlock
    exposure: Decimal
    tier: int
    k: int
    panel: Tuple[str, ...]
    opened_at: datetime
    deadline: datetime
    state: ApprovalState = ApprovalState.PENDING
    signed: Dict[str, datetime] = field(default_factory=dict)
    declined: Set[str] = field(default_factory=set)

    def _to(self, new: ApprovalState):
        if new not in _TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.request_id}: {self.state.value} -> {new.value}")
        self.state = new


@dataclass(frozen=True)
class Grant:
    kind: str
    key: Tuple[str, ...]
    tier: int
    max_exposure: Decimal
    valid_until: datetime
    request_id: str


def approval_message(req: ApprovalRequest) -> bytes:
    parts = ["clearing-rail/approval/v1", req.request_id, req.kind, "|".join(req.key),
             canonical_amount(req.exposure), str(req.tier), req.deadline.isoformat()]
    return hashlib.sha256("\x1f".join(parts).encode()).digest()


class ApprovalBook:
    def __init__(self, ledger, limits, node_keys, verifier, *, graph=None, amort=None, loop=None,
                 policy: ApprovalPolicy = ApprovalPolicy()):
        self.ledger, self.limits, self.node_keys, self.verifier = ledger, limits, node_keys, verifier
        self.policy = policy
        self.graph, self.amort, self.loop = graph, amort, loop
        self._requests: Dict[str, ApprovalRequest] = {}
        self._grants: List[Grant] = []
        self._misses: Dict[str, int] = {}
        self._barred: Dict[str, datetime] = {}
        self._ev_seen = 0
        self._clawbacks: List[StakeClawedBack] = []
        self._seq = 0                                   # panel seed is the book's, never the caller's
        self._cooldown: Dict[Tuple[str, Tuple[str, ...]], datetime] = {}
        ledger.approvals = self

    # ------------------------------------------------------------ signals
    def signals(self, subject: str, exposure: Decimal, as_of: datetime) -> Tuple[str, ...]:
        L = self.ledger
        out = []
        m = self.limits._members.get(subject)
        if m is not None and not m.genesis and as_of - m.joined_at < YOUNG_ACCOUNT_AGE:
            out.append("young_account")
        ts = L.transfers_of(subject, as_of - DIVERSITY_LOOKBACK, as_of)
        cps = {t.payee if t.payer == subject else t.payer for t in ts}
        if len(cps) < MIN_DISTINCT_COUNTERPARTIES:
            out.append("low_diversity")
        # round-trip repayments (wash signature): repaid to a member it borrowed a
        # matching amount from within ROUNDTRIP_GAP before
        reps = [r for r in L.repayments(subject) if r.ts is not None and as_of - ROUNDTRIP_LOOKBACK < r.ts <= as_of]
        rep_total = sum((r.amount for r in reps), ZERO)
        if rep_total > 0:
            buys = [t for t in L.transfers_of(subject, as_of - ROUNDTRIP_LOOKBACK - ROUNDTRIP_GAP, as_of)
                    if t.payer == subject]
            rt = ZERO
            for r in reps:
                for t in buys:
                    if (t.payee == r.counterparty and timedelta(0) <= r.ts - t.ts <= ROUNDTRIP_GAP
                            and abs(t.amount - r.amount) <= ROUNDTRIP_TOLERANCE * t.amount):
                        rt += r.amount
                        break
            share = rt / rep_total
            if share >= ROUNDTRIP_HIT:
                out.append("roundtrip_hit")
            elif share >= ROUNDTRIP_NEAR:
                out.append("roundtrip_near")
        if self.loop is not None:
            bar = self.loop.barred_until(subject)
            if bar is not None and bar > as_of:
                out.append("router_barred")
            elif self.loop.router_misses(subject) > 0 and \
                    as_of - self.loop._last_miss.get(subject, as_of) <= ROUTER_MISS_LOOKBACK:
                out.append("router_miss")
        # sudden limit-maxing: near the effective limit with no comparable prior debt
        lim = self.limits.effective_limit(subject, as_of) if subject in self.limits._members else None
        if lim is not None and lim > 0 and exposure >= LIMIT_MAX_RATIO * lim:
            # replay the balance backwards; only debt held at least RAMP_EXCLUDE ago counts as
            # history, so a fast ramp in several small steps is still "sudden"
            bal = L.node(subject).current_balance
            peak = ZERO
            for t in reversed(L.transfers_of(subject, as_of - PEAK_LOOKBACK, as_of)):
                if t.ts <= as_of - RAMP_EXCLUDE:
                    peak = max(peak, -bal)
                bal = bal + t.amount if t.payer == subject else bal - t.amount   # balance before t
            peak = max(peak, -bal)
            if peak < PRIOR_PEAK_RATIO * exposure:      # no comparable debt between 90 d and 7 d ago
                out.append("limit_maxing")
        raw = getattr(L.events, "_events", None)
        evs = raw if raw is not None else list(L.events)
        n_ev = len(evs)
        for i in range(self._ev_seen, n_ev):            # incremental scan of the append-only log
            if isinstance(evs[i], StakeClawedBack):
                self._clawbacks.append(evs[i])
        self._ev_seen = n_ev
        for e in self._clawbacks:
            if subject in (e.voucher, e.vouchee) and as_of - CLAWBACK_LOOKBACK < e.ts <= as_of:
                out.append("recent_clawback")
                break
        return tuple(out)

    def assess(self, subject: str, exposure, as_of: datetime) -> Assessment:
        exposure = D(exposure)
        st = size_tier(exposure)
        sig = self.signals(subject, exposure, as_of)
        score = sum(SIGNAL_WEIGHTS[s] for s in sig)
        req = st + (bump_for(score) if self.policy.escalation else 0)
        return Assessment(exposure, st, score, sig, min(req, MAX_TIER), req > MAX_TIER)

    # ------------------------------------------------------------ grants / enforcement
    def grant_for(self, kind: str, key: Tuple[str, ...], exposure: Decimal, tier: int, as_of: datetime):
        for g in self._grants:
            if g.kind == kind and g.key == key and g.tier >= tier and g.max_exposure >= exposure \
                    and g.valid_until >= as_of:
                return g
        return None

    def _gate(self, kind, key, subject, exposure, as_of):
        if not self.policy.tiers:
            return None                                  # V2: approvals OFF by default
        if exposure <= 0 or subject not in self.limits._members:
            return None
        a = self.assess(subject, exposure, as_of)
        if a.refused:
            raise ApprovalRequired(f"{subject}: {kind} exposure {exposure} refused (risk tier above {MAX_TIER}: "
                                   f"{','.join(a.signals)})", subject, exposure, a.required_tier, a.signals, True)
        if a.required_tier == 0 or self.grant_for(kind, key, exposure, a.required_tier, as_of):
            return None
        raise ApprovalRequired(f"{subject}: {kind} exposure {exposure} needs tier-{a.required_tier} approval "
                               f"({','.join(a.signals) or 'size'})", subject, exposure, a.required_tier, a.signals)

    def check_credit(self, debtor: str, new_balance: Decimal, as_of: datetime) -> None:
        self._gate("credit", (debtor,), debtor, max(ZERO, -D(new_balance)), as_of)

    def check_unlock(self, voucher: str, vouchee: str, cumulative: Decimal, as_of: datetime) -> None:
        self._gate("unlock", (voucher, vouchee), vouchee, D(cumulative), as_of)

    # ------------------------------------------------------------ panel + state machine
    def is_barred(self, node: str, as_of: datetime) -> bool:
        b = self._barred.get(node)
        return b is not None and b > as_of

    def signer_misses(self, node: str) -> int:
        return self._misses.get(node, 0)

    def eligible_signers(self, exclude, as_of: datetime) -> List[str]:
        return [n for n in self.limits._members
                if n not in exclude and not self.is_barred(n, as_of) and self.limits.is_established(n, as_of)]

    def panel(self, seed: str, subject, exclude, n, as_of) -> Tuple[str, ...]:
        elig = set(self.eligible_signers(exclude, as_of))
        risk = sorted({e.voucher for e in self.graph.edges() if e.vouchee == subject} & elig) if self.graph else []
        rest = sorted(elig - set(risk), key=lambda x: hashlib.sha256(f"{seed}|{x}".encode()).digest())
        return tuple((risk + rest)[:n])

    def open(self, request_id: str, kind: str, key: Tuple[str, ...], exposure, as_of: datetime) -> ApprovalRequest:
        if request_id in self._requests:
            raise ValidationError(f"duplicate approval request {request_id}")
        if kind not in ("credit", "unlock"):
            raise ValidationError("kind must be credit or unlock")
        subject = key[0] if kind == "credit" else key[1]
        cd = self._cooldown.get((kind, tuple(key)))
        if cd is not None and as_of < cd:
            raise ValidationError(f"{request_id}: {kind} {key} is cooling down until {cd.isoformat()}")
        exposure = D(exposure)
        a = self.assess(subject, exposure, as_of)
        if a.refused or a.required_tier == 0:
            raise ValidationError(f"{request_id}: nothing to approve (refused={a.refused}, tier={a.required_tier})")
        k, n = TIER_QUORUM[a.required_tier]
        self._seq += 1
        pnl = self.panel(f"{self._seq}|{kind}|{'|'.join(key)}|{as_of.isoformat()}", subject, set(key), n, as_of)
        req = ApprovalRequest(request_id, subject, kind, tuple(key), exposure, a.required_tier, k, pnl,
                              as_of, as_of + APPROVAL_WINDOW)
        self._requests[request_id] = req
        self.ledger.events.append(ApprovalRequested(as_of, request_id, subject, kind, exposure, a.required_tier,
                                                    pnl, a.signals))
        if len(pnl) < n:
            req._to(ApprovalState.REJECTED)
            self.ledger.events.append(ApprovalResolved(as_of, request_id, "rejected_no_panel", ()))
        return req

    def get(self, request_id: str) -> ApprovalRequest:
        return self._requests[request_id]

    def sign(self, request_id: str, signer: str, signature: bytes, as_of: datetime) -> ApprovalRequest:
        req = self._requests[request_id]
        if req.state is not ApprovalState.PENDING:
            raise IllegalTransition(f"{request_id} is {req.state.value}")
        if as_of > req.deadline:
            raise SignatureError(f"{request_id}: approval window closed")
        if signer not in req.panel:
            raise ValidationError(f"{signer} is not on the panel of {request_id}")
        key = self.node_keys.get(signer)
        if key is None or not self.verifier.verify(key, approval_message(req), signature):
            raise SignatureError(f"bad approval signature from {signer}")
        req.signed[signer] = as_of
        if len(req.signed) >= req.k:
            req._to(ApprovalState.APPROVED)
            self._grants.append(Grant(req.kind, req.key, req.tier, req.exposure, as_of + GRANT_VALIDITY, request_id))
            self.ledger.events.append(ApprovalResolved(as_of, request_id, "approved", tuple(sorted(req.signed))))
        return req

    def decline(self, request_id: str, signer: str, as_of: datetime) -> ApprovalRequest:
        req = self._requests[request_id]
        if req.state is not ApprovalState.PENDING or signer not in req.panel or signer in req.signed:
            raise ValidationError(f"{signer} cannot decline {request_id}")
        req.declined.add(signer)
        if len(req.panel) - len(req.declined) < req.k:
            req._to(ApprovalState.REJECTED)
            self._cooldown[(req.kind, req.key)] = as_of + REJECT_COOLDOWN
            self.ledger.events.append(ApprovalResolved(as_of, request_id, "rejected", tuple(sorted(req.signed))))
        return req

    def tick(self, now: datetime) -> List[ApprovalRequest]:
        """Expire overdue requests; silent panel members get a miss (decliners did answer)."""
        expired = []
        for req in self._requests.values():
            if req.state is ApprovalState.PENDING and now > req.deadline:
                req._to(ApprovalState.EXPIRED)
                expired.append(req)
                self.ledger.events.append(ApprovalResolved(now, req.request_id, "expired", tuple(sorted(req.signed))))
                for s in req.panel:
                    if s in req.signed or s in req.declined:
                        continue
                    self._misses[s] = self._misses.get(s, 0) + 1
                    if self._misses[s] >= SIGNER_MISS_THRESHOLD:
                        self._barred[s] = req.deadline + SIGNER_MISS_BAR
                        self.ledger.events.append(SignerMissFlagged(now, req.request_id, s, self._misses[s],
                                                                    self._barred[s]))
        return expired
