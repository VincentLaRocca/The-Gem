# Edge-Native Clearing Rail

Reference implementation of Vincent's **Edge-Native Clearing Rail** spec: a
brokerless mutual-credit network where localized edge agents propose trades, a
plaintext committee of solvers finds cycles, and a deterministic legal utility
handles defaults.

**There is no centralized matching engine.** Cycle search is deliberately an
interface (`SolverCommittee`); this package only validates candidates and
drives settlement.

## Package layout

```
clearing_rail/
  types.py          Node, VouchEdge, CycleCandidate, Hop, Clock, errors
  events.py         Append-only event log (telemetry is pure over this)
  ledger.py         Double-entry mutual-credit ledger, 30-day bilateral windows
  vouch.py          Directional vouch graph, lock / unlock of ΔC
  amortization.py   Net-leg wash filter (C1 / C2 / C3)
  crypto.py         Verifier protocol, Ed25519 (optional), cycle / hop hashes
  settlement.py     PublishedCandidateBlock registry, commit, slash
  solver.py         CompressionTarget rules + execution state machine
  legal.py          Payload decision tree + rate card
  telemetry.py      Four kill-criteria ratios + GET /telemetry handler
  limits.py         Starter cap + earned credit growth for NEW members (v2)
  approvals.py      Tiered k-of-n approval + risk escalation (OFF by default)
  admission.py      Admission bond book (EXPERIMENT, OFF by default)
tests/              Full constraint coverage (stdlib + pytest)
```

## Design invariants

* **Deterministic.** Every function that needs time takes an injected
  `Clock` or an explicit `as_of` / `ts`. Nothing in this package reads the
  wall clock.
* **Exact money.** All amounts are `decimal.Decimal`. Floats are refused at
  the boundary.
* **Balances sum to zero.** The ledger is double-entry; every trade and every
  cycle clear moves equal and opposite amounts.
* **Slash survives rollback.** Solver bonds live in `SolverRegistry`,
  deliberately outside the ledger, so a mismatch reverts ledger state *and*
  keeps the slash.

## v2 hardening (local branch content; see `/workspace/mc-satoshi/*.md` for evidence)

All of the following were tested on Monte Carlo batteries: 2000 trials per attack scenario,
plus a 50-seed honest-traffic harness. Reports: `mc-satoshi/fixes.md`, `starter-cap.md`,
`starter-cap-v2.md`.

* **S1: wash rings vs amortization** (`amortization.py`).
  * C2 and C3 use the vouchee's aggregate flows.
  * Only legs at least 30 days old (`MATURITY_PERIOD`) back an unlock.
  * Releases stay provisional for 60 days (`CLAWBACK_HORIZON`). A later wash re-locks them
    (`StakeClawedBack`, `AmortizationEngine.recheck`).
  * `true_vouch_integrity` counts each backing transfer once and excludes
    clawed-back releases.
* **S4: signature-window withholding** (`ledger.py`, `solver.py`).
  * A signed hop is reserved, so later trades cannot net it away.
  * Router misses are counted per node across lineages. At 2 misses
    (`ROUTER_MISS_THRESHOLD`) the node is aged and barred for 30 days (`ROUTER_MISS_BAR`).
* **S6: legal floor** (`legal.py`).
  * `ClaimBook` gives each claim a pro-rata share of the fixed cost
    across the debtor and creditor's total exposure.
  * Offers below 10% of face (`MIN_RECOVERY_RATIO`) are never accepted.
* **Hardening.**
  * `MIN_SOLVER_BOND` (100).
  * `Node` rejects bad `urgency_boosts_used`.
* **Router-timeout determinism.** When several routers are late, the rerun drops the
  late router whose debit leg comes **earliest in the signed hop order**. Before, the
  choice depended on frozenset/hash order (`late_routers[0]`), so results changed with
  `PYTHONHASHSEED`. The full suite runs identically under seeds 0 and 1.
* **Invariant fix: `Ledger.net_pair`.**
  * Once a candidate's reservations are released (settle, timeout, `force_revert`),
    the unreserved parts of any opposite-direction obligations they kept apart are netted.
  * Net balances do not change.
  * This removes the "both directions outstanding" state that S4 reservations could leave behind.
* **Starter cap v2** (`limits.py`, `CreditLimits`; opt-in: only nodes registered as
  NEW are capped).
  * A NEW member starts at 250. It grows per 30-day period, once the period is 30 days old,
    at a rate of 0.5 × credited repayment. Growth is capped at 50% per period and at 25% of
    the limit per counterparty.
  * Repayment to GENESIS or unregistered members counts in full.
  * Repayment to other members 60 days or older and not wash-flagged counts only out
    of a budget of 0.33 × that member's own repayment to GENESIS or unregistered members.
    This keeps honest work per credit of growth at 1.5 or more, even for sybil rings.
  * Members who grew to 1000 are peers, not anchors.
  * Repaid lots younger than 1 day earn nothing.
  * Growth freezes only if debt older than 90 days exceeds 0.5 × the limit.
  * `StarterPolicy.v1()` reproduces the earlier rule.
* **Approvals OFF by default** (`approvals.py`). `ApprovalBook(..., policy=ApprovalPolicy())` never
  gates. `ApprovalPolicy(tiers=True, escalation=True)` turns on 2-of-3 approval above 1000
  and 3-of-5 above 5000, plus risk-score tier bumps. **STUB-DEPENDENT:** signatures use the
  `Verifier` interface. This is not Bitcoin multisig.
* **Admission bond EXPERIMENT, OFF by default** (`admission.py`).
  * Nothing posts a bond unless a caller constructs `AdmissionBonds`.
  * Collecting and paying out the deposit is a stub.
  * Measured result: it removes one-and-done profit but not patient-attacker profit, and it
    locks honest capital. Kept off.

## Interpretations (spec gaps — overrule freely)

These are the decisions the parent handoff locked in. Each is also stated in
code comments where it applies.

1. **Immediate vouch tree** = the roots plus every node with a direct vouch
   edge to or from them. Depth is a parameter (`VouchPolicy.tree_depth`,
   default `1`).
2. **Amortization order**: wash check (C3) runs first on the whole 30-day
   window. `outbound == 0` with `inbound > 0` → disqualified; both zero → 0.
   Then C2: if `net_transfer <= 0` → 0. Unlock =
   `min(net_transfer * unlock_ratio, remaining ΔC)` with `unlock_ratio`
   default `1.0`. Consumed volume is tracked per `(vouch edge, counterparty C)`
   so the same window volume cannot amortize twice. Released-stake events
   record `via_outside_volume=True` for this path (feeds
   `true_vouch_integrity`).
3. **Available credit** = `credit_ceiling - locked_vouch_stake`. A debit leg
   may not push a node's balance below `-(available credit)`. This is an
   **assumption** not stated in the original spec. For a NEW member registered
   with `CreditLimits`, the floor is `-min(available credit, earned limit)`.
4. **CompressionTarget / distress_discipline contradiction.** The original
   text requires `urgency_boosts_used == 0` to *be* a CompressionTarget,
   which would make `distress_discipline` always 0 by construction.
   Resolution: every stale-node evaluation is logged as a
   `TargetFlagEvaluated` event with a `prior_boosts` field; nodes with prior
   boosts are flagged-but-rejected (no 12-hour target window).
   `distress_discipline = flagged-with-prior-boosts / all flag events`.
5. **25% floor** must hold for *every* CompressionTarget in the cycle. When
   the candidate carries per-hop amounts we use the amount on the target's
   own debit leg; with the uniform-amount rule enforced here, that equals
   `notional_clearance`.
6. **Execution SM states**: `PROPOSED → VALIDATED → AWAITING_SIGNATURES →
   (SIGNED → SETTLED) | (ROUTER_TIMEOUT → RERUN_REQUESTED) |
   (TARGET_TIMEOUT → DROPPED) | REVERTED`. If a target and a router both
   miss, the **target rule wins**. With several late routers, the one whose
   debit leg is earliest in hop order is dropped (deterministic). `drop_node_and_rerun` excludes the node
   and hands a `ResidualGraph` to `SolverCommittee` — core never searches
   for a replacement. A trivial test-double committee lives in `tests/`.
7. **Settlement**: submitter must be the registered solver that published the
   block. A non-publisher mismatch is rejected **without** slashing (avoids
   griefing an honest solver) and flagged as a deviation. On mismatch *from*
   the publishing solver: ledger state is fully reverted (atomic), but the
   slash persists outside the rollback. Slash amount is a parameter (default
   full bond). Signature array is verified per hop via the `Verifier`
   protocol; vector equality = same ordered nodes, amounts, and cycle hash.
8. **Legal**: B2B where cognovit isn't allowed → `AccountStated`. Cognovit
   never for Consumer. Lien append applies to *any* `tx_type` when both
   `is_real_property` and `notice_clock_open` are true. Return type is an
   ordered list of payload enums.
9. **Rate card**: `t_days_past_180 = max(0, days_outstanding - 180)`. If
   `floor_price < 0`: a positive offer is accepted (`recovered = offer`,
   remainder written off); with no positive offer, the whole balance is
   written off. A zero offer is never booked as recovered. Result object:
   `(ACCEPT | REJECT | WRITE_OFF, recovered, written_off)`.
10. **Telemetry**: ratios return `null` when the denominator is 0.
    *Total capacity utilized* = sum of debit legs settled in the window
    (every `TradeConfirmed.amount`). *Target debits cleared intact* =
    target debit legs fully settled in a single cycle without being
    dropped / rerun (`TargetDebitClearedIntact`).

### Additional interpretations made while building (also overrulable)

11. **Obligation model.** "Clearing" needs bilateral debts to clear, so the
    ledger keeps bilateral obligation *lots* (`debtor owes creditor`, with an
    origination time) as well as each node's net balance. A trade opens a lot
    (after netting any reverse obligation between the same pair). A cycle hop
    `(debtor, creditor, amount)` reduces that obligation, oldest lot first.
    A uniform cycle leaves every net balance unchanged and compresses gross
    debt.
12. **"Balance > 90 days old"** = a node's *stale balance* is the sum of its
    debit lots older than 90 days (strictly greater: exactly 90d is not stale).
13. **Volume direction.** `outbound(B->C)` = value B took from C on credit
    (B is the debtor/payer on that trade). Cycle clearing is netting, not
    economic activity, so it never counts as amortization volume.
14. **Wash filter scope.** C3 is evaluated on the *whole* 30-day window
    (including volume already used for amortization); C2 net-leg uses only
    the not-yet-consumed slice. Inbound that produces a non-positive net is
    left unconsumed so it keeps offsetting later outbound.
15. **Amortization is per voucher.** If B has two vouchers, the same B↔C
    volume can amortize each voucher's edge once (consumption is tracked
    per `(edge, C)` as instructed).
16. **ΔC** = `VouchPolicy.slice_fraction × voucher ceiling` (default 10%).
    A vouch cannot be created if locking ΔC would put the voucher below
    its credit floor.
17. **Boosted stale nodes** get the 2-hour *router* window and no 25% floor.
18. **Target debit identification** is logged only for candidates that pass
    validation, and is keyed by `(lineage_id, node)` so a rerun of the same
    lineage does not double-count. `debtor_clearance_share` is count-based.
    A target that settles only via a rerun counts in the denominator, not the
    numerator.
19. **Signature windows.** A signature at exactly the deadline is accepted;
    a node "misses" only when `now > deadline`. Timeouts are evaluated when
    `tick()` is called; whichever tick first sees a miss decides the outcome
    (so a router miss at 2h01m triggers a rerun before a target's 12h window
    ends). Signatures are checked at submit time and again at commit.
20. **Settlement rejections that do NOT slash:** unregistered submitter, no
    published block, non-publisher submitter, invalid/missing hop
    signatures, replay of an already-settled candidate, and a *matching*
    vector the ledger can no longer apply (obligations shrank after
    publication → `REVERTED_LEDGER`). A slash that drives the bond to 0
    deregisters the solver.
21. **Rate card on accept above floor**: when an offer is accepted for less
    than face, the remainder is reported as `written_off` (the debt is
    discharged). With `floor_price == 0`, a zero offer is `REJECT` (not a
    write-off, since the floor is not negative).
22. **Telemetry window** is `start <= ts < end` (`GET /telemetry?start=&end=`
    with ISO-8601 values).

## Spec contradictions found beyond the ones above

* **`urgency_boosts_used == 0` vs `distress_discipline`** (item 4) — resolved
  as described.
* **Per-hop clearing amounts vs clearing.** The spec says a CycleCandidate
  carries "clearing amounts" (plural), but a cycle with non-uniform amounts
  moves net balances: someone gains and someone loses, so it is a transfer,
  not a clear. We **reject** non-uniform cycles at structure validation, so
  "notional_clearance (min over hops)" and "the target's own debit leg"
  always agree.
* **"Compress" vs net balances.** With only net balances, a closed cycle
  cannot reduce anyone's debt (it is a no-op). The 90-day target / 25% floor
  rules only make sense with bilateral obligations, which is why the ledger
  keeps lots (item 11). Note the consequence: clearing a target's stale lot
  compresses its *gross* debt and resets the stale clock on that lot, but its
  *net* balance is unchanged.
* **`drop_cycle_and_age_balance`** never says how much to age. We keep the
  balance's age running, bump a retry counter, and expose a configurable
  `age_penalty` (default 0).
* **Router timeout fires before a target's window closes.** The 2h router
  window always expires before the 12h target window, so "target rule wins"
  can only apply when both misses are first observed at the same tick
  (≥ 12h). In practice a late router reruns the cycle at 2h and the target
  re-enters through the rerun, so its debit is no longer "cleared intact".
* **`extraction_autonomy`** divides offers (a count-like noun) by capacity
  (an amount). We use amounts for both: agent-initiated confirmed trade
  value ÷ all confirmed trade value.
* **Settlement order.** The spec compares signatures to the published block
  *and* says to revert the transaction, implying it was applied. We verify
  hop signatures first (bad → reject, no slash), then apply inside an atomic
  transaction, compare vectors, and roll back + slash on mismatch.

## Interfaces left unimplemented

See [`INTERFACES.md`](INTERFACES.md). Short list: solver-committee cycle
search, cryptographic key issuance/rotation, on-chain smart-contract target
(this is a reference ledger, not deployed contracts), legal document
rendering / jurisdiction rules data, notice-clock source, the age-penalty
policy for dropped targets, where slashed bonds go, what an urgency boost
does, the edge-agent offer protocol, and production telemetry transport.
Since v2, the list also includes member admission (who is GENESIS), approval
signers and panel selection, and admission-bond custody.

## Run the tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest cryptography
PYTHONHASHSEED=0 .venv/bin/pytest -q    # 186 tests; also run with PYTHONHASHSEED=1
```

Stdlib only is enough for the core; `cryptography` is optional and used by
the Ed25519 verifier. Tests ship a pure-Python verifier double either way.
