"""Legal Payload Decision Tree & Rate Card (utility settlement accept test).

Payload generation and the rate card are pure functions. Jurisdiction rules
data, notice-clock sources, and document rendering are INTERFACES — see
INTERFACES.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List

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


def evaluate_settlement(face_value, fixed_filing_cost, days_outstanding: int, cash_offer) -> RateCardResult:
    """Utility Settlement Accept Test.

    floor_price = face_value * α(t) - fixed_filing_cost.
    * cash_offer > 0 AND cash_offer >= max(0, floor_price)  -> ACCEPT
    * floor_price < 0:
        - cash_offer > 0  -> ACCEPT (recovered = offer; remainder written off)
        - otherwise       -> WRITE_OFF the whole balance
    * cash_offer == 0 is NEVER booked as recovered.
    * otherwise REJECT (recovered = 0, written_off = 0).
    """
    face = D(face_value)
    cost = D(fixed_filing_cost)
    offer = D(cash_offer)
    if face < 0 or cost < 0 or offer < 0:
        raise ValidationError("face_value, fixed_filing_cost, cash_offer must be >= 0")
    a = alpha(days_outstanding)
    floor = face * a - cost

    if floor < 0:
        if offer > 0:
            return RateCardResult(SettlementDecision.ACCEPT, a, floor, offer, face - offer, offer, face)
        return RateCardResult(SettlementDecision.WRITE_OFF, a, floor, ZERO, face, offer, face)

    required = max(ZERO, floor)  # == floor when floor >= 0
    if offer > 0 and offer >= required:
        return RateCardResult(SettlementDecision.ACCEPT, a, floor, offer, max(ZERO, face - offer), offer, face)
    return RateCardResult(SettlementDecision.REJECT, a, floor, ZERO, ZERO, offer, face)
