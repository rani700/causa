"""Exasol first, a local copy while Exasol is unreachable.

A hosted Causa must keep answering when the warehouse is away: Exasol SaaS clusters stop themselves
after a period without use, and networks fail. ``FailoverWarehouse`` sends every statement to the
primary warehouse; when the connection is lost (never on an ordinary SQL error) it switches to the
fallback, a DuckDB copy of the same data, and a background thread keeps trying to reconnect. Each
switch bumps ``generation``, so whatever was computed on the other backend is rebuilt.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import polars as pl

from causa.warehouse.base import Warehouse

log = logging.getLogger(__name__)


def connection_lost(exc: BaseException) -> bool:
    """True for a lost or refused connection; False for errors in the SQL itself."""
    try:
        from pyexasol.exceptions import ExaCommunicationError, ExaConnectionError, ExaRequestError
    except ImportError:  # pragma: no cover - pyexasol is a dependency
        return isinstance(exc, OSError)
    if isinstance(exc, ExaRequestError):
        return False  # the database answered: bad SQL, missing object, ...
    if isinstance(exc, ExaCommunicationError | ExaConnectionError | OSError | ConnectionError):
        return True
    return type(exc).__module__.startswith("websocket")  # closed or broken websocket


class FailoverWarehouse(Warehouse):
    metered = False  # the backend it delegates to counts the statements

    def __init__(
        self,
        primary: Callable[[], Warehouse],
        fallback: Callable[[], Warehouse],
        retry_seconds: float = 120.0,
    ) -> None:
        self._connect_primary = primary
        self._fallback_factory = fallback
        self._retry = retry_seconds
        self._lock = threading.Lock()
        self._primary: Warehouse | None = None
        self._fallback: Warehouse | None = None
        self.generation = 0
        self.reason: str | None = None  # why the fallback is in use
        self.since: datetime | None = None
        self._stop = threading.Event()
        self._retrier: threading.Thread | None = None
        try:
            self._primary = primary()
        except Exception as exc:
            self._fail_over(exc)

    # -- state ------------------------------------------------------------------------------------

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._active().name

    @property
    def on_fallback(self) -> bool:
        return self._primary is None

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "fallback": self.on_fallback,
            "reason": self.reason,
            "since": self.since.isoformat(timespec="seconds") if self.since else None,
        }

    def _active(self) -> Warehouse:
        with self._lock:
            if self._primary is not None:
                return self._primary
            if self._fallback is None:
                self._fallback = self._fallback_factory()
            return self._fallback

    def _fail_over(self, exc: BaseException) -> None:
        with self._lock:
            broken, self._primary = self._primary, None
            self.reason = _describe(exc)
            self.since = datetime.now(UTC)
            self.generation += 1
        log.warning("primary warehouse unavailable, using the fallback copy: %s", self.reason)
        if broken is not None:
            with contextlib.suppress(Exception):  # it is already broken
                broken.close()
        if self._retrier is None or not self._retrier.is_alive():
            self._retrier = threading.Thread(target=self._reconnect, name="warehouse-retry", daemon=True)
            self._retrier.start()

    def _reconnect(self) -> None:
        while not self._stop.wait(self._retry):
            try:
                primary = self._connect_primary()
            except Exception as exc:
                with self._lock:
                    self.reason = _describe(exc)
                continue
            with self._lock:
                self._primary, self.reason, self.since = primary, None, None
                self.generation += 1
            log.warning("primary warehouse is back")
            return

    # -- the Warehouse interface ------------------------------------------------------------------

    def _call(self, method: str, *args: Any) -> Any:
        wh = self._active()
        try:
            return getattr(wh, method)(*args)
        except Exception as exc:
            if wh is self._fallback or not connection_lost(exc):
                raise
            self._fail_over(exc)
            return getattr(self._active(), method)(*args)

    def execute(self, sql: str) -> int:
        return int(self._call("execute", sql))

    def query(self, sql: str) -> pl.DataFrame:
        result: pl.DataFrame = self._call("query", sql)
        return result

    def query_limited(self, sql: str, seconds: float) -> pl.DataFrame:
        result: pl.DataFrame = self._call("query_limited", sql, seconds)
        return result

    def load(self, df: pl.DataFrame, table: str) -> int:
        return int(self._call("load", df, table))

    def columns(self) -> pl.DataFrame:
        result: pl.DataFrame = self._call("columns")
        return result

    def close(self) -> None:
        self._stop.set()
        for wh in (self._primary, self._fallback):
            if wh is not None:
                wh.close()


def _describe(exc: BaseException) -> str:
    """A one-line reason. pyexasol errors read connection options when printed, so prefer their
    ``message``; describing a failure must never fail itself."""
    try:
        text = str(getattr(exc, "message", None) or exc)
    except Exception:
        text = ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return f"{type(exc).__name__}: {first}"[:300]
