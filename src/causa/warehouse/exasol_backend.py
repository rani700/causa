from __future__ import annotations

import ssl

import polars as pl
import pyexasol

from causa.config import Settings
from causa.warehouse.base import Warehouse, lower_columns


class ExasolWarehouse(Warehouse):
    name = "exasol"

    def __init__(self, settings: Settings, client_name: str = "causa") -> None:
        sslopt = {"cert_reqs": ssl.CERT_NONE} if settings.exasol_tls_insecure else None
        self._con = pyexasol.connect(
            dsn=settings.exasol_dsn,
            user=settings.exasol_user,
            password=settings.exasol_password.get_secret_value(),
            encryption=settings.exasol_encryption,
            websocket_sslopt=sslopt,
            client_name=client_name,
            compression=True,
        )

    def execute(self, sql: str) -> int:
        return self._con.execute(sql).rowcount()

    def query(self, sql: str) -> pl.DataFrame:
        stmt = self._con.execute(sql)
        return lower_columns(pl.DataFrame(stmt.fetchall(), schema=stmt.column_names(), orient="row"))

    def load(self, df: pl.DataFrame, table: str) -> int:
        if df.is_empty():
            return 0
        schema, name = table.split(".")
        self._con.import_from_polars(df, (schema, name), import_params={"columns": [c.upper() for c in df.columns]})
        return df.height

    def columns(self) -> pl.DataFrame:
        return self.query(
            """SELECT COLUMN_SCHEMA AS TABLE_SCHEMA, COLUMN_TABLE AS TABLE_NAME, COLUMN_NAME,
                      COLUMN_ORDINAL_POSITION AS ORDINAL_POSITION, COLUMN_TYPE AS DATA_TYPE
               FROM SYS.EXA_ALL_COLUMNS
               ORDER BY 1, 2, 4"""
        )

    def close(self) -> None:
        self._con.close()
