"""Engine 3: explains *why* a metric changed between two periods.

Recursive, pushed-down drill-down in the spirit of Adtributor (Bhagwan et al., NSDI 2014):

1. One ``GROUPING SETS`` query returns current and baseline values for every element of every
   dimension (all aggregation happens in the warehouse).
2. A dimension *explains* the change if a few of its elements (``max_elements``), each carrying at
   least ``min_element_share`` of it, together carry ``target_share``. Among those dimensions the
   one whose composition shifted most (Jensen-Shannon divergence of shares) wins.
3. For each explaining element (a *driver*) the procedure repeats inside it with a new query, so the
   explanation narrows: region -> country -> customer.

Every segment's change is split exactly into a **volume effect** and a **rate effect** (value per
unit of volume) with the two-player Shapley value. Drivers whose change is spread proportionally
over all their sub-segments are marked **uniform**: losing rows looks like that, a business change
usually does not.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from causa.metrics.definitions import MetricDefinition, VolumeAggregation
from causa.sqlutil import dt
from causa.warehouse import Warehouse

BLANK = "(blank)"
MIN_HISTORY_WINDOWS = 6
Filters = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period ends before it starts: {self.start}..{self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def preceding(self) -> Period:
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))

    def same_weekdays_before(self) -> Period:
        """The closest earlier period covering the same weekdays (weekly seasonality cancels out)."""
        return self.shifted(-7 * math.ceil(self.days / 7))

    def shifted(self, days: int) -> Period:
        return Period(self.start + timedelta(days=days), self.end + timedelta(days=days))

    def overlaps(self, other: Period) -> bool:
        return self.start <= other.end and other.start <= self.end

    @classmethod
    def parse(cls, text: str) -> Period:
        start, _, end = text.partition("..")
        return cls(date.fromisoformat(start), date.fromisoformat(end or start))

    def __str__(self) -> str:
        return f"{self.start}..{self.end}" if self.days > 1 else str(self.start)


def shapley_split(m0: float, m1: float, v0: float | None, v1: float | None) -> tuple[float, float] | None:
    """Splits m1 - m0, where m = volume x rate, into (volume effect, rate effect). The two effects
    always add up to the change exactly. A segment that appears or disappears is pure volume."""
    if v0 is None or v1 is None:
        return None
    if v0 <= 0 and v1 <= 0:
        return 0.0, m1 - m0
    r0 = m0 / v0 if v0 > 0 else m1 / v1
    r1 = m1 / v1 if v1 > 0 else r0
    return (v1 - v0) * (r0 + r1) / 2, (r1 - r0) * (v0 + v1) / 2


@dataclass
class Segment:
    filters: Filters
    current: float
    baseline: float
    volume_current: float | None = None
    volume_baseline: float | None = None

    @property
    def delta(self) -> float:
        return self.current - self.baseline

    @property
    def pct(self) -> float | None:
        return self.delta / self.baseline if self.baseline else None

    @property
    def vanished(self) -> bool:
        return self.baseline > 0 and self.current == 0

    @property
    def effects(self) -> tuple[float, float] | None:
        return shapley_split(self.baseline, self.current, self.volume_baseline, self.volume_current)

    @property
    def label(self) -> str:
        return " & ".join(f"{d}={v}" for d, v in self.filters) or "total"

    @property
    def leaf(self) -> tuple[str, str]:
        return self.filters[-1]


@dataclass
class DimensionView:
    dimension: str
    surprise: float  # Jensen-Shannon divergence between baseline and current shares
    proportionality: float  # 1 = every element changed in proportion to its baseline share
    elements: list[Segment]  # by contribution in the direction of the change
    explaining: list[Segment]
    explained_share: float
    # Share of the change the explaining elements carry beyond their share of the baseline: big
    # segments explain a lot by size alone, which says nothing about *why*.
    excess: float = 0.0

    def succinct(self, target_share: float, max_elements: int) -> bool:
        return bool(self.explaining) and self.explained_share >= target_share and len(self.explaining) <= max_elements


@dataclass
class Driver:
    segment: Segment
    dimension: str
    share: float  # of the root change
    uniform: bool = False
    inner: list[DimensionView] = field(default_factory=list)
    children: list[Driver] = field(default_factory=list)
    offsets: list[Segment] = field(default_factory=list)


@dataclass
class Explanation:
    metric: MetricDefinition
    current: Period
    baseline: Period
    total: Segment
    dimensions: list[DimensionView]
    drivers: list[Driver]
    offsets: list[Segment]
    queries: int
    significance: float | None = None  # robust z of the relative change vs comparable past changes
    typical_change: float | None = None  # robust sd of comparable past relative changes

    @property
    def unusual(self) -> bool | None:
        return None if self.significance is None else abs(self.significance) >= 2.0

    def walk(self) -> list[tuple[int, Driver]]:
        out: list[tuple[int, Driver]] = []

        def visit(drivers: list[Driver], depth: int) -> None:
            for d in drivers:
                out.append((depth, d))
                visit(d.children, depth + 1)

        visit(self.drivers, 0)
        return out


@dataclass(frozen=True)
class AttributionSettings:
    target_share: float = 0.67  # Adtributor's TEEP
    min_element_share: float = 0.10  # Adtributor's TEP
    max_elements: int = 3
    min_driver_share: float = 0.05  # of the root change, to keep drilling
    max_depth: int = 3
    uniform_proportionality: float = 0.85
    min_lift: float = 1.3  # an element must carry 30% more of the change than of the baseline
    min_excess: float = 0.15
    history_windows: int = 10  # comparable past windows used to judge significance
    offset_share: float = 0.25  # an offset must undo at least this share of what the drivers explain
    max_offsets: int = 3


class Attributor:
    def __init__(self, wh: Warehouse, settings: AttributionSettings | None = None) -> None:
        self._wh = wh
        self.s = settings or AttributionSettings()
        self._queries = 0

    def explain(
        self, metric: MetricDefinition, current: Period, baseline: Period | None = None, filters: Filters = ()
    ) -> Explanation:
        baseline = baseline or current.same_weekdays_before()
        if current.overlaps(baseline):
            raise ValueError(f"current {current} and baseline {baseline} overlap")
        if current.days != baseline.days:
            raise ValueError(f"periods differ in length ({current.days} vs {baseline.days} days)")
        self._queries = 0
        total, views = self._level(metric, current, baseline, filters)
        sign = _direction(total, views)
        drivers, offsets = self._drill(metric, current, baseline, filters, total, views, total.delta, sign, 0)
        significance, typical = self._significance(metric, current, baseline, filters, total)
        return Explanation(
            metric, current, baseline, total, views, drivers, offsets, self._queries, significance, typical
        )

    def _significance(
        self, metric: MetricDefinition, current: Period, baseline: Period, filters: Filters, total: Segment
    ) -> tuple[float | None, float | None]:
        """Compares the change with the same comparison made over the previous windows."""
        if total.pct is None:
            return None, None
        shift = (current.start - baseline.start).days
        first = current.shifted(-shift * (self.s.history_windows + 1)).start
        where = [f"{metric.time_column} BETWEEN {dt(first)} AND {dt(baseline.end)}", *_filter_sql(filters)]
        if metric.filter:
            where.append(f"({metric.filter})")
        self._queries += 1
        daily = self._wh.query(
            f"""SELECT {metric.time_column} AS D, SUM({metric.measure_column}) AS M FROM {metric.table}
            WHERE {" AND ".join(where)} GROUP BY {metric.time_column}"""
        )
        by_day = dict(zip(daily["d"].to_list(), daily["m"].cast(pl.Float64).to_list(), strict=True))

        def window_sum(p: Period) -> float:
            return sum(by_day.get(p.start + timedelta(days=i), 0.0) for i in range(p.days))

        changes = []
        for k in range(1, self.s.history_windows + 1):
            cur, base = window_sum(current.shifted(-k * shift)), window_sum(current.shifted(-(k + 1) * shift))
            if base > 0 and cur > 0:
                changes.append(cur / base - 1)
        if len(changes) < MIN_HISTORY_WINDOWS:
            return None, None  # too little history for a trustworthy spread
        centre = statistics.median(changes)
        typical = 1.4826 * statistics.median(abs(c - centre) for c in changes)
        if typical <= 0:
            return None, None
        return (total.pct - centre) / typical, typical

    # -- recursion ---------------------------------------------------------------------------

    def _drill(
        self,
        metric: MetricDefinition,
        current: Period,
        baseline: Period,
        filters: Filters,
        total: Segment,
        views: list[DimensionView],
        root_delta: float,
        sign: float,
        depth: int,
    ) -> tuple[list[Driver], list[Segment]]:
        candidates = [
            v for v in views if v.succinct(self.s.target_share, self.s.max_elements) and v.excess >= self.s.min_excess
        ]
        if not candidates:
            return [], []
        best = max(candidates, key=lambda v: (v.excess, v.surprise))
        explained = abs(sum(e.delta for e in best.explaining))
        offsets = sorted(
            (e for e in best.elements if e.delta * sign < 0 and abs(e.delta) >= self.s.offset_share * explained),
            key=lambda e: e.delta * sign,
        )[: self.s.max_offsets]
        drivers = []
        for element in best.explaining:
            if depth > 0 and abs(element.delta) < self.s.min_driver_share * abs(root_delta):
                continue
            driver = Driver(element, best.dimension, element.delta / root_delta if root_delta else 0.0)
            if depth + 1 < self.s.max_depth and abs(element.delta) >= self.s.min_driver_share * abs(root_delta):
                inner_total, inner_views = self._level(metric, current, baseline, element.filters)
                driver.inner = inner_views
                shallow = [v for v in inner_views if v.dimension in metric.dimensions]
                # "Every part moved alike" is informative for a broad segment (a region, a feed),
                # not for a single customer, and trivially true for a segment that vanished.
                concentrated = any(
                    v.succinct(self.s.target_share, self.s.max_elements) and v.excess >= self.s.min_excess
                    for v in inner_views
                )
                driver.uniform = (
                    best.dimension in metric.dimensions
                    and not element.vanished
                    and not concentrated
                    and bool(shallow)
                    and all(v.proportionality >= self.s.uniform_proportionality for v in shallow)
                )
                driver.children, driver.offsets = self._drill(
                    metric, current, baseline, element.filters, inner_total, inner_views, root_delta, sign, depth + 1
                )
            drivers.append(driver)
        return drivers, offsets

    # -- one level: one pushed-down query ----------------------------------------------------------

    def _level(
        self, metric: MetricDefinition, current: Period, baseline: Period, filters: Filters
    ) -> tuple[Segment, list[DimensionView]]:
        used = {d for d, _ in filters}
        dims = [d for d in metric.all_dimensions if d not in used]
        self._queries += 1
        rows = self._wh.query(cube_sql(metric, current, baseline, filters, dims))
        has_volume = metric.volume_column is not None

        def seg(row: dict[str, object], extra: Filters) -> Segment:
            return Segment(
                filters + extra,
                float(row["cur"] or 0.0),  # type: ignore[arg-type]
                float(row["base"] or 0.0),  # type: ignore[arg-type]
                float(row["vcur"] or 0.0) if has_volume else None,  # type: ignore[arg-type]
                float(row["vbase"] or 0.0) if has_volume else None,  # type: ignore[arg-type]
            )

        total = Segment(filters, 0.0, 0.0, 0.0 if has_volume else None, 0.0 if has_volume else None)
        by_dim: dict[str, list[Segment]] = {d: [] for d in dims}
        for row in rows.iter_rows(named=True):
            grouped = [d for d in dims if row[d.lower()] is not None]
            if not grouped:
                total = seg(row, ())
            elif len(grouped) == 1:
                d = grouped[0]
                by_dim[d].append(seg(row, ((d, str(row[d.lower()])),)))

        sign = 1.0 if total.delta >= 0 else -1.0
        views = [self._view(d, elements, total, sign) for d, elements in by_dim.items() if elements]
        views.sort(key=lambda v: -v.surprise)
        return total, views

    def _view(self, dimension: str, elements: list[Segment], total: Segment, sign: float) -> DimensionView:
        elements = sorted(elements, key=lambda e: -e.delta * sign)
        explaining: list[Segment] = []
        explained = excess = 0.0
        if total.delta:
            for e in elements:
                share = e.delta / total.delta
                if share < self.s.min_element_share or explained >= self.s.target_share:
                    break
                base_share = e.baseline / total.baseline if total.baseline > 0 else 0.0
                if base_share > 0 and share / base_share < self.s.min_lift:
                    continue  # explains a lot only because it is big
                explaining.append(e)
                explained += share
                # Capped: with offsets elsewhere one element can carry >100% of the net change, which
                # must not make a coarse segment outscore a finer one that explains it completely.
                excess += min(share, 1.0) - base_share
        return DimensionView(
            dimension,
            _js_divergence([e.baseline for e in elements], [e.current for e in elements]),
            _proportionality(elements, total),
            elements,
            explaining,
            explained,
            excess,
        )


def cube_sql(metric: MetricDefinition, current: Period, baseline: Period, filters: Filters, dims: list[str]) -> str:
    t = metric.time_column
    is_cur = f"CASE WHEN {t} BETWEEN {dt(current.start)} AND {dt(current.end)} THEN 1 ELSE 0 END"
    cols = [f"CAST(COALESCE(CAST({d} AS VARCHAR(200)), '{BLANK}') AS VARCHAR(200)) AS {d}" for d in dims]
    cols += [f"{is_cur} AS IS_CUR", f"{metric.measure_column} AS M"]
    if metric.volume_column:
        cols.append(f"{metric.volume_column} AS V")

    in_base = f"{t} BETWEEN {dt(baseline.start)} AND {dt(baseline.end)}"
    in_current = f"{t} BETWEEN {dt(current.start)} AND {dt(current.end)}"
    where = [f"({in_base} OR {in_current})"]
    where += _filter_sql(filters)
    if metric.filter:
        where.append(f"({metric.filter})")

    aggs = [
        "SUM(CASE WHEN IS_CUR = 1 THEN M ELSE 0 END) AS CUR",
        "SUM(CASE WHEN IS_CUR = 0 THEN M ELSE 0 END) AS BASE",
    ]
    if metric.volume_column:
        for name, flag in (("VCUR", 1), ("VBASE", 0)):
            match metric.volume_aggregation:
                case VolumeAggregation.COUNT_DISTINCT:
                    aggs.append(f"COUNT(DISTINCT CASE WHEN IS_CUR = {flag} THEN V END) AS {name}")
                case VolumeAggregation.SUM:
                    aggs.append(f"SUM(CASE WHEN IS_CUR = {flag} THEN V ELSE 0 END) AS {name}")
                case VolumeAggregation.COUNT:
                    aggs.append(f"SUM(CASE WHEN IS_CUR = {flag} THEN 1 ELSE 0 END) AS {name}")
    sets = ", ".join(["()", *(f"({d})" for d in dims)])
    return f"""WITH B AS (SELECT {", ".join(cols)} FROM {metric.table} WHERE {" AND ".join(where)})
SELECT {", ".join([*dims, *aggs])}
FROM B
GROUP BY GROUPING SETS ({sets})"""


def _filter_sql(filters: Filters) -> list[str]:
    return [f"{d} IS NULL" if v == BLANK else f"CAST({d} AS VARCHAR(200)) = '{_quote(v)}'" for d, v in filters]


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _direction(total: Segment, views: list[DimensionView]) -> float:
    if total.delta:
        return 1.0 if total.delta > 0 else -1.0
    biggest = max((e for v in views for e in v.elements), key=lambda e: abs(e.delta), default=None)
    return 1.0 if biggest is None or biggest.delta >= 0 else -1.0


def _js_divergence(p_raw: list[float], q_raw: list[float]) -> float:
    p = [max(x, 0.0) for x in p_raw]
    q = [max(x, 0.0) for x in q_raw]
    sp, sq = sum(p), sum(q)
    if sp <= 0 or sq <= 0:
        return 0.0
    total = 0.0
    for a, b in zip(p, q, strict=True):
        a, b = a / sp, b / sq
        m = (a + b) / 2
        if a > 0:
            total += 0.5 * a * math.log(a / m)
        if b > 0:
            total += 0.5 * b * math.log(b / m)
    return total


def _proportionality(elements: list[Segment], total: Segment) -> float:
    """1 - (share of the change that is *not* allocated in proportion to baseline shares).

    1.0 means every element moved by the same percentage; 0.0 means the change sits entirely where
    the baseline did not predict it.
    """
    if not total.delta or total.baseline <= 0:
        return 0.0
    misallocated = sum(abs(e.delta - (e.baseline / total.baseline) * total.delta) for e in elements)
    return max(0.0, 1.0 - misallocated / (2 * abs(total.delta)))


def explanation_frame(explanation: Explanation) -> pl.DataFrame:
    rows = []
    for depth, d in explanation.walk():
        effects = d.segment.effects
        rows.append(
            {
                "depth": depth,
                "segment": d.segment.label,
                "current": d.segment.current,
                "baseline": d.segment.baseline,
                "delta": d.segment.delta,
                "share_of_change": d.share,
                "volume_effect": effects[0] if effects else None,
                "rate_effect": effects[1] if effects else None,
                "uniform": d.uniform,
            }
        )
    return pl.DataFrame(rows)
