import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from decimal import Decimal
from http.server import HTTPServer

import pytest

from clearing_rail.events import (
    EventLog,
    StakeReleased,
    TargetDebitClearedIntact,
    TargetDebitIdentified,
    TargetFlagEvaluated,
    TradeConfirmed,
)
from clearing_rail.telemetry import (
    compute,
    debtor_clearance_share,
    distress_discipline,
    extraction_autonomy,
    make_handler,
    true_vouch_integrity,
)
from clearing_rail.types import Initiator
from conftest import T0

D = Decimal


def test_zero_denominators_return_none():
    empty = EventLog()
    assert extraction_autonomy(empty) is None
    assert true_vouch_integrity(empty) is None
    assert distress_discipline(empty) is None
    assert debtor_clearance_share(empty) is None


def test_extraction_autonomy():
    log = EventLog([
        TradeConfirmed(T0, 1, "A", "B", D(30), Initiator.AGENT),
        TradeConfirmed(T0, 2, "B", "C", D(70), Initiator.HUMAN),
    ])
    assert extraction_autonomy(log) == D("0.3")


def test_true_vouch_integrity():
    log = EventLog([
        StakeReleased(T0, "A", "B", D(75), True, "C"),
        StakeReleased(T0, "A", "B", D(25), False),
    ])
    assert true_vouch_integrity(log) == D("0.75")


def test_distress_discipline():
    log = EventLog([
        TargetFlagEvaluated(T0, "c1", "A", D(10), 0, True),
        TargetFlagEvaluated(T0, "c1", "B", D(10), 2, False),
        TargetFlagEvaluated(T0, "c2", "C", D(10), 0, True),
        TargetFlagEvaluated(T0, "c3", "D", D(10), 1, False),
    ])
    assert distress_discipline(log) == D("0.5")


def test_debtor_clearance_share_dedups_lineage():
    log = EventLog([
        TargetDebitIdentified(T0, "L1", "c1", "A", "B", D(10)),
        TargetDebitIdentified(T0, "L1", "c1-r1", "A", "B", D(10)),   # rerun, same debit
        TargetDebitIdentified(T0, "L2", "c2", "X", "Y", D(10)),
        TargetDebitClearedIntact(T0, "L2", "c2", "X", D(10)),
    ])
    assert debtor_clearance_share(log) == D("0.5")


def test_window_filter():
    log = EventLog([
        TradeConfirmed(T0, 1, "A", "B", D(10), Initiator.AGENT),
        TradeConfirmed(T0 + timedelta(days=2), 2, "A", "B", D(10), Initiator.HUMAN),
    ])
    snap = compute(log, start=T0 + timedelta(days=1), end=T0 + timedelta(days=3))
    assert snap.extraction_autonomy == "0"
    assert compute(log, end=T0 + timedelta(days=1)).extraction_autonomy == "1"
    assert compute(log).true_vouch_integrity is None


@pytest.fixture
def server():
    log = EventLog([
        TradeConfirmed(T0, 1, "A", "B", D(1), Initiator.AGENT),
        TradeConfirmed(T0 + timedelta(days=5), 2, "A", "B", D(3), Initiator.HUMAN),
    ])
    srv = HTTPServer(("127.0.0.1", 0), make_handler(log, T0))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_http_endpoint(server):
    with urllib.request.urlopen(server + "/telemetry") as r:
        assert r.headers["Content-Type"] == "application/json"
        body = json.loads(r.read())
    assert body == {
        "extraction_autonomy": "0.25",
        "true_vouch_integrity": None,
        "distress_discipline": None,
        "debtor_clearance_share": None,
        "as_of": T0.isoformat(),
    }
    q = "start=" + urllib.request.quote((T0 + timedelta(days=1)).isoformat())
    with urllib.request.urlopen(server + "/telemetry?" + q) as r:
        assert json.loads(r.read())["extraction_autonomy"] == "0"


def test_http_404_and_400(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(server + "/nope")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(server + "/telemetry?start=garbage")
    assert e.value.code == 400
