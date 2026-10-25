"""Warehouse abstraction.

Causa authors every statement in the **Exasol SQL dialect**. Production runs against Exasol; the
DuckDB backend exists so the engines can be developed and tested on a laptop without a cluster.
Result frames always use lower-case column names regardless of backend.
"""

from __future__ import annotations

import functools
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, ClassVar, Self

import polars as pl


@dataclass
class Meter:
    """What answering one question cost the warehouse: statements sent and time spent in them."""

    statements: int = 0
    seconds: float = 0.0
    backends: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "statements": self.statements,
            "warehouse_seconds": round(self.seconds, 2),
            "backends": sorted(self.backends),
        }


_meter: ContextVar[Meter | None] = ContextVar("causa_meter", default=None)


@contextmanager
def metering() -> Iterator[Meter]:
    """Counts every warehouse statement run in this context (thread / request)."""
    meter = Meter()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


def _metered(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(self: Warehouse, *args: Any, **kwargs: Any) -> Any:
        meter = _meter.get()
        if meter is None:
            return fn(self, *args, **kwargs)
        started = time.perf_counter()
        try:
            return fn(self, *args, **kwargs)
        finally:
            meter.statements += 1
            meter.seconds += time.perf_counter() - started
            meter.backends.add(self.name)

    wrapper._metered = True  # type: ignore[attr-defined]
    return wrapper


class Warehouse(ABC):
    name: str
    # Statements are counted by the backend that runs them; a wrapper that delegates sets False.
    metered: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.__dict__.get("metered", True):
            return
        for name in ("execute", "query", "load"):
            fn = cls.__dict__.get(name)
            if fn is not None and not getattr(fn, "_metered", False):
                setattr(cls, name, _metered(fn))

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
