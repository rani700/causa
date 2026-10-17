"""Warehouse abstraction.

Causa authors every statement in the **Exasol SQL dialect**. Production runs against Exasol; the
DuckDB backend exists so the engines can be developed and tested on a laptop without a cluster.
Result frames always use lower-case column names regardless of backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self

import polars as pl


class Warehouse(ABC):
    name: str

    @abstractmethod
    def execute(self, sql: str) -> int:
        """Run a statement and return the affected row count (``-1`` if not applicable)."""

    @abstractmethod
    def query(self, sql: str) -> pl.DataFrame:
        """Run a query and return its result with lower-case column names."""

    @abstractmethod
    def load(self, df: pl.DataFrame, table: str) -> int:
        """Append ``df`` to ``SCHEMA.TABLE``, matching columns by name. Returns rows loaded."""

    @abstractmethod
    def columns(self) -> pl.DataFrame:
        """Catalog of user tables and views: table_schema, table_name, column_name, ordinal_position,
        data_type (all upper-case identifiers)."""

    @abstractmethod
    def close(self) -> None: ...

    def scalar(self, sql: str) -> object:
        df = self.query(sql)
        return df.item(0, 0) if df.height else None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def lower_columns(df: pl.DataFrame) -> pl.DataFrame:
    return df.rename({c: c.lower() for c in df.columns})
