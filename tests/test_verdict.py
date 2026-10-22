from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from causa.evaluation import true_data_gap
from causa.health import HealthScanner
from causa.lineage import build_lineage
from causa.metrics.attribution import Period
from causa.metrics.definitions import MetricDefinition, load_config
from causa.metrics.snapshots import MetricObserver
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import (
    CustomerChurn,
    FxCorruption,
    LateArrival,
    MarketLaunch,
    SchemaDriftStall,
)
from causa.sources.catalog import Catalog
from causa.sources.query_log import TableQueryLog
from causa.verdict import Investigator, Verdict
from causa.verdict.classifier import HEALED
from causa.verdict.narrative import render
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from tests.conftest import START, small_scenario


def day(offset: int) -> date:
    return START + timedelta(days=offset)


def week(first: int) -> Period:
    return Period(day(first), day(first + 6))


EVENTS = [
    MarketLaunch(event_id="B1", start=day(42), country="MX", new_customers=60),
    FxCorruption(event_id="D1", start=day(57), end=day(58), currency="JPY", factor=1.35),
    LateArrival(event_id="D2", start=day(64), end=day(64), feed="na_orders", delay_days=2),
    CustomerChurn(event_id="B2", start=day(70), customer_name="Acme GmbH"),
    SchemaDriftStall(event_id="D3", start=day(78), end=day(81), feed="emea_orders"),
]


@pytest.fixture(scope="module")
def metric() -> MetricDefinition:
    return load_config("causa.yaml").metric("net_revenue")


@pytest.fixture(scope="module")
def env(metric: MetricDefinition):
    wh = DuckDBWarehouse(":memory:")
    scenario = small_scenario(EVENTS, days=84)
    scenario.base_orders_per_day = 250
    observer = MetricObserver(wh, [metric])
    run_simulation(
        scenario, wh, on_loaded=lambda d: observer.observe(datetime.combine(d + timedelta(days=1), time(3, 30)), d)
    )
    log = TableQueryLog(wh).fetch()
    catalog = Catalog.load(wh)
    graph = build_lineage(log, catalog)
    report = HealthScanner(wh, catalog, graph).scan(day(28), day(84), log)
    yield wh, Investigator(wh, catalog, graph, log, report)
    wh.close()


def judge(env, metric: MetricDefinition, period: Period) -> tuple[Verdict, float]:
    wh, investigator = env
    verdict = investigator.verdict(metric, period)
    _, gap = true_data_gap(wh, metric.measure_column, period, period.same_weekdays_before())
    return verdict, gap


def test_schema_drift_is_quantified_as_data(env, metric: MetricDefinition) -> None:
    v, gap = judge(env, metric, week(77))
    (finding,) = v.data_findings
    assert finding.confidence > 0.9
    assert finding.link.subset.label == "SOURCE_FEED=emea_orders"
    assert finding.estimate.impact == pytest.approx(gap, rel=0.15)
    assert any("integrity signals" in e.text for e in finding.evidence)


def test_fx_error_uses_the_proportional_mechanism(env, metric: MetricDefinition) -> None:
    v, gap = judge(env, metric, week(56))
    (finding,) = v.data_findings
    assert finding.estimate.method.startswith("proportional value shift")
    assert finding.link.bridge == "STG.ORDERS"  # CURRENCY reaches the fact table through staging
    assert finding.estimate.impact == pytest.approx(gap, rel=0.2)


def test_late_batch_is_healed_and_baseline_fx_error_is_counted(env, metric: MetricDefinition) -> None:
    v, gap = judge(env, metric, week(63))  # the FX error (days 57-58) sits in this week's baseline
    healed = [f for f in v.data_findings if f.estimate.method == HEALED]
    assert len(healed) == 1 and healed[0].estimate.impact == 0.0
    assert any(r.metric_date == day(64) and r.first_reported < r.latest for r in v.restatements)
    fx = [f for f in v.data_findings if f.estimate.baseline_days]
    assert fx and fx[0].estimate.impact < 0  # an inflated baseline makes the present look worse
    assert v.data_impact == pytest.approx(gap, rel=0.2)


@pytest.mark.parametrize("first", [42, 70], ids=["market_launch", "customer_churn"])
def test_business_changes_are_not_called_data(env, metric: MetricDefinition, first: int) -> None:
    v, gap = judge(env, metric, week(first))
    assert gap == pytest.approx(0.0, abs=1.0)
    assert v.data_impact == pytest.approx(0.0, abs=1.0)  # incidents in view (if any) are healed
    assert v.business_change == pytest.approx(v.explanation.total.delta)


def test_churn_surfaces_in_the_business_story(env, metric: MetricDefinition) -> None:
    v, _ = judge(env, metric, week(70))
    assert any("Acme GmbH" in line and "-100.0%" in line for line in render(v))
