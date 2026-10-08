from decimal import Decimal

import pytest

from clearing_rail.legal import (
    JobContext,
    Jurisdiction,
    PayloadKind as P,
    SettlementDecision as S,
    TxType,
    alpha,
    evaluate_settlement,
    generate_payload,
)
from clearing_rail.types import ValidationError

OH = Jurisdiction("US-OH", allows_cognovit=True)
NY = Jurisdiction("US-NY", allows_cognovit=False)


@pytest.mark.parametrize("tx,jur,prop,notice,expected", [
    (TxType.CONSUMER, NY, False, False, [P.ACCOUNT_STATED]),
    (TxType.CONSUMER, OH, False, False, [P.ACCOUNT_STATED]),                       # cognovit never for consumer
    (TxType.B2B, OH, False, False, [P.ACCOUNT_STATED_WITH_COGNOVIT]),
    (TxType.B2B, NY, False, False, [P.ACCOUNT_STATED]),
    (TxType.B2B, OH, True, True, [P.ACCOUNT_STATED_WITH_COGNOVIT, P.LIEN_ASSIGNMENT_PREAUTH]),
    (TxType.CONSUMER, NY, True, True, [P.ACCOUNT_STATED, P.LIEN_ASSIGNMENT_PREAUTH]),
    (TxType.B2B, NY, True, False, [P.ACCOUNT_STATED]),                              # notice clock closed
    (TxType.B2B, NY, False, True, [P.ACCOUNT_STATED]),                              # not real property
])
def test_payload_tree(tx, jur, prop, notice, expected):
    assert generate_payload(JobContext(tx, jur, prop, notice)) == expected


def test_unknown_tx_type():
    with pytest.raises(ValidationError):
        generate_payload(JobContext("Lease", NY, False, False))


@pytest.mark.parametrize("days,expected", [
    (0, "0.70"), (180, "0.70"), (181, "0.701"), (300, "0.820"),
    (479, "0.999"), (480, "1.000"), (481, "1.0"), (5000, "1.0"),
])
def test_alpha_and_cap(days, expected):
    assert alpha(days) == Decimal(expected)


def test_alpha_negative_days():
    with pytest.raises(ValidationError):
        alpha(-1)


def test_accept_at_exact_floor_and_reject_below():
    # floor = 1000 * 0.70 - 100 = 600
    r = evaluate_settlement(Decimal(1000), Decimal(100), 180, Decimal(600))
    assert (r.decision, r.recovered, r.written_off, r.floor_price) == (S.ACCEPT, Decimal(600), Decimal(400), Decimal("600.00"))
    r = evaluate_settlement(Decimal(1000), Decimal(100), 180, Decimal("599.99"))
    assert (r.decision, r.recovered, r.written_off) == (S.REJECT, 0, 0)


def test_zero_offer_never_recovered_when_floor_positive():
    r = evaluate_settlement(Decimal(1000), Decimal(100), 200, Decimal(0))
    assert r.decision is S.REJECT and r.recovered == 0


def test_floor_exactly_zero():
    # 100 * 0.70 - 70 = 0
    assert evaluate_settlement(Decimal(100), Decimal(70), 0, Decimal(0)).decision is S.REJECT
    r = evaluate_settlement(Decimal(100), Decimal(70), 0, Decimal("0.01"))
    assert r.decision is S.ACCEPT and r.recovered == Decimal("0.01")


def test_negative_floor_positive_offer_accepts_and_writes_off_rest():
    # floor = 100 * 0.70 - 500 = -430
    r = evaluate_settlement(Decimal(100), Decimal(500), 10, Decimal(5))
    assert r.floor_price == Decimal("-430.00")
    assert (r.decision, r.recovered, r.written_off) == (S.ACCEPT, Decimal(5), Decimal(95))


def test_negative_floor_zero_offer_writes_off_whole_balance_and_books_nothing():
    r = evaluate_settlement(Decimal(100), Decimal(500), 10, Decimal(0))
    assert (r.decision, r.recovered, r.written_off) == (S.WRITE_OFF, Decimal(0), Decimal(100))


def test_alpha_cap_feeds_floor():
    r = evaluate_settlement(Decimal(1000), Decimal(0), 10_000, Decimal(999))
    assert r.alpha == Decimal("1.0") and r.decision is S.REJECT
    assert evaluate_settlement(Decimal(1000), Decimal(0), 10_000, Decimal(1000)).decision is S.ACCEPT


def test_negative_inputs_refused():
    with pytest.raises(ValidationError):
        evaluate_settlement(Decimal(100), Decimal(0), 0, Decimal(-1))
