from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from causa.simulator.scenario import (
    CustomerChurn,
    DuplicateLoad,
    FxCorruption,
    JobFailure,
    LateArrival,
    NullForeignKey,
    SchemaDriftStall,
    default_scenario,
)
from causa.sqlutil import command_name
from tests.conftest import START, Simulate


def day(offset: int) -> date:
    return START + timedelta(days=offset)


def gap_by_date(wh, where: str = "1 = 1") -> pl.DataFrame:
    """Reported minus true net revenue per date (only dates with a difference)."""
    return wh.query(f"""
        WITH r AS (SELECT ORDER_DATE, SUM(NET_REVENUE_EUR) AS REP
                   FROM MART.DAILY_REVENUE WHERE {where} GROUP BY ORDER_DATE),
             t AS (SELECT ORDER_DATE, SUM(NET_REVENUE_EUR) AS TRU
                   FROM SIM.TRUE_DAILY_REVENUE WHERE {where} GROUP BY ORDER_DATE)
        SELECT t.ORDER_DATE, t.TRU, COALESCE(r.REP, 0) AS REP, COALESCE(r.REP, 0) - t.TRU AS GAP
        FROM t LEFT JOIN r ON r.ORDER_DATE = t.ORDER_DATE
        WHERE ABS(COALESCE(r.REP, 0) - t.TRU) > 0.01 ORDER BY 1""")


def test_without_incidents_the_warehouse_reports_the_truth(simulate: Simulate) -> None:
    wh, result = simulate([CustomerChurn(event_id="B", start=day(5), customer_name="Acme GmbH")])
    assert gap_by_date(wh).is_empty()
    assert result.reported_net_revenue == pytest.approx(result.true_net_revenue, rel=1e-9)


def test_schema_drift_silently_drops_only_the_drifted_feed(simulate: Simulate) -> None:
    wh, _ = simulate([SchemaDriftStall(event_id="D", start=day(5), end=day(7), feed="emea_orders")])
    gaps = gap_by_date(wh)
    assert gaps["order_date"].to_list() == [day(5), day(6), day(7)]
    assert gap_by_date(wh, "REGION <> 'EMEA'").is_empty()
    # Rows still landed (with the new column) and the connector reported success.
    assert wh.scalar("SELECT COUNT(*) FROM RAW.SHOP_ORDERS WHERE ORDER_TS IS NULL AND ORDER_TS_UTC IS NOT NULL") > 0
    assert wh.scalar("SELECT COUNT(*) FROM OPS.PIPELINE_RUNS WHERE RUN_STATE <> 'success'") == 0
    assert wh.scalar("SELECT COUNT(*) FROM OPS.QUERY_LOG WHERE COMMAND_NAME = 'ALTER TABLE'") == 1


def test_duplicate_load_inflates_one_day_in_one_region(simulate: Simulate) -> None:
    wh, _ = simulate([DuplicateLoad(event_id="D", start=day(4), end=day(4), feed="apac_orders")])
    gaps = gap_by_date(wh)
    assert gaps["order_date"].to_list() == [day(4)]
    assert gaps["gap"][0] > 0
    assert gap_by_date(wh, "REGION <> 'APAC'").is_empty()


def test_fx_corruption_hits_only_that_currency(simulate: Simulate) -> None:
    wh, _ = simulate([FxCorruption(event_id="D", start=day(3), end=day(3), currency="JPY", factor=1.35)])
    gaps = gap_by_date(wh)
    assert gaps["order_date"].to_list() == [day(3)]
    jp = gap_by_date(wh, "COUNTRY = 'JP'")
    assert jp["rep"][0] / jp["tru"][0] == pytest.approx(1.35, rel=1e-6)


def test_null_foreign_keys_lose_revenue_in_one_channel(simulate: Simulate) -> None:
    wh, _ = simulate([NullForeignKey(event_id="D", start=day(3), end=day(4), channel="web", fraction=0.5)])
    assert gap_by_date(wh)["gap"].max() < 0
    assert gap_by_date(wh, "CHANNEL <> 'web'").is_empty()


def test_late_arrival_and_job_failure_self_heal(simulate: Simulate) -> None:
    wh, result = simulate(
        [
            LateArrival(event_id="D1", start=day(3), end=day(3), feed="na_orders", delay_days=2),
            JobFailure(event_id="D2", start=day(6), end=day(6), recovered_on=day(8)),
        ]
    )
    assert gap_by_date(wh).is_empty()  # final state is correct ...
    assert result.failed_tasks == ["sales_mart__fct_order_lines__" + f"{day(6):%Y%m%d}__1"]
    # ... but the operational trail shows what went wrong and when.
    late = wh.query(f"""SELECT LOGICAL_DATE, ROWS_AFFECTED FROM OPS.PIPELINE_RUNS
                        WHERE TASK_ID = 'na_orders' AND LOGICAL_DATE IN ({",".join(f"DATE '{day(i)}'" for i in (3, 5))})
                        ORDER BY LOGICAL_DATE""")
    assert late["rows_affected"][0] == 0
    assert wh.scalar("SELECT COUNT(*) FROM OPS.QUERY_LOG WHERE CLIENT LIKE '%trigger=manual%'") > 0


def test_query_log_captures_every_layer(simulate: Simulate) -> None:
    wh, _ = simulate()
    commands = set(wh.query("SELECT DISTINCT COMMAND_NAME FROM OPS.QUERY_LOG")["command_name"])
    assert {"IMPORT", "INSERT", "SELECT", "CREATE TABLE", "CREATE VIEW", "TRUNCATE"} <= commands
    clients = wh.query("SELECT DISTINCT CLIENT FROM OPS.QUERY_LOG")["client"].to_list()
    assert any("workbook=Executive Revenue" in c for c in clients)
    assert any("dag=sales_mart" in c for c in clients)


def test_simulation_is_deterministic(simulate: Simulate) -> None:
    wa, a = simulate(days=5)
    wb, b = simulate(days=5)
    # Same rows, byte for byte; float totals may differ in the last digit because polars sums in parallel.
    fingerprint = "SELECT COUNT(*), SUM(ORDER_ID * LINE_NO), SUM(QUANTITY), MIN(UNIT_PRICE) FROM RAW.SHOP_ORDER_LINES"
    assert wa.query(fingerprint).rows() == wb.query(fingerprint).rows()
    assert a.true_net_revenue == pytest.approx(b.true_net_revenue, rel=1e-12)
    assert a.query_log_rows == b.query_log_rows


def test_default_scenario_mixes_business_and_data_events() -> None:
    scenario = default_scenario()
    natures = {e.nature for e in scenario.events}
    assert natures == {"business", "data"}
    assert all(scenario.start <= e.start <= scenario.end for e in scenario.events)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("CREATE OR REPLACE TABLE A.B AS SELECT 1", "CREATE TABLE AS"),
        ("CREATE TABLE A.B (X INT)", "CREATE TABLE"),
        ("  select 1", "SELECT"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT"),
        ("IMPORT INTO A.B FROM LOCAL CSV FILE 'f.csv'", "IMPORT"),
        ("ALTER TABLE A.B ADD COLUMN C INT", "ALTER TABLE"),
    ],
)
def test_command_name(sql: str, expected: str) -> None:
    assert command_name(sql) == expected


def test_random_scenarios_are_varied_and_valid() -> None:
    from causa.benchmark import random_scenario

    drawn = [random_scenario(seed) for seed in range(1, 6)]
    assert all(9 <= len(s.events) <= 12 for s in drawn)
    assert len({tuple(e.kind for e in s.events) for s in drawn}) == 5  # every company has its own story
    for s in drawn:
        churned = [e.customer_name for e in s.events if e.kind == "customer_churn"]
        assert len(churned) == len(set(churned))  # a customer churns once
        assert sum(e.kind == "market_launch" for e in s.events) <= 1
        assert all(s.start <= e.start <= s.start + timedelta(days=s.days) for e in s.events)
    assert random_scenario(3) == random_scenario(3)  # reproducible
