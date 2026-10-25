from datetime import date, datetime

import polars as pl

from causa.warehouse.exasol_backend import typed_frame

# Metadata and values exactly as pyexasol returned them from Exasol 2025.1 over the websocket.
META = {
    "D": {"type": "DATE", "size": 4},
    "T": {"type": "TIMESTAMP", "withLocalTimeZone": False, "size": 16},
    "I": {"type": "DECIMAL", "precision": 18, "scale": 0},
    "BIG": {"type": "DECIMAL", "precision": 36, "scale": 0},
    "N": {"type": "DECIMAL", "precision": 10, "scale": 2},
    "F": {"type": "DOUBLE"},
    "B": {"type": "BOOLEAN"},
    "S": {"type": "CHAR", "size": 1, "characterSet": "ASCII"},
}
ROW = ("2026-01-02", "2026-01-02 03:04:05.123000", 5, "5", "1.5", 2.5, True, "x")


def test_types_follow_metadata_not_values() -> None:
    df = typed_frame([ROW, (None,) * len(ROW)], META)
    assert df.schema == pl.Schema(
        {
            "D": pl.Date,
            "T": pl.Datetime("us"),
            "I": pl.Int64,
            "BIG": pl.Int64,
            "N": pl.Float64,
            "F": pl.Float64,
            "B": pl.Boolean,
            "S": pl.String,
        }
    )
    assert df.row(0) == (date(2026, 1, 2), datetime(2026, 1, 2, 3, 4, 5, 123000), 5, 5, 1.5, 2.5, True, "x")
    assert df.row(1) == (None,) * len(ROW)


def test_empty_result_keeps_its_schema() -> None:
    df = typed_frame([], {"D": {"type": "DATE"}, "C": {"type": "DECIMAL", "precision": 18, "scale": 0}})
    assert df.is_empty()
    assert df.schema == pl.Schema({"D": pl.Date, "C": pl.Int64})


def test_audit_log_leaves_out_causas_own_work() -> None:
    from causa.health.discovery import EXCLUDED_SCHEMAS
    from causa.sources.query_log import ExasolAuditLog
    from causa.warehouse.duckdb_backend import DuckDBWarehouse

    wh = DuckDBWarehouse(":memory:")
    wh.execute("CREATE SCHEMA EXA_STATISTICS")
    wh.execute("CREATE TABLE EXA_STATISTICS.EXA_DBA_AUDIT_SESSIONS (SESSION_ID INT, CLIENT VARCHAR, USER_NAME VARCHAR)")
    wh.execute(
        "CREATE TABLE EXA_STATISTICS.EXA_DBA_AUDIT_SQL (SESSION_ID INT, STMT_ID INT, COMMAND_NAME VARCHAR, "
        "SQL_TEXT VARCHAR, START_TIME TIMESTAMP, STOP_TIME TIMESTAMP, DURATION DOUBLE, SUCCESS BOOLEAN, "
        "ROW_COUNT INT, ERROR_TEXT VARCHAR)"
    )
    sessions = {
        1: "Tableau Server 2025.1; workbook=Executive Revenue",
        2: "causa 3.0.0",  # Causa's engines
        3: "causa-mcp 3.0.0",  # Causa behind Claude Desktop / Claude Code
        4: "causa-simulator 3.0.0",  # the simulated company's own workload: kept
        5: None,
        6: "proust",  # Exasol SaaS's own monitor, as EXA_PROUST_MONITOR
    }
    for sid, client in sessions.items():
        value = f"'{client}'" if client else "NULL"
        user = "EXA_PROUST_MONITOR" if sid == 6 else "SHWETA"
        wh.execute(f"INSERT INTO EXA_STATISTICS.EXA_DBA_AUDIT_SESSIONS VALUES ({sid}, {value}, '{user}')")
    statements = [
        (1, "SELECT * FROM RPT.V_EXEC_REVENUE_DAILY"),
        (2, "SELECT * FROM MART.FCT_ORDER_LINES"),
        (3, "SELECT * FROM MART.DAILY_REVENUE"),
        (4, "INSERT INTO MART.DAILY_REVENUE SELECT * FROM MART.FCT_ORDER_LINES"),
        (5, "SELECT 1"),
        (1, "SELECT * FROM causa_replay.R1_MART__FCT_ORDER_LINES"),  # anyone touching Causa's scratch tables
        (6, "SELECT * FROM DB_METADATA"),
    ]
    for i, (sid, sql) in enumerate(statements):
        wh.execute(
            f"INSERT INTO EXA_STATISTICS.EXA_DBA_AUDIT_SQL VALUES ({sid}, {i}, 'SELECT', '{sql}', "
            "TIMESTAMP '2026-10-01 00:00:00', TIMESTAMP '2026-10-01 00:00:01', 1, TRUE, 0, NULL)"
        )
    log = ExasolAuditLog(wh).fetch()
    assert sorted(log["session_id"].to_list()) == [1, 4, 5]
    assert {"CAUSA", "CAUSA_REPLAY"} <= EXCLUDED_SCHEMAS
