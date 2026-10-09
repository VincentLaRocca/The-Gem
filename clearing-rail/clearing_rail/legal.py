"""Legal Payload Decision Tree & Rate Card (utility settlement accept test).

Payload generation and the rate card are pure functions. Jurisdiction rules
data, notice-clock sources, and document rendering are INTERFACES — see
INTERFACES.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .types import ZERO, D, ValidationError


class TxType(str, Enum):
    CONSUMER = "Consumer"
    B2B = "B2B"


class PayloadKind(str, Enum):
    ACCOUNT_STATED = "AccountStated"
    ACCOUNT_STATED_WITH_COGNOVIT = "AccountStated_With_Cognovit"
    LIEN_ASSIGNMENT_PREAUTH = "LienAssignment_PreAuth"


@dataclass(frozen=True)
class Jurisdiction:
    code: str
    allows_cognovit: bool


@dataclass(frozen=True)
class JobContext:
    tx_type: TxType
    jurisdiction: Jurisdiction
    is_real_property: bool
    notice_clock_open: bool


def generate_payload(ctx: JobContext) -> List[PayloadKind]:
    """Ordered list of payload kinds for the contract at job start.

    * Consumer            -> AccountStated (cognovit NEVER for Consumer)
    * B2B + cognovit ok   -> AccountStated_With_Cognovit
    * B2B + no cognovit   -> AccountStated
    * real property AND notice_clock_open -> append LienAssignment_PreAuth
      (applies to any tx_type)
    """
    if ctx.tx_type is TxType.CONSUMER:
        base = PayloadKind.ACCOUNT_STATED
    elif ctx.tx_type is TxType.B2B:
        base = (PayloadKind.ACCOUNT_STATED_WITH_COGNOVIT
                if ctx.jurisdiction.allows_cognovit
                else PayloadKind.ACCOUNT_STATED)
    else:
        raise ValidationError(f"unknown tx_type {ctx.tx_type}")
    out: List[PayloadKind] = [base]
    if ctx.is_real_property and ctx.notice_clock_open:
        out.append(PayloadKind.LIEN_ASSIGNMENT_PREAUTH)
    return out


# ------------------------------------------------------------------ rate card
# FIX (S6, approved): no offer below this fraction of the claim's face is ever
# ACCEPTed, even when floor_price < 0 (a zero offer on a negative floor is still
# a WRITE_OFF, as before).
MIN_RECOVERY_RATIO = Decimal("0.10")

class SettlementDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    WRITE_OFF = "WRITE_OFF"


@dataclass(frozen=True)
class RateCardResult:
    decision: SettlementDecision
    alpha: Decimal
    floor_price: Decimal
    recovered: Decimal
    written_off: Decimal
    cash_offer: Decimal
    face_value: Decimal


def alpha(days_outstanding: int) -> Decimal:
    """α(t) = 0.70 + t_days_past_180 * 0.001, capped at 1.0.
    t_days_past_180 = max(0, days_outstanding - 180)."""
    if days_outstanding < 0:
        raise ValidationError("days_outstanding must be >= 0")
    t = max(0, int(days_outstanding) - 180)
    a = Decimal("0.70") + Decimal(t) * Decimal("0.001")
    return min(a, Decimal("1.0"))


def evaluate_settlement(face_value, fixed_filing_cost, days_outstanding: int, cash_offer,
                        *, exposure=None) -> RateCardResult:
    """Utility Settlement Accept Test.

    exposure (FIX S6) = the debtor's total face owed to this creditor, including
    this claim (defaults to ``face_value``: a single claim). The filing cost is
    paid once per exposure, so this claim's floor is its pro-rata share of the
    aggregate floor:

        floor_price = face_value * α(t) - fixed_filing_cost * face_value / exposure
        min_recovery = MIN_RECOVERY_RATIO * face_value

    * floor_price >= 0: ACCEPT iff cash_offer > 0 and cash_offer >= max(floor_price, min_recovery)
    * floor_price < 0:
        - cash_offer >= min_recovery (and > 0) -> ACCEPT (remainder written off)
        - cash_offer == 0                      -> WRITE_OFF the whole balance
        - otherwise                            -> REJECT
    * cash_offer == 0 is NEVER booked as recovered.
    """
    face = D(face_value)
    cost = D(fixed_filing_cost)
    offer = D(cash_offer)
    if face < 0 or cost < 0 or offer < 0:
        raise ValidationError("face_value, fixed_filing_cost, cash_offer must be >= 0")
    exp = face if exposure is None else D(exposure)
    if exp < face:
        raise ValidationError("exposure must be >= face_value")
    a = alpha(days_outstanding)
    cost_share = cost if exp == face else (cost * face / exp if exp > 0 else ZERO)
    floor = face * a - cost_share
    min_recovery = MIN_RECOVERY_RATIO * face

    if floor < 0:
        if offer > 0 and offer >= min_recovery:
            return RateCardResult(SettlementDecision.ACCEPT, a, floor, offer, max(ZERO, face - offer), offer, face)
        if offer == 0:
            return RateCardResult(SettlementDecision.WRITE_OFF, a, floor, ZERO, face, offer, face)
        return RateCardResult(SettlementDecision.REJECT, a, floor, ZERO, ZERO, offer, face)

    required = max(floor, min_recovery)
    if offer > 0 and offer >= required:
        return RateCardResult(SettlementDecision.ACCEPT, a, floor, offer, max(ZERO, face - offer), offer, face)
    return RateCardResult(SettlementDecision.REJECT, a, floor, ZERO, ZERO, offer, face)


class ClaimBook:
    """FIX (S6): aggregates every claim a debtor owes one creditor, so splitting a
    debt into many small claims cannot push each piece's floor below zero.

    Exposure for a (debtor, creditor) pair = total face of every claim ever
    registered for that pair in this book (open or closed), so settling pieces
    one by one does not shrink the basis either."""

    def __init__(self):
        self._claims: Dict[int, Tuple[str, str, Decimal]] = {}
        self._closed: Dict[int, SettlementDecision] = {}
        self._next = 1

    def add_claim(self, debtor: str, creditor: str, face_value) -> int:
        face = D(face_value)
        if face <= 0:
            raise ValidationError("claim face must be > 0")
        if debtor == creditor:
            raise ValidationError("self-claim")
        cid = self._next
        self._next += 1
        self._claims[cid] = (debtor, creditor, face)
        return cid

    def exposure(self, debtor: str, creditor: str) -> Decimal:
        return sum((f for d, c, f in self._claims.values() if (d, c) == (debtor, creditor)), ZERO)

    def evaluate(self, claim_id: int, fixed_filing_cost, days_outstanding: int, cash_offer) -> RateCardResult:
        if claim_id not in self._claims:
            raise ValidationError(f"unknown claim {claim_id}")
        if claim_id in self._closed:
            raise ValidationError(f"claim {claim_id} already {self._closed[claim_id].value}")
        d, c, face = self._claims[claim_id]
        r = evaluate_settlement(face, fixed_filing_cost, days_outstanding, cash_offer, exposure=self.exposure(d, c))
        if r.decision is not SettlementDecision.REJECT:
            self._closed[claim_id] = r.decision
        return r
