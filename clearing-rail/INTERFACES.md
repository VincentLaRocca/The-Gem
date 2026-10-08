# Interfaces left unimplemented

This reference package implements the *state machines, constraints, and data
structures* of the Edge-Native Clearing Rail. The following pieces are
deliberately interfaces (Protocols / stubs) rather than executable logic,
because the original handoff either left them unspecified or put them outside
the clearing core.

## 1. Solver committee cycle search

* **Where:** `clearing_rail.solver.SolverCommittee` Protocol.
* **What core does:** validates a published `CycleCandidate`, drives the
  signature windows, and on a router timeout hands a `ResidualGraph` to
  `committee.request_rerun(...)`.
* **What core does NOT do:** find cycles. There is no matching engine, no
  graph search, no ILP / heuristic solver. Tests ship a trivial double that
  records the residual and optionally returns a pre-seeded candidate.

## 2. Cryptographic signature scheme / key registry

* **Where:** `clearing_rail.crypto.Verifier` Protocol + `KeyRegistry`.
* **What core does:** verifies hop signatures and solver block signatures
  through the Protocol; stores public keys by entity id; computes
  deterministic cycle / hop / block message encodings (SHA-256 of a tagged
  canonical encoding).
* **What core does NOT do:** key issuance, rotation, revocation, hardware
  security modules, or choosing a production scheme. An optional
  `Ed25519Verifier` is provided when `cryptography` is installed; tests use a
  pure-Python HMAC-style double so they run with stdlib alone.

## 3. On-chain smart-contract target

* **Where:** `clearing_rail.settlement` (the whole module).
* **What core does:** a reference in-process ledger with atomic commit,
  mismatch → revert + slash, and a solver bond registry that lives *outside*
  the ledger so slashes survive rollbacks.
* **What core does NOT do:** deploy or target any specific chain. No chain
  was named in the handoff. Settlement and slash here are the *spec of the
  contract*, not a Solidity / Move / Clarity artifact. A future adapter
  would map `SettlementEngine.commit` onto chain transactions.

## 4. Legal document rendering / jurisdiction rules data

* **Where:** `clearing_rail.legal.generate_payload`, `Jurisdiction`.
* **What core does:** the decision tree that picks an ordered list of
  `PayloadKind` enums from `(tx_type, jurisdiction.allows_cognovit,
  is_real_property, notice_clock_open)`, plus the rate-card accept test.
* **What core does NOT do:** render the actual Account Stated / Cognovit /
  Lien Assignment documents, maintain a jurisdiction table of which US
  states (or other forums) allow cognovit, or file anything with a court /
  recorder. Those are product / legal-ops concerns.

## 5. Notice clock source

* **Where:** `JobContext.notice_clock_open` (a boolean input).
* **What core does:** consumes the flag.
* **What core does NOT do:** decide when a statutory notice clock is open.
  That depends on jurisdiction-specific notice-and-opportunity statutes and
  an external calendar / docket feed.

## 6. Dropped-target age-penalty policy

* **Where:** `ClearingLoop.age_penalty` (default `timedelta(0)`).
* **What core does:** on `TARGET_TIMEOUT → DROPPED`, bumps a per-node retry
  counter and adds `age_penalty` to every debit lot's effective age.
* **What core does NOT do:** decide how large that penalty should be (or
  whether it should instead reset / freeze the clock). Real aging still
  advances with the injected clock past the 90-day threshold.

## 7. Edge-agent offer protocol

* **Where:** `Ledger.record_trade(..., initiated_by=Initiator.AGENT|HUMAN)`.
* **What core does:** records who initiated a confirmed offer (feeds
  `extraction_autonomy`).
* **What core does NOT do:** the edge-agent transport, confirmation UX, or
  gossip / discovery of counterparties. Agents are assumed to exist outside
  this package and to post confirmed offers into the ledger.

## 8. Telemetry transport beyond the local HTTP handler

* **Where:** `clearing_rail.telemetry.serve` / `TelemetryHandler`.
* **What core does:** a stdlib `http.server` handler for `GET /telemetry`
  returning the four kill-criteria ratios as JSON.
* **What core does NOT do:** authn/authz, TLS, push to a metrics backend, or
  pilot-control A/B wiring. The handler is a reference endpoint for the
  pilot control comparison, not a production service.

## 9. Slashed-bond destination

* **Where:** `SolverRegistry.slash_stake`.
* **What core does:** reduces the solver's bond and records `slashed_total`.
* **What core does NOT do:** route slashed funds anywhere (burn, insurance
  pool, compensation to affected nodes). Unspecified.

## 10. Urgency boosts

* **Where:** `Node.urgency_boosts_used` (an integer).
* **What core does:** reads it to decide target eligibility and to feed
  `distress_discipline`.
* **What core does NOT do:** grant, price, or apply a boost. The spec never
  defines what a boost does, so there is no `use_boost()` mechanic.

## 11. Rerun candidate provenance

* **Where:** `CycleCandidate.lineage_id` / `rerun_depth`.
* **What core does:** trusts the committee to set these when it answers a
  `ResidualGraph`, and uses them for telemetry.
* **What core does NOT do:** verify that a rerun candidate actually excludes
  the dropped node or descends from the named parent.
