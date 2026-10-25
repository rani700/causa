from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

import pytest

from causa import slack
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import SchemaDriftStall
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from causa.watch import Watcher
from causa.workspace import Workspace
from tests.conftest import START, small_scenario

DAYS = 36


@pytest.fixture(scope="module")
def ws():
    wh = DuckDBWarehouse(":memory:")
    # The EMEA feed breaks two days before "today": a fresh, serious incident.
    drift = SchemaDriftStall(
        event_id="D", start=START + timedelta(days=DAYS - 3), end=START + timedelta(days=DAYS - 2), feed="emea_orders"
    )
    run_simulation(small_scenario([drift], days=DAYS), wh)
    yield Workspace(wh)
    wh.close()


def test_each_new_incident_is_announced_once_and_recorded(ws) -> None:
    watcher = Watcher(ws)
    first = watcher.check(datetime(2026, 4, 7, 9, 0))
    incidents = [n for n in first if n.kind == "incident"]
    assert incidents and all(n.channel == "recorded" for n in incidents)
    text = incidents[0].text
    assert "RAW.SHOP_ORDERS" in text and "consumers" in text and "Executive Revenue" in text

    assert watcher.check(datetime(2026, 4, 7, 9, 15)) == []  # nothing new: nothing repeated
    # A restarted server (a new watcher) reads the record in the warehouse: still no repeat.
    assert Watcher(ws).check(datetime(2026, 4, 7, 10, 0)) == []
    assert [n["key"] for n in watcher.recent()] == [n.key for n in incidents]


def test_the_brief_goes_out_once_a_day_after_its_time(ws, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(slack, "post", lambda url, payload: posted.append(payload) or 200)
    watcher = Watcher(ws, webhook_url="https://hooks.example/x", brief_metric="net_revenue", brief_at=dtime(7, 0))

    assert [n.kind for n in watcher.check(datetime(2026, 4, 7, 6, 30))] == []  # before 07:00
    sent = watcher.check(datetime(2026, 4, 7, 7, 5))
    assert [(n.kind, n.channel) for n in sent] == [("brief", "slack")]
    assert "Net revenue" in sent[0].text and posted and "blocks" in posted[0]
    assert watcher.check(datetime(2026, 4, 7, 12, 0)) == []  # once per day


def test_a_slack_outage_does_not_stop_the_record(ws, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(url: str, payload: dict[str, Any]) -> int:
        raise OSError("connection refused")

    monkeypatch.setattr(slack, "post", down)
    watcher = Watcher(ws, webhook_url="https://hooks.example/x", brief_metric="units_sold", brief_at=dtime(0, 0))
    (sent,) = watcher.check(datetime(2026, 4, 8, 8, 0))
    assert sent.kind == "brief" and sent.channel == "recorded"
