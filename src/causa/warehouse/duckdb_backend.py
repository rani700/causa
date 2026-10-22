from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import duckdb
import polars as pl
import sqlglot

from causa.warehouse.base import Warehouse, lower_columns


@lru_cache(maxsize=4096)
def exasol_to_duckdb(sql: str) -> str:
    return sqlglot.transpile(sql, read="exasol", write="duckdb")[0]


class DuckDBWarehouse(Warehouse):
    name = "duckdb"

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._con = duckdb.connect(":memory:")
        if str(path) != ":memory:":
            # Attached under a fixed name: DuckDB would otherwise name the catalog after the file,
            # and a file called causa.duckdb would collide with Causa's own CAUSA schema.
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._con.execute(f"ATTACH '{Path(path).as_posix()}' AS warehouse")
            self._con.execute("USE warehouse")

    def execute(self, sql: str) -> int:
        rows = self._con.execute(exasol_to_duckdb(sql)).fetchall()
        if len(rows) == 1 and len(rows[0]) == 1 and isinstance(rows[0][0], int):
            return rows[0][0]
        return -1

    def query(self, sql: str) -> pl.DataFrame:
        return lower_columns(self._con.execute(exasol_to_duckdb(sql)).pl())

    def load(self, df: pl.DataFrame, table: str) -> int:
        if df.is_empty():
            return 0
        cols = ", ".join(c.upper() for c in df.columns)
        self._con.register("__causa_load", df.to_arrow())
        try:
            self._con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM __causa_load")
        finally:
            self._con.unregister("__causa_load")
        return df.height

    def columns(self) -> pl.DataFrame:
        df = self._con.execute(
            """SELECT UPPER(table_schema) AS table_schema, UPPER(table_name) AS table_name,
                      UPPER(column_name) AS column_name, ordinal_position, UPPER(data_type) AS data_type
               FROM information_schema.columns
               WHERE table_catalog = current_database()
                 AND table_schema NOT IN ('information_schema', 'pg_catalog')
               ORDER BY 1, 2, 4"""
        ).pl()
        return lower_columns(df)

    def close(self) -> None:
        self._con.close()
