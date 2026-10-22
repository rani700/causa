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
