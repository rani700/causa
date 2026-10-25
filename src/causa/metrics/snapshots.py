"""What each metric *showed*, night after night.

A warehouse only holds the latest truth: once late rows arrive or a failed job is re-run, the
numbers people saw (and acted on) are gone. The observer records each metric per day and per
dimension value as reported at observation time, which makes **restatements** visible: a value
that changed after it was first reported is strong evidence of a data problem, not a business one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import polars as pl

from causa import store
from causa.metrics.definitions import MetricDefinition, VolumeAggregation
from causa.sqlutil import dt
from causa.warehouse import Warehouse

TABLE = "CAUSA.METRIC_SNAPSHOTS"
TOTAL = "*"


def _volume_sql(metric: MetricDefinition) -> str:
    if not metric.volume_column:
        return "CAST(NULL AS DOUBLE)"
    match metric.volume_aggregation:
        case VolumeAggregation.COUNT_DISTINCT:
            return f"COUNT(DISTINCT {metric.volume_column})"
        case VolumeAggregation.SUM:
            return f"SUM({metric.volume_column})"
        case VolumeAggregation.COUNT:
            return "COUNT(*)"


def snapshot_sql(metric: MetricDefinition, first: date, last: date) -> str:
    dims = metric.dimensions
    cols = ", ".join(f"CAST({d} AS VARCHAR(200)) AS {d}" for d in dims)
    where = f"{metric.time_column} BETWEEN {dt(first)} AND {dt(last)}"
    if metric.filter:
        where += f" AND ({metric.filter})"
    sets = ", ".join(["(T)", *(f"(T, {d})" for d in dims)])
    return f"""WITH B AS (SELECT {metric.time_column} AS T, {cols}, {metric.measure_column} AS M
                      {", " + metric.volume_column + " AS " + metric.volume_column if metric.volume_column else ""}
               FROM {metric.table} WHERE {where})
SELECT T, {", ".join(dims)}, SUM(M) AS MEASURE, {_volume_sql(metric)} AS VOLUME
FROM B GROUP BY GROUPING SETS ({sets})"""


class MetricObserver:
    def __init__(self, wh: Warehouse, metrics: list[MetricDefinition], lookback_days: int = 14) -> None:
        self._wh = wh
        self._metrics = metrics
        self._lookback = lookback_days

    def reset(self) -> None:
        store.drop(self._wh, TABLE)

    def observe(self, at: datetime, through: date) -> int:
        """Records every metric for the ``lookback_days`` up to ``through`` as it looks right now."""
        store.ensure(self._wh, TABLE)
        rows = 0
        first = through - timedelta(days=self._lookback - 1)
        for metric in self._metrics:
            raw = self._wh.query(snapshot_sql(metric, first, through))
            dims = [d.lower() for d in metric.dimensions]
            dimension = pl.coalesce(
                *[pl.when(pl.col(d).is_not_null()).then(pl.lit(d.upper())) for d in dims], pl.lit(TOTAL)
            )
            value = pl.coalesce(*[pl.col(d) for d in dims], pl.lit(TOTAL))
            frame = raw.select(
                metric=pl.lit(metric.name),
                observed_at=pl.lit(at),
                metric_date=pl.col("t").cast(pl.Date),
                dimension=dimension,
                dim_value=value,
                measure=pl.col("measure").cast(pl.Float64),
                volume=pl.col("volume").cast(pl.Float64),
            )
            rows += self._wh.load(_with_zeros(frame, metric.name, at, first, through), TABLE)
        return rows


def _with_zeros(frame: pl.DataFrame, metric: str, at: datetime, first: date, last: date) -> pl.DataFrame:
    """A day (or a segment on a day) without rows is a reported zero, not a missing observation:
    a failed load or a late batch shows up exactly as that."""
    days = pl.DataFrame({"metric_date": pl.date_range(first, last, "1d", eager=True)})
    total = pl.DataFrame({"dimension": [TOTAL], "dim_value": [TOTAL]})
    keys = pl.concat([frame.select("dimension", "dim_value").unique(), total]).unique()
    filled = (
        keys.join(days, how="cross")
        .join(frame.drop("metric", "observed_at"), on=["dimension", "dim_value", "metric_date"], how="left")
        .with_columns(pl.col("measure").fill_null(0.0), pl.col("volume").fill_null(0.0))
    )
    return filled.with_columns(metric=pl.lit(metric), observed_at=pl.lit(at))


@dataclass(frozen=True)
class Restatement:
    metric: str
    metric_date: date
    dimension: str
    dim_value: str
    first_reported: float
    first_observed_at: datetime
    latest: float

    @property
    def change(self) -> float:
        return self.latest - self.first_reported

    @property
    def pct(self) -> float | None:
        return self.change / self.first_reported if self.first_reported else None


def restatements(
    wh: Warehouse,
    metric: MetricDefinition,
    since: date,
    until: date,
    dimension: str | Sequence[str] = TOTAL,
    min_change: float = 0.02,
) -> list[Restatement]:
    """Days whose value changed by at least ``min_change`` after it was first reported.

    Several dimensions are read in one query (each query is a round trip to the warehouse)."""
    dimensions = [dimension] if isinstance(dimension, str) else list(dimension)
    if not dimensions or not store.exists(wh, TABLE):
        return []
    snaps = wh.query(f"""
        SELECT DIMENSION, METRIC_DATE, DIM_VALUE, OBSERVED_AT, MEASURE FROM {TABLE}
        WHERE METRIC = '{metric.name}' AND DIMENSION IN ({", ".join(f"'{d}'" for d in dimensions)})
          AND METRIC_DATE BETWEEN {dt(since)} AND {dt(until)}""")
    if snaps.is_empty():
        return []
    keys = ["dimension", "metric_date", "dim_value"]
    first = snaps.sort("observed_at").group_by(keys).first()
    latest = snaps.sort("observed_at").group_by(keys).last()
    joined = first.join(latest, on=keys, suffix="_latest")
    order = {d: i for i, d in enumerate(dimensions)}
    out = []
    for r in sorted(
        joined.iter_rows(named=True), key=lambda r: (order[r["dimension"]], r["metric_date"], r["dim_value"])
    ):
        base = r["measure"] or 0.0
        change = (r["measure_latest"] or 0.0) - base
        if abs(change) >= min_change * max(abs(base), 1e-9):
            out.append(
                Restatement(
                    metric.name,
                    r["metric_date"],
                    r["dimension"],
                    r["dim_value"],
                    base,
                    r["observed_at"],
                    r["measure_latest"] or 0.0,
                )
            )
    return out
