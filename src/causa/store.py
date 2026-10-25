"""Causa's own state, kept in the CAUSA schema of the warehouse it observes."""

from __future__ import annotations

from causa.warehouse import Warehouse

SCHEMA = "CAUSA"
REPLAY_SCHEMA = "CAUSA_REPLAY"  # scratch tables of counterfactual replays
SCHEMAS = frozenset({SCHEMA, REPLAY_SCHEMA})  # never monitored, never part of the company's lineage

TABLES = {
    "CAUSA.METRIC_SNAPSHOTS": """CREATE TABLE CAUSA.METRIC_SNAPSHOTS (
        METRIC VARCHAR(100), OBSERVED_AT TIMESTAMP, METRIC_DATE DATE, DIMENSION VARCHAR(128),
        DIM_VALUE VARCHAR(200), MEASURE DOUBLE, VOLUME DOUBLE)""",
}


def exists(wh: Warehouse, table: str) -> bool:
    try:
        wh.query(f"SELECT 1 FROM {table} WHERE 1 = 0")
    except Exception:
        return False
    return True


def ensure(wh: Warehouse, table: str) -> None:
    if exists(wh, table):
        return
    wh.execute("CREATE SCHEMA IF NOT EXISTS CAUSA")
    wh.execute(TABLES[table])


def drop(wh: Warehouse, table: str) -> None:
    if exists(wh, table):
        wh.execute(f"DROP TABLE {table}")
