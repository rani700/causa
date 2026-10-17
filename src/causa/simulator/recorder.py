"""Records every simulated statement into a query log shaped like Exasol's audit tables.

On a real cluster this information comes from ``EXA_STATISTICS.EXA_DBA_AUDIT_SQL``. The simulator
compresses months into minutes, so it writes its own log with *simulated* timestamps instead.
"""

from __future__ import annotations

import itertools
import time as _time
from datetime import datetime, timedelta

import polars as pl

from causa.sqlutil import command_name
from causa.warehouse import Warehouse


class QueryRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self._stmt_ids = itertools.count(1)
        self._session_ids = itertools.count(7_340_000_000_000_001)

    def session(self, wh: Warehouse, client: str, user: str) -> RecordingSession:
        return RecordingSession(self, wh, client, user, next(self._session_ids))

    def append(self, **record: object) -> None:
        record["stmt_id"] = next(self._stmt_ids)
        self.records.append(record)

    def to_frame(self) -> pl.DataFrame:
        return pl.DataFrame(
            self.records,
            schema={
                "session_id": pl.Int64,
                "stmt_id": pl.Int64,
                "client": pl.String,
                "user_name": pl.String,
                "command_name": pl.String,
                "sql_text": pl.String,
                "start_time": pl.Datetime("us"),
                "stop_time": pl.Datetime("us"),
                "duration": pl.Float64,
                "success": pl.Boolean,
                "row_count": pl.Int64,
                "error_text": pl.String,
            },
        )


class RecordingSession:
    def __init__(self, recorder: QueryRecorder, wh: Warehouse, client: str, user: str, session_id: int) -> None:
        self._rec = recorder
        self._wh = wh
        self.client = client
        self.user = user
        self.session_id = session_id

    def execute(self, sql: str, at: datetime) -> int:
        t0 = _time.perf_counter()
        try:
            rows = self._wh.execute(sql)
        except Exception as exc:
            self.log(sql, at, duration=_time.perf_counter() - t0, row_count=None, error=str(exc)[:2000])
            raise
        self.log(sql, at, duration=_time.perf_counter() - t0, row_count=rows if rows >= 0 else None)
        return rows

    def query(self, sql: str, at: datetime) -> pl.DataFrame:
        t0 = _time.perf_counter()
        df = self._wh.query(sql)
        self.log(sql, at, duration=_time.perf_counter() - t0, row_count=df.height)
        return df

    def log(
        self,
        sql: str,
        at: datetime,
        *,
        duration: float,
        row_count: int | None,
        error: str | None = None,
    ) -> None:
        self._rec.append(
            session_id=self.session_id,
            client=self.client,
            user_name=self.user,
            command_name=command_name(sql),
            sql_text=sql,
            start_time=at,
            stop_time=at + timedelta(seconds=duration),
            duration=round(duration, 3),
            success=error is None,
            row_count=row_count,
            error_text=error,
        )
