"""Daily profiles of monitored tables, computed inside the warehouse.

One aggregate query per table returns, per load day and segment: row count, NULL counts of the
columns that matter, means of value columns and average lateness. A second query counts duplicate
keys. The result is a long frame of series ``(table, segment, signal, column, day) -> value, n``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import polars as pl

from causa.health.models import Signal, TableMonitor
from causa.sqlutil import ts
from causa.warehouse import Warehouse

SERIES_SCHEMA = {
    "table": pl.String,
    "segment_column": pl.String,
    "segment": pl.String,
    "signal": pl.String,
    "column": pl.String,
    "day": pl.Date,
    "value": pl.Float64,
    "n": pl.Float64,
}


def _window(m: TableMonitor, since: date, until: date) -> str:
    return f"{m.load_column} >= {ts(_dt(since))} AND {m.load_column} < {ts(_dt(until + timedelta(days=1)))}"


def _dt(d: date) -> datetime:
    return datetime.combine(d, time())


def profile_sql(m: TableMonitor, since: date, until: date) -> str:
    day = f"CAST({m.load_column} AS DATE)"
    segment = f"CAST({m.segment_column} AS VARCHAR(200))" if m.segment_column else "CAST('*' AS VARCHAR(200))"
    select = [f"{day} AS LOAD_DAY", f"{segment} AS SEGMENT", "COUNT(*) AS N_ROWS"]
    select += [f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS NULL__{c}" for c in m.null_columns]
    select += [f"AVG(CAST({c} AS DOUBLE)) AS AVG__{c}" for c in m.value_columns]
    if m.event_column:
        select.append(
            f"AVG(CAST(CAST({m.load_column} AS DATE) - CAST({m.event_column} AS DATE) AS DOUBLE)) AS LAG_DAYS"
        )
    return f"SELECT {', '.join(select)}\nFROM {m.table}\nWHERE {_window(m, since, until)}\nGROUP BY {day}, {segment}"


def duplicates_sql(m: TableMonitor, since: date, until: date) -> str:
    day = f"CAST({m.load_column} AS DATE)"
    segment = f"CAST({m.segment_column} AS VARCHAR(200))" if m.segment_column else "CAST('*' AS VARCHAR(200))"
    keys = ", ".join(m.key_columns)
    return f"""SELECT LOAD_DAY, SEGMENT, SUM(C - 1) AS DUP_ROWS
FROM (SELECT {day} AS LOAD_DAY, {segment} AS SEGMENT, {keys}, COUNT(*) AS C
      FROM {m.table} WHERE {_window(m, since, until)}
      GROUP BY {day}, {segment}, {keys} HAVING COUNT(*) > 1) d
GROUP BY LOAD_DAY, SEGMENT"""


def profile_table(wh: Warehouse, m: TableMonitor, since: date, until: date) -> pl.DataFrame:
    raw = wh.query(profile_sql(m, since, until))
    if raw.is_empty():
        return pl.DataFrame(schema=SERIES_SCHEMA)
    base = raw.select(
        day=pl.col("load_day").cast(pl.Date),
        segment=pl.col("segment"),
        n=pl.col("n_rows").cast(pl.Float64),
    )
    parts = [
        base.with_columns(signal=pl.lit(Signal.VOLUME.value), column=pl.lit(None, dtype=pl.String), value=pl.col("n"))
    ]
    for c in m.null_columns:
        parts.append(
            base.with_columns(
                signal=pl.lit(Signal.NULL_RATE.value),
                column=pl.lit(c),
                value=raw[f"null__{c.lower()}"].cast(pl.Float64) / pl.col("n"),
            )
        )
    for c in m.value_columns:
        parts.append(
            base.with_columns(signal=pl.lit(Signal.VALUE_SHIFT.value), column=pl.lit(c), value=raw[f"avg__{c.lower()}"])
        )
    if m.event_column:
        parts.append(
            base.with_columns(
                signal=pl.lit(Signal.LATENESS.value), column=pl.lit(m.event_column), value=raw["lag_days"]
            )
        )
    if m.key_columns:
        dups = wh.query(duplicates_sql(m, since, until)).select(
            day=pl.col("load_day").cast(pl.Date), segment=pl.col("segment"), dup=pl.col("dup_rows").cast(pl.Float64)
        )
        joined = base.join(dups, on=["day", "segment"], how="left").with_columns(pl.col("dup").fill_null(0.0))
        parts.append(
            joined.select(
                "day",
                "segment",
                "n",
                signal=pl.lit(Signal.DUPLICATE_RATE.value),
                column=pl.lit(",".join(m.key_columns)),
                value=pl.col("dup") / pl.col("n"),
            )
        )
    out = pl.concat([p.select("day", "segment", "n", "signal", "column", "value") for p in parts], how="diagonal")
    return out.with_columns(table=pl.lit(m.table), segment_column=pl.lit(m.segment_column)).select(
        [pl.col(k).cast(v) for k, v in SERIES_SCHEMA.items()]
    )


def conservation_series(
    series: pl.DataFrame, edges: list[tuple[str, str]], monitors: list[TableMonitor]
) -> pl.DataFrame:
    """Row conservation along pipeline edges: rows in the target per row in the source, per load day.

    Compared per segment when both tables share the segment column, otherwise on totals.
    """
    by_table = {m.table: m for m in monitors}
    volume = series.filter(pl.col("signal") == Signal.VOLUME.value)
    parts = []
    for source, target in edges:
        if source not in by_table or target not in by_table:
            continue
        same_segment = by_table[source].segment_column == by_table[target].segment_column
        keys = ["day", "segment"] if same_segment else ["day"]

        def agg(table: str, keys: list[str] = keys) -> pl.DataFrame:
            v = volume.filter(pl.col("table") == table)
            return v.group_by(keys).agg(pl.col("n").sum())

        joined = (
            agg(source).join(agg(target), on=keys, how="left", suffix="_t").with_columns(pl.col("n_t").fill_null(0))
        )
        if not same_segment:
            joined = joined.with_columns(segment=pl.lit("*"))
        parts.append(
            joined.select(
                table=pl.lit(target),
                segment_column=pl.lit(by_table[target].segment_column if same_segment else None, dtype=pl.String),
                segment=pl.col("segment"),
                signal=pl.lit(Signal.ROW_LOSS.value),
                column=pl.lit(source),  # the upstream table the ratio is measured against
                day=pl.col("day"),
                value=pl.col("n_t") / pl.col("n"),
                n=pl.col("n"),
            )
        )
    if not parts:
        return pl.DataFrame(schema=SERIES_SCHEMA)
    return pl.concat(parts).select([pl.col(k).cast(v) for k, v in SERIES_SCHEMA.items()])
