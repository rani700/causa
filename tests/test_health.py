from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from causa.health import HealthReport, HealthScanner, Severity, Signal
from causa.health.detectors import detect
from causa.lineage import build_lineage
from causa.simulator.scenario import (
    CustomerChurn,
    DuplicateLoad,
    Event,
    FxCorruption,
    JobFailure,
    LateArrival,
    NullForeignKey,
    SchemaDriftStall,
)
from causa.sources.catalog import Catalog
from causa.sources.query_log import TableQueryLog
from tests.conftest import START, Simulate

DAYS = 30
INCIDENT_DAY = 22


def day(offset: int) -> date:
    return START + timedelta(days=offset)


def scan(simulate: Simulate, events: list[Event]) -> HealthReport:
    wh, _ = simulate(events, days=DAYS)
    log = TableQueryLog(wh).fetch()
    catalog = Catalog.load(wh)
    graph = build_lineage(log, catalog)
    return HealthScanner(wh, catalog, graph).scan(day(14), day(DAYS), log)


def alerting(report: HealthReport):
    return [c for c in report.cases if c.severity in (Severity.CRITICAL, Severity.HIGH)]


# -- detectors on synthetic series ----------------------------------------------------------------


def series(values: list[float], signal: Signal = Signal.VOLUME, n: list[float] | None = None) -> pl.DataFrame:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(len(values))]  # starts on a Monday
    return pl.DataFrame(
        {
            "table": "T.X",
            "segment_column": "FEED",
            "segment": "a",
            "signal": signal.value,
            "column": None if signal is Signal.VOLUME else "C",
            "day": days,
            "value": values,
            "n": n or values,
        },
        schema_overrides={"column": pl.String},
    )


def test_volume_drop_is_detected_but_weekly_rhythm_is_not() -> None:
    week = [1000, 1050, 1010, 990, 950, 400, 300]
    values = week * 6
    df = series([*values, 1000, 1050, 20, 990])  # Wednesday collapses
    anomalies = detect(df, date(2026, 2, 16), date(2026, 2, 19))
    assert [a.day for a in anomalies] == [date(2026, 2, 18)]
    assert anomalies[0].score < 0


def test_small_counts_are_not_flagged_for_poisson_noise() -> None:
    rng = np.random.default_rng(1)
    df = series(rng.poisson(12, size=60).astype(float).tolist())
    assert detect(df, date(2026, 2, 1), date(2026, 3, 5)) == []


def test_missing_days_count_as_zero_volume() -> None:
    df = series([500.0] * 20).filter(pl.col("day") != date(2026, 1, 22))
    anomalies = detect(df, date(2026, 1, 20), date(2026, 1, 24))
    assert [(a.day, a.observed) for a in anomalies] == [(date(2026, 1, 22), 0.0)]


def test_null_rate_needs_practical_and_statistical_significance() -> None:
    n = [10_000.0] * 20
    tiny = series([0.001] * 19 + [0.004], Signal.NULL_RATE, n)  # significant but negligible
    big = series([0.001] * 19 + [0.08], Signal.NULL_RATE, n)
    assert detect(tiny, date(2026, 1, 20), date(2026, 1, 24)) == []
    assert len(detect(big, date(2026, 1, 20), date(2026, 1, 24))) == 1


# -- end to end on the simulator ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "signal", "table"),
    [
        (
            SchemaDriftStall(event_id="D", start=day(INCIDENT_DAY), end=day(24), feed="emea_orders"),
            Signal.SCHEMA_CHANGE,
            "RAW.SHOP_ORDERS",
        ),
        (
            DuplicateLoad(event_id="D", start=day(INCIDENT_DAY), end=day(INCIDENT_DAY), feed="na_orders"),
            Signal.DUPLICATE_RATE,
            "RAW.SHOP_ORDER_LINES",
        ),
        (
            FxCorruption(event_id="D", start=day(INCIDENT_DAY), end=day(23), currency="USD", factor=1.3),
            Signal.VALUE_SHIFT,
            "RAW.FX_RATES",
        ),
        (
            NullForeignKey(event_id="D", start=day(INCIDENT_DAY), end=day(24), channel="web", fraction=0.5),
            Signal.NULL_RATE,
            "RAW.SHOP_ORDER_LINES",
        ),
        (
            JobFailure(event_id="D", start=day(INCIDENT_DAY), end=day(INCIDENT_DAY), recovered_on=day(24)),
            Signal.FAILED_WRITE,
            "MART.FCT_ORDER_LINES",
        ),
        (
            LateArrival(event_id="D", start=day(INCIDENT_DAY), end=day(INCIDENT_DAY), feed="na_orders", delay_days=2),
            Signal.LATENESS,
            "RAW.SHOP_ORDERS",
        ),
    ],
    ids=lambda v: getattr(v, "kind", None) or str(v),
)
def test_each_data_incident_raises_one_alerting_case(simulate: Simulate, event: Event, signal: Signal, table: str):
    report = scan(simulate, [event])
    cases = alerting(report)
    assert len(cases) == 1, [c.primary.summary for c in cases]
    assert (cases[0].primary.signal, cases[0].primary.table) == (signal, table)
    assert day(INCIDENT_DAY) <= cases[0].primary.start <= day(INCIDENT_DAY + 3)


def test_business_change_does_not_alert(simulate: Simulate) -> None:
    report = scan(simulate, [CustomerChurn(event_id="B", start=day(INCIDENT_DAY), customer_name="Acme GmbH")])
    assert alerting(report) == []


def test_downstream_echoes_fold_into_the_root(simulate: Simulate) -> None:
    report = scan(
        simulate, [DuplicateLoad(event_id="D", start=day(INCIDENT_DAY), end=day(INCIDENT_DAY), feed="na_orders")]
    )
    (dup,) = report.by_signal(Signal.DUPLICATE_RATE)
    assert dup.table == "RAW.SHOP_ORDER_LINES"
    assert {p.table for p in dup.propagated} == {"STG.ORDER_LINES", "MART.FCT_ORDER_LINES"}


def test_schema_drift_is_one_case_with_its_symptoms(simulate: Simulate) -> None:
    report = scan(simulate, [SchemaDriftStall(event_id="D", start=day(INCIDENT_DAY), end=day(24), feed="emea_orders")])
    (case,) = alerting(report)
    assert {Signal.SCHEMA_CHANGE, Signal.NULL_RATE, Signal.ROW_LOSS} <= case.signals
    null = next(i for i in case.incidents if i.signal is Signal.NULL_RATE)
    assert (null.column, null.segment, null.observed) == ("ORDER_TS", "emea_orders", 1.0)


def test_discovery_infers_monitors(simulate: Simulate) -> None:
    report = scan(simulate, [])
    monitors = {m.table: m for m in report.monitors}
    lines = monitors["RAW.SHOP_ORDER_LINES"]
    assert (lines.load_column, lines.segment_column, lines.key_columns) == (
        "LOADED_AT",
        "SOURCE_FEED",
        ["ORDER_ID", "LINE_NO"],
    )
    assert "PRODUCT_ID" in lines.null_columns  # gates the product join two layers downstream
    assert monitors["RAW.FX_RATES"].key_columns == ["CURRENCY"]  # one rate per currency per load
