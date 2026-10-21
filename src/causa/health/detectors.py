"""Anomaly detection over daily health series.

Every detector compares a day with its own trailing history (never the future), using robust
statistics (median baseline, MAD scale) so that past incidents do not mask new ones. An anomaly must
be both statistically surprising (``z_threshold``) and practically large (a per-signal minimum
effect), which keeps big-but-normal noise and tiny-but-significant drifts out of the report.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl

from causa.health.models import Anomaly, HealthIncident, Signal

_MAD_TO_SD = 1.4826
MAX_GAP_DAYS = 2  # one or two quiet days inside an episode do not split it


@dataclass(frozen=True)
class Sensitivity:
    lookback_days: int = 28
    volume_lookback_days: int = 56  # eight same-weekday samples, so two bad weeks cannot skew the median
    min_history: int = 7
    z_threshold: float = 5.0
    volume_min_change: float = 0.25
    rate_min_increase: float = 0.02
    value_min_change: float = 0.05
    lateness_min_days: float = 0.5
    row_loss_min_change: float = 0.05
    # Pipeline edges whose typical target/source row ratio lies outside this band are different
    # grains (e.g. FX rates -> orders) and are not checked for conservation.
    conservation_ratio_band: tuple[float, float] = (0.2, 5.0)


def _binomial_sd(p: float, n: float) -> float:
    """Standard error of a proportion, with p kept 1/(n+2) away from 0 and 1 so that a baseline of
    exactly 0% or 100% still carries the uncertainty of a small sample."""
    if n <= 0:
        return float("inf")
    floor = 1.0 / (n + 2)
    p = min(max(p, floor), 1 - floor)
    return float(np.sqrt(p * (1 - p) / n))


def _mad(x: np.ndarray) -> float:
    return float(_MAD_TO_SD * np.median(np.abs(x - np.median(x)))) if x.size else 0.0


@dataclass
class _Series:
    table: str
    segment_column: str | None
    segment: str | None
    signal: Signal
    column: str | None
    days: np.ndarray  # datetime64[D]
    values: np.ndarray
    n: np.ndarray

    def history(self, i: int, lookback: int) -> np.ndarray:
        lo = self.days[i] - np.timedelta64(lookback, "D")
        mask = (self.days >= lo) & (self.days < self.days[i]) & np.isfinite(self.values)
        return np.flatnonzero(mask)


def _densify_volume(group: pl.DataFrame, until: date) -> pl.DataFrame:
    """A feed that delivers nothing produces no profile row; make those days explicit zeros."""
    first = group["day"].min()
    full = pl.DataFrame({"day": pl.date_range(first, until, "1d", eager=True)})  # type: ignore[arg-type]
    return full.join(group.select("day", "value", "n"), on="day", how="left").with_columns(
        pl.col("value").fill_null(0.0), pl.col("n").fill_null(0.0)
    )


def detect(series: pl.DataFrame, since: date, until: date, sensitivity: Sensitivity | None = None) -> list[Anomaly]:
    s = sensitivity or Sensitivity()
    keys = ["table", "segment_column", "segment", "signal", "column"]
    anomalies: list[Anomaly] = []
    for key, group in series.sort("day").group_by(keys, maintain_order=True):
        table, seg_col, segment, signal, column = key
        signal = Signal(signal)
        if signal is Signal.VOLUME:
            group = _densify_volume(group, until)
        ser = _Series(
            table,  # type: ignore[arg-type]
            seg_col,  # type: ignore[arg-type]
            segment,  # type: ignore[arg-type]
            signal,
            column,  # type: ignore[arg-type]
            group["day"].to_numpy().astype("datetime64[D]"),
            group["value"].to_numpy().astype(float),
            group["n"].to_numpy().astype(float),
        )
        detector = _DETECTORS[signal]
        if signal is Signal.ROW_LOSS and not _comparable_grain(ser, s):
            continue
        lo, hi = np.datetime64(since, "D"), np.datetime64(until, "D")
        for i in np.flatnonzero((ser.days >= lo) & (ser.days <= hi)):
            found = detector(ser, i, s)
            if found:
                observed, expected, z = found
                anomalies.append(
                    Anomaly(signal, ser.table, ser.column, ser.segment, ser.days[i].astype(date), observed, expected, z)
                )
    return anomalies


def _comparable_grain(ser: _Series, s: Sensitivity) -> bool:
    finite = ser.values[np.isfinite(ser.values)]
    if not finite.size:
        return False
    typical = float(np.median(finite))
    return s.conservation_ratio_band[0] <= typical <= s.conservation_ratio_band[1]


def _weekday_baseline(ser: _Series, i: int, hist: np.ndarray) -> float:
    weekday = (ser.days[hist].astype("datetime64[D]").view("int64") - 4) % 7  # 1970-01-01 was a Thursday
    today = (ser.days[i].astype("datetime64[D]").view("int64") - 4) % 7
    same = hist[weekday == today]
    return float(np.median(ser.values[same])) if same.size >= 2 else float(np.median(ser.values[hist]))


def _volume(ser: _Series, i: int, s: Sensitivity) -> tuple[float, float, float] | None:
    hist = ser.history(i, s.volume_lookback_days)
    if hist.size < s.min_history:
        return None
    baseline = _weekday_baseline(ser, i, hist)
    ratios = []
    for j in hist:
        h = ser.history(j, s.volume_lookback_days)
        if h.size >= 3:
            ratios.append(np.log((ser.values[j] + 1) / (_weekday_baseline(ser, j, h) + 1)))
    # Poisson floor: small feeds are noisy, a log-ratio of a count near b has sd ~ 1/sqrt(b).
    scale = max(_mad(np.array(ratios)), 0.05, 1.0 / np.sqrt(baseline + 1))
    r = float(np.log((ser.values[i] + 1) / (baseline + 1)))
    z = r / scale
    if abs(z) >= s.z_threshold and abs(np.expm1(r)) >= s.volume_min_change:
        return float(ser.values[i]), baseline, z
    return None


def _rate(ser: _Series, i: int, s: Sensitivity) -> tuple[float, float, float] | None:
    hist = ser.history(i, s.lookback_days)
    if hist.size < s.min_history or ser.n[i] <= 0:
        return None
    p, p0 = float(ser.values[i]), float(np.median(ser.values[hist]))
    sd = max(_binomial_sd(p0, ser.n[i]), _mad(ser.values[hist]))
    z = (p - p0) / sd
    if z >= s.z_threshold and p - p0 >= s.rate_min_increase:
        return p, p0, z
    return None


def _value(ser: _Series, i: int, s: Sensitivity) -> tuple[float, float, float] | None:
    hist = ser.history(i, s.lookback_days)
    if hist.size < s.min_history or not np.isfinite(ser.values[i]):
        return None
    v, b = float(ser.values[i]), float(np.median(ser.values[hist]))
    h = ser.values[hist]
    if v > 0 and b > 0 and (h > 0).all():
        change, scale = np.log(v / b), max(_mad(np.log(h)), 0.01)
    else:
        change, scale = (v - b) / (abs(b) or 1.0), max(_mad(h) / (abs(b) or 1.0), 0.01)
    z = float(change / scale)
    if abs(z) >= s.z_threshold + 1 and abs(change) >= s.value_min_change:
        return v, b, z
    return None


def _lateness(ser: _Series, i: int, s: Sensitivity) -> tuple[float, float, float] | None:
    hist = ser.history(i, s.lookback_days)
    if hist.size < s.min_history or not np.isfinite(ser.values[i]):
        return None
    v, b = float(ser.values[i]), float(np.median(ser.values[hist]))
    z = (v - b) / max(_mad(ser.values[hist]), 0.05)
    if z >= s.z_threshold and v - b >= s.lateness_min_days:
        return v, b, z
    return None


def _row_loss(ser: _Series, i: int, s: Sensitivity) -> tuple[float, float, float] | None:
    hist = ser.history(i, s.lookback_days)
    if hist.size < s.min_history or not np.isfinite(ser.values[i]):
        return None
    v = float(ser.values[i])
    b = _weekday_baseline(ser, i, hist)
    binomial = _binomial_sd(b, ser.n[i]) if b <= 1 else 0.0
    z = (v - b) / max(_mad(ser.values[hist]), 0.005, binomial)
    if abs(z) >= s.z_threshold and b > 0 and abs(v - b) / b >= s.row_loss_min_change:
        return v, b, z
    return None


_DETECTORS = {
    Signal.VOLUME: _volume,
    Signal.NULL_RATE: _rate,
    Signal.DUPLICATE_RATE: _rate,
    Signal.VALUE_SHIFT: _value,
    Signal.LATENESS: _lateness,
    Signal.ROW_LOSS: _row_loss,
}


# -- episodes ---------------------------------------------------------------------------------


def to_incidents(anomalies: list[Anomaly], segment_columns: dict[str, str | None]) -> list[HealthIncident]:
    """Merges anomalies of the same series and direction on consecutive days into incidents."""
    groups: dict[tuple[object, ...], list[Anomaly]] = defaultdict(list)
    for a in anomalies:
        groups[(a.signal, a.table, a.column, a.segment, a.score > 0)].append(a)

    incidents = []
    for (_, table, *_rest), items in groups.items():
        items.sort(key=lambda a: a.day)
        run: list[Anomaly] = []
        for a in items:
            if run and a.day - run[-1].day > timedelta(days=MAX_GAP_DAYS + 1):
                incidents.append(_incident(run, segment_columns.get(table)))  # type: ignore[arg-type]
                run = []
            run.append(a)
        incidents.append(_incident(run, segment_columns.get(table)))  # type: ignore[arg-type]
    return sorted(incidents, key=lambda x: (x.start, -abs(x.peak_score)))


def _incident(run: list[Anomaly], segment_column: str | None) -> HealthIncident:
    peak = max(run, key=lambda a: abs(a.score))
    if peak.signal is Signal.ROW_LOSS and peak.segment == "*":
        segment_column = None
    return HealthIncident(
        signal=peak.signal,
        table=peak.table,
        column=peak.column,
        segment=None if peak.segment == "*" else peak.segment,
        segment_column=segment_column,
        start=run[0].day,
        end=run[-1].day,
        peak_score=round(peak.score, 2),
        observed=peak.observed,
        expected=peak.expected,
        summary=summarize(peak, segment_column),
        days=[a.day for a in run],
    )


def summarize(a: Anomaly, segment_column: str | None) -> str:
    where = f" [{segment_column}={a.segment}]" if a.segment not in (None, "*") and segment_column else ""
    change = (a.observed - a.expected) / a.expected if a.expected else float("inf")
    match a.signal:
        case Signal.VOLUME:
            return f"{a.table}{where}: {a.observed:,.0f} rows loaded vs ~{a.expected:,.0f} expected ({change:+.0%})"
        case Signal.NULL_RATE:
            return f"{a.table}.{a.column}{where}: {a.observed:.1%} NULL vs {a.expected:.1%} usually"
        case Signal.DUPLICATE_RATE:
            return f"{a.table}{where}: {a.observed:.1%} rows repeat key ({a.column}) vs {a.expected:.1%} usually"
        case Signal.VALUE_SHIFT:
            return f"{a.table}.{a.column}{where}: mean {a.observed:,.4g} vs {a.expected:,.4g} usually ({change:+.1%})"
        case Signal.LATENESS:
            return f"{a.table}{where}: rows arrive {a.observed:.1f} days after {a.column} vs {a.expected:.1f} usually"
        case Signal.ROW_LOSS:
            return f"{a.column} -> {a.table}{where}: {a.observed:.1%} of rows kept vs {a.expected:.1%} usually"
        case _:
            return f"{a.signal} on {a.table}"
