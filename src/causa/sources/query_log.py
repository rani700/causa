"""Query log sources. Every source yields the same frame shape:

session_id, stmt_id, client, user_name, command_name, sql_text, start_time, stop_time, duration,
success, row_count, error_text
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import polars as pl

from causa import store
from causa.sqlutil import ts
from causa.warehouse import ENGINE_CLIENTS, Warehouse


class QueryLogSource(Protocol):
    def fetch(self, since: datetime | None = None, until: datetime | None = None) -> pl.DataFrame: ...


def _window(column: str, since: datetime | None, until: datetime | None) -> str:
    parts = ["1 = 1"]
    if since:
        parts.append(f"{column} >= {ts(since)}")
    if until:
        parts.append(f"{column} < {ts(until)}")
    return " AND ".join(parts)


class ExasolAuditLog:
    """Reads Exasol's own audit trail (requires auditing enabled on the database and a user with
    access to the EXA_DBA_AUDIT_* system tables).

    Causa's own sessions are left out (drivers append their version to the client name), and so is
    any statement on Causa's own schemas: they are Causa's work, not the company's data flow."""

    def __init__(self, wh: Warehouse) -> None:
        self._wh = wh

    def fetch(self, since: datetime | None = None, until: datetime | None = None) -> pl.DataFrame:
        return self._wh.query(f"""
            SELECT q.SESSION_ID, q.STMT_ID, s.CLIENT, s.USER_NAME, q.COMMAND_NAME, q.SQL_TEXT,
                   q.START_TIME, q.STOP_TIME, q.DURATION, q.SUCCESS, q.ROW_COUNT, q.ERROR_TEXT
            FROM EXA_STATISTICS.EXA_DBA_AUDIT_SQL q
            JOIN EXA_STATISTICS.EXA_DBA_AUDIT_SESSIONS s ON s.SESSION_ID = q.SESSION_ID
            WHERE {_window("q.START_TIME", since, until)} AND {_not_causa("s.CLIENT", "q.SQL_TEXT")}
            ORDER BY q.START_TIME, q.STMT_ID""")


def _not_causa(client: str, sql_text: str) -> str:
    sessions = " AND ".join(f"{client} <> '{c}' AND {client} NOT LIKE '{c} %'" for c in ENGINE_CLIENTS)
    schemas = " AND ".join(f"UPPER({sql_text}) NOT LIKE '%{s}.%'" for s in sorted(store.SCHEMAS))
    return f"({client} IS NULL OR ({sessions})) AND {schemas}"


class TableQueryLog:
    """A query log stored in a regular table (the simulator's replayed log, or an imported export)."""

    def __init__(self, wh: Warehouse, table: str = "OPS.QUERY_LOG") -> None:
        self._wh = wh
        self._table = table

    def fetch(self, since: datetime | None = None, until: datetime | None = None) -> pl.DataFrame:
        return self._wh.query(f"""
            SELECT SESSION_ID, STMT_ID, CLIENT, USER_NAME, COMMAND_NAME, SQL_TEXT, START_TIME, STOP_TIME,
                   DURATION, SUCCESS, ROW_COUNT, ERROR_TEXT
            FROM {self._table}
            WHERE {_window("START_TIME", since, until)}
            ORDER BY START_TIME, STMT_ID""")
