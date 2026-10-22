from __future__ import annotations

from datetime import date, timedelta

import pytest

from causa.health import HealthScanner, Severity, Signal
from causa.health.models import HealthCase
from causa.lineage import build_lineage
from causa.lineage.actors import ActorKind
from causa.metrics.attribution import Period
from causa.metrics.definitions import MetricDefinition, load_config
from causa.recall import recall
from causa.replay import Replayer
from causa.replay.engine import rewrite
from causa.replay.repairs import CoalesceRenamed, Dedupe, Interpolate, QuarantineKey
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import DuplicateLoad, FxCorruption, NullForeignKey, SchemaDriftStall
from causa.sources.catalog import Catalog
from causa.sources.query_log import TableQueryLog
from causa.verdict import Investigator
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from tests.conftest import START, small_scenario


def day(offset: int) -> date:
    return START + timedelta(days=offset)


EVENTS = [
    FxCorruption(event_id="D1", start=day(50), end=day(51), currency="JPY", factor=1.35),
    DuplicateLoad(event_id="D2", start=day(57), end=day(57), feed="apac_orders"),
    NullForeignKey(event_id="D3", start=day(64), end=day(66), channel="web", fraction=0.4),
    SchemaDriftStall(event_id="D4", start=day(77), end=day(80), feed="emea_orders"),
]


@pytest.fixture(scope="module")
def metric() -> MetricDefinition:
    return load_config("causa.yaml").metric("net_revenue")


@pytest.fixture(scope="module")
def env():
    wh = DuckDBWarehouse(":memory:")
    scenario = small_scenario(EVENTS, days=84)
    scenario.base_orders_per_day = 250
    run_simulation(scenario, wh)
    log = TableQueryLog(wh).fetch()
    catalog = Catalog.load(wh)
    graph = build_lineage(log, catalog)
    report = HealthScanner(wh, catalog, graph).scan(day(28), day(84), log)
    yield wh, catalog, graph, log, report
    wh.close()


def case_with(report, signal: Signal, start: date) -> HealthCase:
    alerting = [c for c in report.cases if c.severity in (Severity.CRITICAL, Severity.HIGH)]
    return next(c for c in alerting if signal in c.signals and abs((c.start - start).days) <= 2)


def true_misstatement(wh, days: list[date]) -> dict[date, float]:
    out = {}
    for d in days:
        reported = wh.scalar(f"SELECT SUM(NET_REVENUE_EUR) FROM MART.DAILY_REVENUE WHERE ORDER_DATE = DATE '{d}'")
        true = wh.scalar(f"SELECT SUM(NET_REVENUE_EUR) FROM SIM.TRUE_DAILY_REVENUE WHERE ORDER_DATE = DATE '{d}'")
        out[d] = float(reported or 0) - float(true or 0)
    return out


@pytest.mark.parametrize(
    ("signal", "offset", "repair", "tolerance"),
    [
        (Signal.SCHEMA_CHANGE, 78, CoalesceRenamed, 1e-6),
        (Signal.DUPLICATE_RATE, 58, Dedupe, 1e-6),
        (Signal.NULL_RATE, 65, QuarantineKey, 1e-6),
        (Signal.VALUE_SHIFT, 51, Interpolate, 0.03),  # interpolated rates are close, not exact
    ],
    ids=["renamed_column", "duplicates", "null_keys", "fx_rate"],
)
def test_replay_reproduces_the_true_misstatement(env, metric, signal, offset, repair, tolerance) -> None:
    wh, catalog, graph, log, report = env
    replayer = Replayer(wh, catalog, graph, log, report)
    plan = replayer.plan(case_with(report, signal, day(offset)), metric)
    assert not isinstance(plan, str), plan
    assert isinstance(plan.repair, repair)
    result = replayer.run(plan, metric)
    assert result.faithful, result.fidelity
    truth = true_misstatement(wh, sorted(result.daily_diff))
    for d, diff in result.daily_diff.items():
        assert -diff == pytest.approx(truth[d], rel=tolerance, abs=1.0), d
    assert not wh.query("SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema = 'CAUSA_REPLAY'").height


def test_rewrite_swaps_tables_and_keeps_aliases() -> None:
    sql = "INSERT INTO STG.X SELECT a.ID FROM RAW.A a JOIN RAW.B b ON b.ID = a.ID"
    out = rewrite(sql, {"STG.X": "CAUSA_REPLAY.R1_STG__X", "RAW.A": "CAUSA_REPLAY.F1_RAW__A"})
    assert "INSERT INTO CAUSA_REPLAY.R1_STG__X" in out
    assert "FROM CAUSA_REPLAY.F1_RAW__A AS a" in out and "JOIN RAW.B AS b" in out


def test_verdict_with_replay_and_recall(env, metric) -> None:
    wh, catalog, graph, log, report = env
    verdict = Investigator(wh, catalog, graph, log, report, replay=True).verdict(metric, Period(day(77), day(83)))
    (finding,) = verdict.data_findings
    assert finding.estimate.method.startswith("counterfactual replay")
    assert finding.replaced is not None  # the statistical estimate is kept as a cross-check

    (r,) = recall(verdict, graph, today=day(84))
    assert r.exposure_end is None  # lost rows were never re-loaded: still wrong
    assert r.total_misstatement == pytest.approx(sum(true_misstatement(wh, sorted(r.misstatement)).values()))
    kinds = {i.actor.kind for i in r.items}
    assert {ActorKind.DASHBOARD, ActorKind.PIPELINE, ActorKind.REVERSE_ETL} <= kinds
    carrier = next(i for i in r.items if i.actor.name == "sales_mart")
    assert "carried the incident" in carrier.notice
