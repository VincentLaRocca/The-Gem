"""TIERED APPROVAL + risk escalation (STUB-DEPENDENT on signatures: HMAC test double)."""
from datetime import timedelta
from decimal import Decimal

import pytest

from clearing_rail.amortization import AmortizationEngine, AmortizationStatus as S
from clearing_rail.approvals import (
    APPROVAL_WINDOW, BUMP_SCORES, REJECT_COOLDOWN, GRANT_VALIDITY, SIGNAL_WEIGHTS, SIGNER_MISS_BAR, SIGNER_MISS_THRESHOLD,
    TIER_QUORUM, TIER_THRESHOLDS, ApprovalBook, ApprovalPolicy, ApprovalRequired, ApprovalState as A, approval_message,
    bump_for, size_tier,
)
from clearing_rail.crypto import KeyRegistry
from clearing_rail.events import StakeClawedBack
from clearing_rail.ledger import Ledger
from clearing_rail.limits import CreditLimits
from clearing_rail.types import IllegalTransition, Node, SignatureError, ValidationError
from clearing_rail.vouch import VouchGraph
from conftest import T0, HmacVerifier, hsign, secret_for

D = Decimal
DAY = timedelta(days=1)
H = timedelta(hours=1)
GEN = [f"G{i}" for i in range(8)]


def world(new=("M",), new_ceiling="20000", gen_ceiling="100000", policy=None):
    policy = policy or ApprovalPolicy(tiers=True, escalation=True)
    l = Ledger()
    lim = CreditLimits(l)
    keys = KeyRegistry()
    for g in GEN:
        l.add_node(Node(g, D(gen_ceiling)))
        lim.register(g, T0, genesis=True)
        keys.register(g, secret_for(g))
    for n in new:
        l.add_node(Node(n, D(new_ceiling)))
        lim.register(n, T0)
        keys.register(n, secret_for(n))
    g = VouchGraph(l)
    am = AmortizationEngine(l, g)
    book = ApprovalBook(l, lim, keys, HmacVerifier(), graph=g, amort=am, policy=policy)
    return l, lim, book, g, am


def sign(book, rid, who, ts):
    return book.sign(rid, who, hsign(secret_for(who), approval_message(book.get(rid))), ts)


def diversify(l, node, ts, cps=("G5", "G6", "G7")):
    for c in cps:
        l.record_trade(c, node, D(1), ts)


def test_size_tiers_and_bumps():
    assert TIER_THRESHOLDS == (D(1000), D(5000)) and TIER_QUORUM == {1: (2, 3), 2: (3, 5)}
    assert [size_tier(D(x)) for x in ("1000", "1000.01", "5000", "5000.01")] == [0, 1, 1, 2]
    assert [bump_for(s) for s in (0, 2, 3, 4, 5, 9)] == [0, 0, 1, 1, 2, 2]
    assert BUMP_SCORES == (3, 5) and APPROVAL_WINDOW == 12 * H


def test_large_honest_genesis_loan_needs_2_of_3_then_grant_covers_it():
    l, lim, book, *_ = world()
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    l.record_trade("G0", "G1", D(1003), t)                        # exposure exactly 1000: free
    with pytest.raises(ApprovalRequired) as e:
        l.record_trade("G0", "G1", D(1), t)
    assert e.value.tier == 1 and not e.value.refused
    req = book.open("r1", "credit", ("G0",), D(4000), t)
    assert (req.k, len(req.panel)) == (2, 3) and "G0" not in req.panel
    sign(book, "r1", req.panel[0], t + H)
    assert req.state is A.PENDING
    sign(book, "r1", req.panel[1], t + 2 * H)
    assert req.state is A.APPROVED
    l.record_trade("G0", "G1", D(3000), t + 3 * H)                # exposure 4000 covered
    with pytest.raises(ApprovalRequired):
        l.record_trade("G0", "G1", D(4), t + 3 * H)               # 4001 > granted exposure (G0 had +3)


def test_grant_expires():
    l, lim, book, *_ = world()
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    req = book.open("r1", "credit", ("G0",), D(2000), t)
    for s in req.panel[:2]:
        sign(book, "r1", s, t)
    l.record_trade("G0", "G1", D(2000), t)
    l.record_trade("G1", "G0", D(2000), t)
    with pytest.raises(ApprovalRequired):
        l.record_trade("G0", "G1", D(2000), t + GRANT_VALIDITY + H)


def test_top_tier_needs_3_of_5_and_suspect_top_tier_is_refused():
    l, lim, book, *_ = world()
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    req = book.open("r1", "credit", ("G0",), D(6000), t)
    assert (req.k, len(req.panel)) == (3, 5)
    # G2 has no counterparties (low_diversity 1) + clawback (2) = 3 -> bump 1 -> a 6000 loan is refused
    l.events.append(StakeClawedBack(t - DAY, "G2", "X", D(1)))
    with pytest.raises(ApprovalRequired) as e:
        l.record_trade("G2", "G1", D(6000), t)
    assert e.value.refused and set(e.value.signals) == {"low_diversity", "recent_clawback"}


def test_small_suspect_loan_escalates_to_2_of_3():
    l, lim, book, *_ = world()
    # brand-new M: young (1) + low diversity (1) + maxing its 250 starter with no prior debt (1) = 3
    a = book.assess("M", D(250), T0 + H)
    assert set(a.signals) == {"young_account", "low_diversity", "limit_maxing"} and a.score == 3
    assert a.size_tier == 0 and a.required_tier == 1
    with pytest.raises(ApprovalRequired):
        l.record_trade("M", "G1", D(250), T0 + H)
    l.record_trade("M", "G1", D(100), T0 + H)                     # 40 % of the limit: not maxing
    assert book.assess("M", D(100), T0 + H).required_tier == 0
    # ramping in two steps inside 7 days is still sudden; debt held > 7 days is history
    l.record_trade("M", "G1", D(100), T0 + 2 * H)                 # exposure 200 < 90 % of 250: ok
    assert "limit_maxing" in book.signals("M", D(250), T0 + 3 * H)
    assert "limit_maxing" not in book.signals("M", D(250), T0 + 9 * DAY)


def test_roundtrip_wash_signal():
    l, lim, book, *_ = world()
    t = T0 + 100 * DAY
    l.approvals = None                                             # build history without the gate
    for k in range(4):
        l.record_trade("M", "G1", D(50), t + k * DAY)
        l.record_trade("G1", "M", D(50), t + k * DAY + H)         # repaid within 24 h, same amount
    l.approvals = book
    assert "roundtrip_hit" in book.signals("M", D(10), t + 5 * DAY)
    with pytest.raises(ApprovalRequired):                          # wash hit 2 + low diversity 1 = 3 -> 2-of-3
        l.record_trade("M", "G1", D(10), t + 5 * DAY)
    l2, _, book2, *_ = world()
    l2.approvals = None
    for k in range(4):
        l2.record_trade("M", "G1", D(50), t + 5 * k * DAY)
        l2.record_trade("G1", "M", D(50), t + 5 * k * DAY + 3 * DAY)  # repaid days later: ordinary trade
    assert not {"roundtrip_hit", "roundtrip_near"} & set(book2.signals("M", D(10), t + 20 * DAY))


def test_signer_timeout_counts_misses_bars_and_rerun_replaces_signer():
    l, lim, book, g, _ = world()
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    g.vouch("G7", "G0", t - DAY)                                   # risk carrier: always on G0's panel
    quiet = "G7"
    for i in range(SIGNER_MISS_THRESHOLD):
        req = book.open(f"r{i}", "credit", ("G0",), D(2000), t)
        assert req.panel[0] == quiet
        others = [p for p in req.panel if p != quiet]
        sign(book, f"r{i}", others[0], t + H)                      # only 1 of the needed 2
        t = t + APPROVAL_WINDOW + H
        assert [r.request_id for r in book.tick(t)] == [f"r{i}"]
        assert req.state is A.EXPIRED
        with pytest.raises(IllegalTransition):
            sign(book, f"r{i}", others[1], t)
    assert book.signer_misses(quiet) == SIGNER_MISS_THRESHOLD and book.is_barred(quiet, t)
    assert not book.is_barred(quiet, t + SIGNER_MISS_BAR)
    req = book.open("rerun", "credit", ("G0",), D(2000), t)
    assert quiet not in req.panel


def test_signature_and_panel_checks():
    l, lim, book, *_ = world()
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    req = book.open("r1", "credit", ("G0",), D(2000), t)
    outsider = next(g for g in GEN if g not in req.panel and g != "G0")
    with pytest.raises(ValidationError):
        sign(book, "r1", outsider, t)
    with pytest.raises(SignatureError):
        book.sign("r1", req.panel[0], b"\x00" * 32, t)
    with pytest.raises(SignatureError):
        sign(book, "r1", req.panel[0], t + APPROVAL_WINDOW + H)   # window closed
    with pytest.raises(ValidationError):
        book.open("r1", "credit", ("G0",), D(2000), t)
    with pytest.raises(ValidationError):
        book.open("r2", "credit", ("G0",), D(10), t)               # nothing to approve


def test_new_members_cannot_sign_and_vouchers_sit_on_panel_first():
    l, lim, book, g, _ = world(new=("M", "N1", "N2"))
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    g.vouch("G7", "G0", t)
    req = book.open("r1", "credit", ("G0",), D(2000), t)
    assert req.panel[0] == "G7" and not {"M", "N1", "N2"} & set(req.panel)


def test_no_panel_rejects():
    l, lim, book, *_ = world()
    for gg in GEN[1:]:
        lim._members[gg].genesis = False                           # nobody else established
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    req = book.open("r1", "credit", ("G0",), D(2000), t)
    assert req.state is A.REJECTED


def test_decline_rejects_when_quorum_impossible():
    l, lim, book, *_ = world()
    t = T0 + 200 * DAY
    diversify(l, "G0", t - DAY)
    req = book.open("r1", "credit", ("G0",), D(2000), t)
    book.decline("r1", req.panel[0], t)
    assert req.state is A.PENDING
    book.decline("r1", req.panel[1], t)
    assert req.state is A.REJECTED
    with pytest.raises(ValidationError, match="cooling down"):
        book.open("r2", "credit", ("G0",), D(2000), t + DAY)
    assert book.open("r3", "credit", ("G0",), D(2000), t + REJECT_COOLDOWN).state is A.PENDING
    assert book.tick(t + DAY) == [] and book.signer_misses(req.panel[0]) == 0


def test_stake_unlock_above_tier_needs_approval():
    l, lim, book, g, am = world(new=())
    t0 = T0 + 200 * DAY
    e = g.vouch("G0", "G1", t0)                                    # ΔC = 10000 (genesis vouchee)
    diversify(l, "G1", t0)
    cr = book.open("c1", "credit", ("G1",), D(3000), t0)
    for s_ in cr.panel[:2]:
        sign(book, "c1", s_, t0)
    for c in ("G4", "G5", "G6"):
        l.record_trade("G1", c, D(1000), t0 + H)
    r = am.amortize("G0", "G1", "G4", t0 + 31 * DAY)
    assert r.status is S.UNLOCKED and r.unlocked == D(1000)         # cumulative 1000: tier 0
    r = am.amortize("G0", "G1", "G5", t0 + 31 * DAY)
    assert r.status is S.NEEDS_APPROVAL and e.released == D(1000)
    req = book.open("u1", "unlock", ("G0", "G1"), D(2000), t0 + 31 * DAY)
    assert not {"G0", "G1"} & set(req.panel)
    for s in req.panel[:2]:
        sign(book, "u1", s, t0 + 31 * DAY)
    r = am.amortize("G0", "G1", "G5", t0 + 31 * DAY)
    assert r.status is S.UNLOCKED and e.released == D(2000)


def test_weights_are_ints_and_documented():
    assert set(SIGNAL_WEIGHTS) == {"young_account", "low_diversity", "roundtrip_near", "roundtrip_hit",
                                   "router_miss", "router_barred", "limit_maxing", "recent_clawback"}
    assert all(isinstance(v, int) and v > 0 for v in SIGNAL_WEIGHTS.values())


def test_v2_default_policy_is_off():
    l, lim, book, *_ = world(policy=ApprovalPolicy())
    assert book.policy == ApprovalPolicy() and not book.policy.tiers and not book.policy.escalation
    t = T0 + 200 * DAY
    l.record_trade("G0", "G1", D(50000), t)                       # no sign-off needed when off
    l.record_trade("M", "G1", D(250), T0 + H)                     # suspect + maxing, still no gate
    with pytest.raises(Exception):
        l.record_trade("M", "G1", D(1), T0 + H)                   # starter cap still applies


def test_v2_tiers_on_escalation_off():
    l, lim, book, *_ = world(policy=ApprovalPolicy(tiers=True, escalation=False))
    a = book.assess("M", D(250), T0 + H)
    assert a.score == 3 and a.required_tier == 0                  # signals computed, no bump
    l.record_trade("M", "G1", D(250), T0 + H)
    with pytest.raises(ApprovalRequired):
        l.record_trade("G0", "G1", D(1001), T0 + 200 * DAY)       # size tier still enforced
