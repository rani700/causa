from __future__ import annotations

import polars as pl
import pytest
from pyexasol.exceptions import ExaCommunicationError, ExaConnectionFailedError, ExaQueryError

from causa.warehouse.base import Warehouse
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from causa.warehouse.failover import FailoverWarehouse, connection_lost


class FlakyExasol(Warehouse):
    """Answers until told the connection is gone."""

    name = "exasol"

    def __init__(self) -> None:
        self.down = False
        self.closed = False

    def _check(self) -> None:
        if self.down:
            raise ExaCommunicationError(None, "websocket closed")

    def execute(self, sql: str) -> int:
        self._check()
        return 1

    def query(self, sql: str) -> pl.DataFrame:
        self._check()
        if "BROKEN" in sql:
            raise ExaQueryError(None, sql, "42000", "syntax error")
        return pl.DataFrame({"v": [1]})

    def load(self, df: pl.DataFrame, table: str) -> int:
        self._check()
        return df.height

    def columns(self) -> pl.DataFrame:
        self._check()
        return pl.DataFrame()

    def close(self) -> None:
        self.closed = True


def copy() -> DuckDBWarehouse:
    wh = DuckDBWarehouse(":memory:")
    wh.execute("CREATE SCHEMA S")
    wh.execute("CREATE TABLE S.T AS SELECT 2 AS V")
    return wh


def test_lost_connection_switches_to_the_copy_and_comes_back() -> None:
    exasol = FlakyExasol()
    attempts: list[int] = []

    def connect() -> Warehouse:
        attempts.append(1)
        if len(attempts) > 1 and exasol.down:
            raise ExaConnectionFailedError(None, "Could not connect to Exasol: timed out")
        return exasol

    wh = FailoverWarehouse(connect, copy, retry_seconds=0.05)
    assert wh.query("SELECT V FROM S.T")["v"].to_list() == [1] and not wh.on_fallback

    # an error in the SQL itself is the caller's problem, not a reason to switch
    with pytest.raises(ExaQueryError):
        wh.query("SELECT BROKEN")
    assert not wh.on_fallback and wh.generation == 0

    exasol.down = True  # the cluster stopped itself
    assert wh.query("SELECT V FROM S.T")["v"].to_list() == [2]  # answered from the copy
    assert wh.on_fallback and wh.name == "duckdb" and wh.generation == 1
    assert wh.status()["fallback"] and "ExaCommunicationError" in wh.status()["reason"]

    exasol.down = False  # someone pressed Start
    for _ in range(100):
        if not wh.on_fallback:
            break
        import time

        time.sleep(0.02)
    assert not wh.on_fallback and wh.name == "exasol" and wh.generation == 2
    wh.close()


def test_unreachable_at_start_serves_the_copy() -> None:
    def connect() -> Warehouse:
        raise ExaConnectionFailedError(None, "Could not connect to Exasol: timed out")

    wh = FailoverWarehouse(connect, copy, retry_seconds=60)
    assert wh.on_fallback and wh.scalar("SELECT V FROM S.T") == 2
    wh.close()


def test_what_counts_as_a_lost_connection() -> None:
    assert connection_lost(ExaCommunicationError(None, "x"))
    assert connection_lost(ConnectionResetError())
    assert not connection_lost(ExaQueryError(None, "SELECT", "42000", "x"))
    assert not connection_lost(ValueError("x"))


def test_switching_backends_rebuilds_the_workspace() -> None:
    from causa.agent import Toolbox
    from causa.simulator.runner import run_simulation
    from causa.workspace import Workspace
    from tests.conftest import small_scenario

    data = DuckDBWarehouse(":memory:")
    run_simulation(small_scenario(days=8), data)

    class Remote(FlakyExasol):
        """The real data, until the connection drops."""

        def query(self, sql: str) -> pl.DataFrame:
            self._check()
            return data.query(sql)

        def columns(self) -> pl.DataFrame:
            self._check()
            return data.columns()

    remote = Remote()
    wh = FailoverWarehouse(lambda: remote, lambda: data, retry_seconds=60)
    ws = Workspace(wh)
    tb = Toolbox(ws)
    graph = ws.graph
    assert tb.keep_fresh() is False and ws.graph is graph

    remote.down = True
    tb.run_sql("SELECT COUNT(*) AS N FROM MART.FCT_ORDER_LINES")  # reaches the warehouse: fails over
    assert wh.on_fallback and ws.switched
    assert tb.keep_fresh() is True  # immediately, not after the refresh interval
    assert ws.graph is not graph and not ws.switched
