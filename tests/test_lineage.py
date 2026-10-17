from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from causa.lineage import LineageGraph, build_lineage
from causa.lineage.actors import ActorKind, parse_client
from causa.lineage.parser import ColumnRef, parse_statement
from causa.simulator import sql
from causa.simulator.scenario import SchemaDriftStall
from causa.sources.catalog import Catalog
from causa.sources.query_log import TableQueryLog
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from tests.conftest import START, small_scenario

LO = datetime(2026, 3, 3)
HI = LO + timedelta(days=1)


@pytest.fixture(scope="module")
def warehouse():
    from causa.simulator.runner import run_simulation

    wh = DuckDBWarehouse(":memory:")
    drift = SchemaDriftStall(
        event_id="D", start=START + timedelta(days=9), end=START + timedelta(days=10), feed="emea_orders"
    )
    run_simulation(small_scenario([drift], days=14), wh)
    yield wh
    wh.close()


@pytest.fixture(scope="module")
def catalog(warehouse) -> Catalog:
    return Catalog.load(warehouse)


@pytest.fixture(scope="module")
def graph(warehouse, catalog) -> LineageGraph:
    return build_lineage(TableQueryLog(warehouse).fetch(), catalog)


def refs(*names: str) -> set[ColumnRef]:
    return {ColumnRef(*n.rsplit(".", 1)) for n in names}


def test_insert_select_uses_catalog_for_positional_columns(catalog: Catalog) -> None:
    stmt = parse_statement(sql.stg_orders(LO, HI), catalog)
    assert stmt.target == "STG.ORDERS"
    assert stmt.sources == {"RAW.SHOP_ORDERS", "RAW.FX_RATES"}
    assert stmt.derivations["FX_RATE_TO_EUR"] == refs("RAW.FX_RATES.RATE_TO_EUR")
    assert stmt.derivations["ORDER_DATE"] == refs("RAW.SHOP_ORDERS.ORDER_TS")
    # Columns that decide which rows survive, not just which values flow.
    assert refs("RAW.SHOP_ORDERS.ORDER_TS", "RAW.SHOP_ORDERS.ORDER_STATUS", "RAW.FX_RATES.CURRENCY") <= stmt.gating


def test_join_keys_are_gating(catalog: Catalog) -> None:
    stmt = parse_statement(sql.fct_order_lines(LO, HI), catalog)
    assert ColumnRef("STG.ORDER_LINES", "PRODUCT_ID") in stmt.gating
    assert stmt.derivations["NET_REVENUE_EUR"] == refs(
        "STG.ORDER_LINES.QUANTITY",
        "STG.ORDER_LINES.UNIT_PRICE",
        "STG.ORDER_LINES.DISCOUNT_PCT",
        "STG.ORDERS.FX_RATE_TO_EUR",
    )


def test_cte_names_are_not_tables(catalog: Catalog) -> None:
    stmt = parse_statement(
        "WITH x AS (SELECT CUSTOMER_ID, SUM(NET_REVENUE_EUR) AS R FROM MART.FCT_ORDER_LINES GROUP BY CUSTOMER_ID) "
        "SELECT * FROM x WHERE R > 10",
        catalog,
    )
    assert stmt.target is None
    assert stmt.sources == {"MART.FCT_ORDER_LINES"}


def test_import_and_ddl(catalog: Catalog) -> None:
    imp = parse_statement(sql.import_stmt("RAW.SHOP_ORDERS", ["order_id"], "emea.csv"), catalog)
    assert (imp.command, imp.target, imp.external_source) == ("IMPORT", "RAW.SHOP_ORDERS", "emea.csv")
    alter = parse_statement(sql.SCHEMA_DRIFT_DDL, catalog)
    assert alter.target == "RAW.SHOP_ORDERS"
    assert "ORDER_TS_UTC" in (alter.ddl or "")


def test_unparseable_sql_is_reported_not_raised(catalog: Catalog) -> None:
    assert parse_statement("SELEC broken FROM", catalog).error


@pytest.mark.parametrize(
    ("client", "kind", "name"),
    [
        ("Tableau Server 2025.1; workbook=Executive Revenue", ActorKind.DASHBOARD, "Executive Revenue"),
        ("Airflow 2.10; dag=sales_mart; trigger=manual", ActorKind.PIPELINE, "sales_mart"),
        ("Microsoft Excel 16; workbook=Close.xlsx", ActorKind.SPREADSHEET, "Close.xlsx"),
        ("Hightouch; sync=scores", ActorKind.REVERSE_ETL, "scores"),
        ("DBeaver 25.1", ActorKind.AD_HOC, "MEI via DBeaver"),
    ],
)
def test_parse_client(client: str, kind: ActorKind, name: str) -> None:
    actor = parse_client(client, "mei")
    assert (actor.kind, actor.name) == (kind, name)


def test_graph_reconstructs_the_pipeline(graph: LineageGraph) -> None:
    assert not graph.parse_errors
    assert graph.upstream_tables("RPT.V_EXEC_REVENUE_DAILY")[:2] == ["MART.DAILY_REVENUE", "MART.FCT_ORDER_LINES"]
    assert {"RAW.SHOP_ORDERS", "RAW.SHOP_ORDER_LINES", "RAW.FX_RATES"} <= set(
        graph.upstream_tables("RPT.V_EXEC_REVENUE_DAILY")
    )
    assert graph.column_sources("RPT.V_EXEC_REVENUE_DAILY", "NET_REVENUE_EUR") == sorted(
        refs(
            "RAW.FX_RATES.RATE_TO_EUR",
            "RAW.SHOP_ORDER_LINES.QUANTITY",
            "RAW.SHOP_ORDER_LINES.UNIT_PRICE",
            "RAW.SHOP_ORDER_LINES.DISCOUNT_PCT",
        )
    )
    assert len(graph.loaders("RAW.SHOP_ORDERS")) == 4


def test_gating_columns_include_upstream_filters(graph: LineageGraph) -> None:
    gates = graph.gating_columns("MART.DAILY_REVENUE")
    assert ColumnRef("RAW.SHOP_ORDERS", "ORDER_TS") in gates["STG.ORDERS"]
    assert ColumnRef("STG.ORDER_LINES", "PRODUCT_ID") in gates["MART.FCT_ORDER_LINES"]


def test_schema_change_is_attributed(graph: LineageGraph) -> None:
    (change,) = graph.schema_changes
    assert change.table == "RAW.SHOP_ORDERS"
    assert change.actor == "Fivetran/connector=emea_orders"
    assert change.at.date() == START + timedelta(days=10)


def test_impact_reaches_reports_ml_and_reverse_etl(graph: LineageGraph) -> None:
    impact = graph.impact("RAW.SHOP_ORDERS")
    names = {a.name for a in impact.actors}
    assert {
        "Executive Revenue",
        "Regional Performance",
        "Weekly Board Pack",
        "churn_forecast",
        "churn_scores_to_salesforce",
    } <= names
    # Customer master data reaches the ML model through the fact table.
    assert "ML.CHURN_SCORES" in graph.impact("RAW.CRM_CUSTOMERS").tables


def test_impact_window_filters_readers(graph: LineageGraph) -> None:
    sunday = START + timedelta(days=6)  # simulation starts on a Monday
    window = graph.impact("RAW.SHOP_ORDERS", since=sunday, until=sunday)
    assert "Weekly Board Pack" not in {a.name for a in window.actors}
    assert all(a.last_read is None or a.last_read.date() == sunday for a in window.actors)
