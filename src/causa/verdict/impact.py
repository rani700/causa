"""How much of a metric's change a data incident is responsible for.

Each day ``d`` of the current period is compared with day ``d - shift`` of the baseline (a *pair*).
An incident can touch either side of a pair: on the current side it distorts the present, on the
baseline side it distorts what the present is compared with. Three estimators, each explicit about
its assumption:

* **difference-in-differences** (default, needs a control group): the affected subset's expected
  value is its value on the other side of the pair, moved by how the *rest of the metric* moved on
  that pair, adjusted by the subset's own drift on untouched pairs. Uncertainty comes from the
  historical day-level volatility of subset vs rest, which also yields an *alignment* z-score.
* **proportional value shift**: a column the measure is computed from was off by a known factor
  (e.g. an FX rate x1.35), so affected rows are off by the same factor. Assumes the column acts
  multiplicatively.
* **row loss**: a known share ``p`` of rows was dropped (NULLs in a gating column), so the observed
  value is ``1 - p`` of the true one. Assumes the lost rows were average.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from causa.metrics.attribution import Period
from causa.metrics.definitions import MetricDefinition
from causa.sqlutil import dt
from causa.verdict.linkage import Subset
from causa.warehouse import Warehouse

FLOOR = 1e-3  # stands in for a zero when taking logs (a fully lost day)
NO_CONTROL_SHARE = 0.9
Pair = tuple[date, date]


@dataclass
class ImpactEstimate:
    subset: Subset
    method: str
    current_days: list[date]  # current-period days the incident touched
    baseline_days: list[date]  # baseline days the incident touched
    impact: float  # effect of the incident on the metric's change (current - baseline)
    sd: float | None = None
    alignment_z: float | None = None
    subset_share: float = 0.0  # the subset's share of the metric on the affected pairs' baseline
    notes: list[str] = field(default_factory=list)

    @property
    def low(self) -> float:
        return self.impact - 2 * (self.sd or 0.0)

    @property
    def high(self) -> float:
        return self.impact + 2 * (self.sd or 0.0)

    @property
    def aligned(self) -> bool:
        return self.alignment_z is not None and abs(self.alignment_z) >= 3.0


@dataclass
class DailySeries:
    """Daily metric values for a subset and for the rest, over the periods and their history."""

    subset: Subset
    current: Period
    baseline: Period
    seg: dict[date, float]
    rest: dict[date, float]
    history_windows: int

    @property
    def shift(self) -> int:
        return (self.current.start - self.baseline.start).days

    def pairs(self) -> list[Pair]:
        c0 = self.current.start
        return [(c0 + timedelta(days=i), c0 + timedelta(days=i - self.shift)) for i in range(self.current.days)]

    def s(self, d: date) -> float:
        return self.seg.get(d, 0.0)

    def r(self, d: date) -> float:
        return self.rest.get(d, 0.0)

    def touched(self, days: set[date]) -> list[Pair]:
        return [(c, b) for c, b in self.pairs() if c in days or b in days]

    def share(self, pairs: list[Pair]) -> float:
        total = sum(self.s(b) + self.r(b) for _, b in pairs)
        return sum(self.s(b) for _, b in pairs) / total if total > 0 else 0.0


def daily_sql(metric: MetricDefinition, subset: Subset, first: date, last: date) -> str:
    where = f"m.{metric.time_column} BETWEEN {dt(first)} AND {dt(last)}"
    if metric.filter:
        where += f" AND ({metric.filter})"
    return f"""SELECT m.{metric.time_column} AS D, SUM(m.{metric.measure_column}) AS TOTAL,
       SUM(CASE WHEN {subset.predicate} THEN m.{metric.measure_column} ELSE 0 END) AS SEG
FROM {metric.table} m {subset.join}
WHERE {where}
GROUP BY m.{metric.time_column}"""


def load_daily(
    wh: Warehouse, metric: MetricDefinition, current: Period, baseline: Period, subset: Subset, history_windows: int = 8
) -> DailySeries:
    shift = (current.start - baseline.start).days
    first = current.start - timedelta(days=shift * (history_windows + 1))
    seg: dict[date, float] = {}
    rest: dict[date, float] = {}
    for d, total, s in wh.query(daily_sql(metric, subset, first, current.end)).iter_rows():
        seg[d] = float(s or 0.0)
        rest[d] = float(total or 0.0) - seg[d]
    return DailySeries(subset, current, baseline, seg, rest, history_windows)


# -- difference-in-differences ------------------------------------------------------------------


def did_estimate(series: DailySeries, incident_days: set[date]) -> ImpactEstimate:
    touched = series.touched(incident_days)
    clean = [p for p in series.pairs() if p not in touched]
    notes: list[str] = []

    def ratio(items: list[Pair], get: Callable[[date], float]) -> float | None:
        num, den = sum(get(c) for c, _ in items), sum(get(b) for _, b in items)
        return num / den if den > 0 and num > 0 else None

    # Without a control group (the subset is essentially the whole metric) there is nothing to
    # compare against but the metric's own trend on unaffected days.
    everything = series.subset.everything or series.share(series.pairs()) >= NO_CONTROL_SHARE
    if everything:
        method = "change of the metric on unaffected days"
        overall = ratio(clean, series.s) if len(clean) >= 2 else None
        if overall is None:
            overall = 1.0
            notes.append("no unaffected days to measure the trend on; assumed flat")
        growth = dict.fromkeys(touched, overall)
        adjustment = 1.0
    else:
        method = "difference-in-differences against the rest of the metric"
        growth = {(c, b): (series.r(c) / series.r(b) if series.r(b) > 0 else 1.0) for c, b in touched}
        seg_clean, rest_clean = ratio(clean, series.s), ratio(clean, series.r)
        if len(clean) >= 2 and seg_clean and rest_clean:
            adjustment = min(max(seg_clean / rest_clean, 0.5), 2.0)
        else:
            adjustment = 1.0
            notes.append("too few unaffected days to measure the segment's own trend; assumed it moved like the rest")

    impact = 0.0
    expected_values: list[float] = []
    log_q: list[float] = []
    for c, b in touched:
        g = growth[(c, b)] * adjustment
        if c in incident_days and b in incident_days:
            notes.append(f"{c} and its comparison day {b} are both affected; left out")
            continue
        if c in incident_days:
            expected = series.s(b) * g
            impact += series.s(c) - expected
            log_q.append(math.log(max(series.s(c), FLOOR) / max(expected, FLOOR)))
        else:
            expected = series.s(c) / g if g > 0 else series.s(b)
            impact += expected - series.s(b)
            log_q.append(-math.log(max(series.s(b), FLOOR) / max(expected, FLOOR)))
        expected_values.append(expected)

    volatility = _volatility(series, incident_days, everything)
    sd = math.sqrt(sum((e * volatility) ** 2 for e in expected_values)) if volatility is not None else None
    z = statistics.fmean(log_q) / (volatility / math.sqrt(len(log_q))) if volatility and log_q else None
    return ImpactEstimate(
        series.subset,
        method,
        sorted(c for c, _ in touched if c in incident_days),
        sorted(b for _, b in touched if b in incident_days),
        impact,
        sd,
        z,
        series.share(touched),
        notes,
    )


def _volatility(series: DailySeries, incident_days: set[date], everything: bool) -> float | None:
    """Robust day-level volatility of the subset relative to the rest (log scale) over past pairs."""
    values = []
    for k in range(1, series.history_windows + 1):
        for c, b in series.pairs():
            hc, hb = c - timedelta(days=k * series.shift), b - timedelta(days=k * series.shift)
            if hc in incident_days or hb in incident_days:
                continue
            s_c, s_b = series.s(hc), series.s(hb)
            if s_c <= 0 or s_b <= 0:
                continue
            if everything:
                values.append(math.log(s_c / s_b))
                continue
            r_c, r_b = series.r(hc), series.r(hb)
            if r_c > 0 and r_b > 0:
                values.append(math.log((s_c / s_b) / (r_c / r_b)))
    if len(values) < 10:
        return None
    centre = statistics.median(values)
    return max(1.4826 * statistics.median(abs(v - centre) for v in values), 0.01)


# -- mechanistic estimators ---------------------------------------------------------------------


def proportional_estimate(series: DailySeries, factors: dict[date, float]) -> ImpactEstimate:
    """Rows of the subset were inflated by ``factor`` on the given days."""
    impact = 0.0
    touched = series.touched(set(factors))
    for c, b in touched:
        if c in factors and factors[c] > 0:
            impact += series.s(c) * (1 - 1 / factors[c])
        if b in factors and factors[b] > 0:
            impact -= series.s(b) * (1 - 1 / factors[b])
    return ImpactEstimate(
        series.subset,
        "proportional value shift (assumes the shifted column scales the measure)",
        sorted(c for c, _ in touched if c in factors),
        sorted(b for _, b in touched if b in factors),
        impact,
        subset_share=series.share(touched),
    )


def row_loss_estimate(series: DailySeries, loss_share: dict[date, float]) -> ImpactEstimate:
    """A share ``p`` of the subset's rows was dropped on the given days (observed = (1 - p) x true)."""
    impact = 0.0
    touched = series.touched(set(loss_share))
    notes = []
    for c, b in touched:
        for day, value, sign in ((c, series.s(c), 1.0), (b, series.s(b), -1.0)):
            p = loss_share.get(day)
            if p is None:
                continue
            if p >= 0.95:
                notes.append(f"{day}: {p:.0%} of rows lost; too much to scale up from what is left")
                continue
            impact -= sign * value * p / (1 - p)
    return ImpactEstimate(
        series.subset,
        "row loss (assumes the dropped rows were average)",
        sorted(c for c, _ in touched if c in loss_share),
        sorted(b for _, b in touched if b in loss_share),
        impact,
        subset_share=series.share(touched),
        notes=notes,
    )
