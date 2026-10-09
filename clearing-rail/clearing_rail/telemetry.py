"""Telemetry Suite (Kill Criteria).

Four pure ratio functions over the event log, plus a tiny stdlib HTTP handler
serving ``GET /telemetry`` as JSON. Ratios return ``None`` when the
denominator is 0.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from .events import (
    Event,
    EventLog,
    StakeClawedBack,
    StakeReleased,
    TargetDebitClearedIntact,
    TargetDebitIdentified,
    TargetFlagEvaluated,
    TradeConfirmed,
)
from .types import ZERO, Initiator


def _ratio(num: Decimal, den: Decimal) -> Optional[Decimal]:
    if den == 0:
        return None
    return num / den


def extraction_autonomy(events: Iterable[Event]) -> Optional[Decimal]:
    """Agent-initiated confirmed offers / total capacity utilized.

    Capacity utilized = sum of debit legs settled in the window
    (i.e. every TradeConfirmed amount)."""
    agent = ZERO
    total = ZERO
    for e in events:
        if isinstance(e, TradeConfirmed):
            total += e.amount
            if e.initiated_by is Initiator.AGENT:
                agent += e.amount
    return _ratio(agent, total)


def true_vouch_integrity(events: Iterable[Event]) -> Optional[Decimal]:
    """Matured-volume-backed release / total stake released.  (FIX: new definition)

    numerator   = min(V, R_out)
      R_out = outside-volume releases that were NOT later clawed back
      V     = distinct matured transfers backing those releases, each counted ONCE
              (so one small trade backing many vouchers' releases counts once)
    denominator = every StakeReleased amount (outside or not, clawed back or not)

    A hand-built outside release with no ``backing`` (legacy events) is treated as
    backed by its own amount. Ratio is None when nothing was released."""
    events = list(events)
    clawed_ids = {e.release_id for e in events if isinstance(e, StakeClawedBack) and e.release_id is not None}
    clawed_anon = sum((e.amount for e in events if isinstance(e, StakeClawedBack) and e.release_id is None), ZERO)
    total = ZERO
    r_out = ZERO
    backing: Dict[object, Decimal] = {}
    for i, e in enumerate(events):
        if not isinstance(e, StakeReleased):
            continue
        total += e.amount
        if not e.via_outside_volume or (e.release_id is not None and e.release_id in clawed_ids):
            continue
        r_out += e.amount
        if e.backing:
            for tid, amt in e.backing:
                backing[("t", tid)] = amt
        else:
            backing[("legacy", i)] = e.amount
    r_out = max(ZERO, r_out - clawed_anon)
    v = sum(backing.values(), ZERO)
    return _ratio(min(v, r_out), total)


def distress_discipline(events: Iterable[Event]) -> Optional[Decimal]:
    """Target flags triggered on nodes with prior boosts / total target flags."""
    prior = 0
    total = 0
    for e in events:
        if isinstance(e, TargetFlagEvaluated):
            total += 1
            if e.prior_boosts > 0:
                prior += 1
    return _ratio(Decimal(prior), Decimal(total))


def debtor_clearance_share(events: Iterable[Event]) -> Optional[Decimal]:
    """Target debits cleared intact / total target debits identified.

    A target debit is keyed by (lineage_id, node_id), so a debit that is
    re-identified in a rerun cycle of the same lineage counts once.
    'Cleared intact' = settled in a single cycle without being dropped or
    rerun (TargetDebitClearedIntact, only emitted for rerun_depth == 0)."""
    identified = set()
    cleared = set()
    for e in events:
        if isinstance(e, TargetDebitIdentified):
            identified.add((e.lineage_id, e.node_id))
        elif isinstance(e, TargetDebitClearedIntact):
            cleared.add((e.lineage_id, e.node_id))
    return _ratio(Decimal(len(cleared & identified)), Decimal(len(identified)))


@dataclass(frozen=True)
class TelemetrySnapshot:
    extraction_autonomy: Optional[str]
    true_vouch_integrity: Optional[str]
    distress_discipline: Optional[str]
    debtor_clearance_share: Optional[str]
    as_of: Optional[str] = None

    @classmethod
    def from_events(cls, events: Iterable[Event], as_of: Optional[datetime] = None) -> "TelemetrySnapshot":
        def fmt(r: Optional[Decimal]) -> Optional[str]:
            return None if r is None else format(r, "f")

        return cls(
            extraction_autonomy=fmt(extraction_autonomy(events)),
            true_vouch_integrity=fmt(true_vouch_integrity(events)),
            distress_discipline=fmt(distress_discipline(events)),
            debtor_clearance_share=fmt(debtor_clearance_share(events)),
            as_of=as_of.isoformat() if as_of else None,
        )


def in_window(events: Iterable[Event], start: Optional[datetime] = None,
              end: Optional[datetime] = None) -> list:
    """Events with start <= ts < end (either bound optional)."""
    return [e for e in events
            if (start is None or e.ts >= start) and (end is None or e.ts < end)]


def compute(log: Iterable[Event], as_of: Optional[datetime] = None,
            start: Optional[datetime] = None, end: Optional[datetime] = None) -> TelemetrySnapshot:
    return TelemetrySnapshot.from_events(in_window(log, start, end), as_of)


class TelemetryHandler(BaseHTTPRequestHandler):
    """Minimal stdlib handler. Construct via :func:`make_handler` so the log
    is closed over without relying on global state."""

    log: EventLog  # set on the subclass by make_handler
    clock_now: Optional[datetime] = None

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path != "/telemetry":
            self.send_error(404, "not found")
            return
        q = parse_qs(url.query)
        try:
            start = datetime.fromisoformat(q["start"][0]) if "start" in q else None
            end = datetime.fromisoformat(q["end"][0]) if "end" in q else None
        except ValueError:
            self.send_error(400, "start/end must be ISO-8601")
            return
        snap = compute(self.log, self.clock_now, start, end)
        body = json.dumps(asdict(snap)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence default stderr spam in tests
        return


def make_handler(log: EventLog, as_of: Optional[datetime] = None):
    return type("BoundTelemetryHandler", (TelemetryHandler,), {"log": log, "clock_now": as_of})


def serve(log: EventLog, host: str = "127.0.0.1", port: int = 8765,
          as_of: Optional[datetime] = None) -> HTTPServer:
    """Start a blocking HTTPServer serving GET /telemetry. Caller owns the loop."""
    return HTTPServer((host, port), make_handler(log, as_of))
