from __future__ import annotations

import ssl

import polars as pl
import pyexasol

from causa.config import Settings
from causa.warehouse.base import Warehouse, lower_columns

SMALL_LOAD_ROWS = 10_000


class ExasolWarehouse(Warehouse):
    name = "exasol"

    def __init__(self, settings: Settings, client_name: str = "causa") -> None:
        sslopt = {"cert_reqs": ssl.CERT_NONE if settings.exasol_tls_insecure else ssl.CERT_REQUIRED}
        self._con = pyexasol.connect(
            dsn=settings.exasol_dsn,
            user=settings.exasol_user,
            password=settings.exasol_password.get_secret_value(),
            encryption=settings.exasol_encryption,
            websocket_sslopt=sslopt,
            client_name=client_name,
            compression=True,
            connection_timeout=settings.exasol_connect_timeout,
        )

    def execute(self, sql: str) -> int:
        return self._con.execute(sql).rowcount()

    def query(self, sql: str) -> pl.DataFrame:
        stmt = self._con.execute(sql)
        return lower_columns(typed_frame(stmt.fetchall(), stmt.columns()))

    def load(self, df: pl.DataFrame, table: str) -> int:
        if df.is_empty():
            return 0
        schema, name = table.split(".")
        # IMPORT parses CSV text with the session's formats; polars writes ISO 8601 ('T' separator).
        df = df.with_columns(
            pl.col(pl.Datetime).dt.strftime("%Y-%m-%d %H:%M:%S%.6f"),
            pl.col(pl.Date).dt.strftime("%Y-%m-%d"),
        )
        columns = [c.upper() for c in df.columns]
        if df.height <= SMALL_LOAD_ROWS:
            # IMPORT opens a separate HTTP data channel per call (~1 s of setup on a remote cluster);
            # a prepared INSERT ships small batches over the open websocket in one round trip.
            self._con.ext.insert_multi((schema, name), df.rows(), columns=columns)
        else:
            self._con.import_from_polars(df, (schema, name), import_params={"columns": columns})
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


def _target(meta: dict) -> pl.DataType:
    kind = meta["type"]
    if kind == "DATE":
        return pl.Date()
    if kind == "TIMESTAMP":
        return pl.Datetime("us")
    if kind == "DECIMAL":
        return pl.Int64() if meta.get("scale", 0) == 0 else pl.Float64()
    if kind == "DOUBLE":
        return pl.Float64()
    if kind == "BOOLEAN":
        return pl.Boolean()
    return pl.String()


def typed_frame(rows: list[tuple], meta: dict[str, dict]) -> pl.DataFrame:
    """Build a frame from websocket rows, typed by the result set's column metadata: the protocol
    returns DATE, TIMESTAMP and wide or fractional DECIMAL values as strings."""
    names = list(meta)
    raw = pl.DataFrame(rows, schema=dict.fromkeys(names, pl.String), orient="row", strict=False)
    exprs = []
    for name in names:
        target, col = _target(meta[name]), pl.col(name)
        if isinstance(target, pl.Date):
            exprs.append(col.str.to_date("%Y-%m-%d"))
        elif isinstance(target, pl.Datetime):
            exprs.append(col.str.to_datetime("%Y-%m-%d %H:%M:%S%.f", time_unit="us"))
        elif isinstance(target, pl.Boolean):
            exprs.append(
                col.replace_strict(
                    {"true": True, "false": False, "True": True, "False": False}, default=None, return_dtype=pl.Boolean
                )
            )
        else:
            exprs.append(col.cast(target))
    return raw.select(exprs)
