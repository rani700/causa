"""Engine 4: how much of a metric's change is real, and how much is broken data.

For one metric and two periods the investigator combines the three engines:

1. **attribution** (engine 3) says where the change sits;
2. every **health case** (engine 2) on a table that feeds the metric (lineage, engine 1) and whose
   days fall in either period is linked to the metric's rows, and its impact on the change is
   estimated with the estimator that fits its mechanism;
3. **evidence** for and against "this is a data incident" is weighed in log-odds, so every verdict
   carries its reasons and a confidence;
4. the **business remainder** (total change minus data impact) is explained again by attribution,
   using only the days no data incident touched.

Numbers people saw at the time and that were corrected since (**restatements**) are reported too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from causa.health import HealthReport, HealthScanner, Severity, Signal
from causa.health.models import HealthCase, HealthIncident
from causa.lineage import LineageGraph
from causa.lineage.parser import ColumnRef
from causa.metrics.attribution import Attributor, Explanation, Period
from causa.metrics.definitions import MetricDefinition
from causa.metrics.snapshots import Restatement, restatements
from causa.sources.catalog import Catalog
from causa.sqlutil import dt
from causa.verdict.impact import (
    DailySeries,
    ImpactEstimate,
    did_estimate,
    load_daily,
    proportional_estimate,
    row_loss_estimate,
)
from causa.verdict.linkage import (
    CaseLink,
    Subset,
    all_incidents,
    filters_subset,
    link_case,
    link_case_value,
    relevant,
)
from causa.warehouse import Warehouse

INTEGRITY_SIGNALS = {
    Signal.SCHEMA_CHANGE,
    Signal.FAILED_WRITE,
    Signal.NULL_RATE,
    Signal.DUPLICATE_RATE,
    Signal.LATENESS,
    Signal.ROW_LOSS,
}
PRIOR = -1.5  # log-odds before evidence: most metric moves are not data incidents
DECISION = 0.6


@dataclass(frozen=True)
class Evidence:
    text: str
    weight: float  # log-odds; positive supports "data incident", negative supports "business"


@dataclass
class DriverOverlap:
    driver: str
    purity: float  # share of the driver's baseline value that the incident's subset covers
    coverage: float  # share of the subset's baseline value that falls in the driver


@dataclass
class DataFinding:
    case: HealthCase
    link: CaseLink
    business_days: list[date]
    estimate: ImpactEstimate
    check: ImpactEstimate | None  # the statistical estimate, when a mechanistic one was used
    evidence: list[Evidence]
    overlaps: list[DriverOverlap] = field(default_factory=list)
    duplicate_of: str | None = None  # same rows on the same days as another finding: not counted twice
    replaced: ImpactEstimate | None = None  # the estimate a counterfactual replay superseded
    replay_note: str | None = None  # why no replay was done, when one was attempted
    replay: object | None = None  # the ReplayResult behind the estimate, when there is one

    @property
    def score(self) -> float:
        return PRIOR + sum(e.weight for e in self.evidence)

    @property
    def confidence(self) -> float:
        return 1 / (1 + math.exp(-self.score))

    @property
    def is_data(self) -> bool:
        return self.confidence >= DECISION


@dataclass
class Verdict:
    metric: MetricDefinition
    explanation: Explanation
    findings: list[DataFinding]
    business: Explanation | None
    excluded_days: list[date]
    restatements: list[Restatement]

    @property
    def data_findings(self) -> list[DataFinding]:
        return [f for f in self.findings if f.is_data and f.duplicate_of is None]

    @property
    def corroborating(self) -> list[DataFinding]:
        return [f for f in self.findings if f.is_data and f.duplicate_of is not None]

    @property
    def data_impact(self) -> float:
        return sum(f.estimate.impact for f in self.data_findings)

    @property
    def business_change(self) -> float:
        return self.explanation.total.delta - self.data_impact


class Investigator:
    def __init__(
        self,
        wh: Warehouse,
        catalog: Catalog,
        graph: LineageGraph,
        query_log: pl.DataFrame,
        health: HealthReport | None = None,
        replay: bool = False,
    ) -> None:
        self._replay = replay
        self._wh = wh
        self._catalog = catalog
        self._graph = graph
        self._log = query_log
        self._health = health

    def health(self, since: date, until: date) -> HealthReport:
        h = self._health
        if h is None or h.since > since or h.until < until:
            h = HealthScanner(self._wh, self._catalog, self._graph).scan(since, until, self._log)
            self._health = h
        return h

    def verdict(self, metric: MetricDefinition, current: Period, baseline: Period | None = None) -> Verdict:
        baseline = baseline or current.same_weekdays_before()
        explanation = Attributor(self._wh).explain(metric, current, baseline)
        report = self.health(baseline.start, current.end + timedelta(days=3))
        lags = _lags(report)
        window = {baseline.start + timedelta(days=i) for i in range(baseline.days)} | {
            current.start + timedelta(days=i) for i in range(current.days)
        }
        restated = [
            r
            for dim in ["*", *metric.dimensions]
            for r in restatements(self._wh, metric, baseline.start, current.end, dim)
        ]

        findings = []
        for case in report.cases:
            if case.severity is Severity.LOW or not relevant(case, metric, self._graph):
                continue
            days = _business_days(case, lags) & window
            if not days:
                continue
            link = link_case(case, metric, self._graph, self._catalog, report.monitors)
            series = load_daily(self._wh, metric, current, baseline, link.subset)
            estimate, check = self._estimate(metric, case, link, series, days, report, lags)
            finding = DataFinding(case, link, sorted(days), estimate, check, [])
            finding.evidence = _evidence(case, finding, restated)
            finding.overlaps = self._overlaps(metric, baseline, link.subset, explanation)
            findings.append(finding)

        _mark_duplicates(findings)
        if self._replay:
            self._replay_findings(metric, current, baseline, report, findings)
        excluded = sorted({d for f in findings if f.is_data for d in f.business_days})
        business = None
        if excluded:
            remaining = len(window - set(excluded))
            if remaining >= 2:
                business = Attributor(self._wh).explain(metric, current, baseline, excluded_days=frozenset(excluded))
        else:
            business = explanation
        return Verdict(metric, explanation, findings, business, excluded, restated)

    # -- estimation ------------------------------------------------------------------------------

    def _estimate(
        self,
        metric: MetricDefinition,
        case: HealthCase,
        link: CaseLink,
        series: DailySeries,
        days: set[date],
        report: HealthReport,
        lags: dict[str, int],
    ) -> tuple[ImpactEstimate, ImpactEstimate | None]:
        statistical = did_estimate(series, days)
        incidents = all_incidents(case)
        if _healed(incidents):
            healed = ImpactEstimate(
                link.subset,
                HEALED,
                statistical.current_days,
                statistical.baseline_days,
                0.0,
                notes=["it distorted the numbers reported at the time, not today's data"],
            )
            return healed, statistical
        measure_sources = set(self._graph.column_sources(metric.table, metric.measure_column))

        shifted = [
            i
            for i in incidents
            if i.signal is Signal.VALUE_SHIFT
            and not i.evidence.get("mix_sensitive")
            and i.column
            and ColumnRef(i.table, i.column) in measure_sources
        ]
        if shifted:
            factors = _daily_values(report, shifted[0], lags, lambda v, i: v / i.expected if i.expected else 1.0)
            factors = {d: f for d, f in factors.items() if d in days}
            if factors:
                return proportional_estimate(series, factors), statistical

        # A share r of repeated keys inflates the subset by 1 / (1 - r) on those days.
        duplicated = [i for i in incidents if i.signal is Signal.DUPLICATE_RATE and i.observed < 0.95]
        if duplicated:
            factors = _daily_values(report, duplicated[0], lags, lambda v, _: 1 / (1 - v))
            factors = {d: f for d, f in factors.items() if d in days and f > 1}
            if factors:
                estimate = proportional_estimate(series, factors)
                estimate.method = "duplicated rows (observed = true / (1 - duplicate share))"
                return estimate, statistical

        # A partial NULL spike in a gating column drops that share of rows; a complete one leaves
        # nothing to scale up from, so the statistical estimate is used instead.
        partial_nulls = [i for i in incidents if i.signal is Signal.NULL_RATE and i.observed < 0.95]
        if partial_nulls:
            estimate = self._row_loss(metric, link, series, partial_nulls[0], days, report, lags)
            if estimate is not None:
                return estimate, statistical
        return statistical, None

    def _replay_findings(
        self,
        metric: MetricDefinition,
        current: Period,
        baseline: Period,
        report: HealthReport,
        findings: list[DataFinding],
    ) -> None:
        """Supersedes estimates with the exact effect from re-running the pipeline on repaired data."""
        from causa.replay import Replayer  # replay builds on verdict; imported late to avoid a cycle

        replayer = Replayer(self._wh, self._catalog, self._graph, self._log, report)
        for f in findings:
            if not f.is_data or f.duplicate_of or f.estimate.method == HEALED:
                continue
            plan = replayer.plan(f.case, metric)
            if isinstance(plan, str):
                f.replay_note = plan
                continue
            result = replayer.run(plan, metric)
            if not result.faithful:
                f.replay_note = "control replay did not reproduce the logged row counts; estimate kept"
                continue
            f.replaced = f.estimate
            f.replay = result
            f.estimate = ImpactEstimate(
                f.estimate.subset,
                f"counterfactual replay: {plan.repair.description}; re-ran {plan.statements.height} pipeline "
                f"statements on {', '.join(plan.path)}, the control run reproduced every logged row count",
                f.estimate.current_days,
                f.estimate.baseline_days,
                result.impact(current, baseline),
                subset_share=f.estimate.subset_share,
            )

    def _row_loss(
        self,
        metric: MetricDefinition,
        link: CaseLink,
        series: DailySeries,
        incident: HealthIncident,
        days: set[date],
        report: HealthReport,
        lags: dict[str, int],
    ) -> ImpactEstimate | None:
        """Row loss per segment value, so each feed's loss rate applies to that feed's own value."""
        if link.bridge is None or not link.segment_values or link.segment_column != incident.segment_column:
            shares = {d: p for d, p in _null_shares(report, incident, lags).items() if d in days}
            return row_loss_estimate(series, shares) if shares else None
        total: ImpactEstimate | None = None
        for value in link.segment_values:
            shares = {d: p for d, p in _null_shares(report, incident, lags, value).items() if d in days}
            if not shares:
                continue
            single = link_case_value(link, value)
            part = row_loss_estimate(load_daily(self._wh, metric, series.current, series.baseline, single), shares)
            if total is None:
                total = part
                total.subset = link.subset
                total.method += ", per segment"
            else:
                total.impact += part.impact
                total.current_days = sorted(set(total.current_days) | set(part.current_days))
                total.baseline_days = sorted(set(total.baseline_days) | set(part.baseline_days))
                total.notes += part.notes
        return total

    def _overlaps(
        self, metric: MetricDefinition, baseline: Period, subset: Subset, explanation: Explanation
    ) -> list[DriverOverlap]:
        if subset.everything or not explanation.drivers:
            return []
        out = []
        for driver in explanation.drivers:
            d = filters_subset(driver.segment.filters)
            m = f"m.{metric.measure_column}"
            row = self._wh.query(
                f"""SELECT SUM(CASE WHEN ({subset.predicate}) AND {d.predicate} THEN {m} ELSE 0 END) AS BOTH_,
                       SUM(CASE WHEN {d.predicate} THEN {m} ELSE 0 END) AS DRIVER,
                       SUM(CASE WHEN {subset.predicate} THEN {m} ELSE 0 END) AS SUBSET
                FROM {metric.table} m {subset.join}
                WHERE m.{metric.time_column} BETWEEN {dt(baseline.start)} AND {dt(baseline.end)}"""
            ).row(0, named=True)
            both, drv, sub = (float(row[k] or 0.0) for k in ("both_", "driver", "subset"))
            if both > 0:
                out.append(DriverOverlap(d.label, both / drv if drv else 0.0, both / sub if sub else 0.0))
        return out


# -- helpers -----------------------------------------------------------------------------------


def _mark_duplicates(findings: list[DataFinding]) -> None:
    """Two findings about the same rows on the same days are one incident seen twice (e.g. a feed
    loss and the volume drop it causes). The most specific, most confident one is counted."""
    counted: list[DataFinding] = []
    for f in sorted((f for f in findings if f.is_data), key=lambda f: (f.link.subset.everything, -f.confidence)):
        twin = next((c for c in counted if _same_rows(c, f) and set(c.business_days) & set(f.business_days)), None)
        if twin:
            f.duplicate_of = twin.case.case_id
        else:
            counted.append(f)


def _same_rows(a: DataFinding, b: DataFinding) -> bool:
    if a.link.subset.everything or b.link.subset.everything:
        return True
    if a.link.segment_column == b.link.segment_column:
        return bool(set(a.link.segment_values) & set(b.link.segment_values))
    return False  # different segmentations (e.g. feed vs currency): assume independent


HEALED = "healed: the data was complete again by the time of this analysis"
_LASTING = {Signal.NULL_RATE, Signal.DUPLICATE_RATE, Signal.SCHEMA_CHANGE, Signal.VALUE_SHIFT}


def _healed(incidents: list[HealthIncident]) -> bool:
    """Late batches that arrived and failed writes that were re-run leave today's data complete."""
    signals = {i.signal for i in incidents}
    if signals & _LASTING:
        return False
    recovered = all(i.evidence.get("recovered_at") for i in incidents if i.signal is Signal.FAILED_WRITE)
    return bool(signals & {Signal.LATENESS, Signal.FAILED_WRITE}) and recovered


def _lags(report: HealthReport) -> dict[str, int]:
    """Typical days between a row's business date and its load date, per table."""
    late = report.series.filter(pl.col("signal") == Signal.LATENESS.value)
    out = {}
    for (table,), g in late.group_by(["table"]):
        values = g["value"].drop_nulls()
        if values.len():
            out[str(table)] = max(0, round(float(values.median())))  # type: ignore[arg-type]
    return out


def _business_days(case: HealthCase, lags: dict[str, int]) -> set[date]:
    """Health incidents are dated by load day; metrics by business date."""
    days: set[date] = set()
    for incident in all_incidents(case):
        lag = lags.get(incident.table, 1)
        span = incident.days or [incident.start]
        days |= {d - timedelta(days=lag) for d in span}
    return days


def _daily_values(report, incident: HealthIncident, lags, transform) -> dict[date, float]:  # type: ignore[no-untyped-def]
    s = report.series.filter(
        (pl.col("signal") == incident.signal.value)
        & (pl.col("table") == incident.table)
        & (pl.col("column") == incident.column)
        & (pl.col("segment") == (incident.segment or "*"))
    )
    lag = lags.get(incident.table, 1)
    return {
        d - timedelta(days=lag): transform(v, incident)
        for d, v in zip(s["day"].to_list(), s["value"].to_list(), strict=True)
        if d in set(incident.days) and v is not None
    }


def _null_shares(
    report: HealthReport, incident: HealthIncident, lags: dict[str, int], segment: str | None = None
) -> dict[date, float]:
    """Share of rows lost per business day: NULLs in the gating column (one segment, or all)."""
    s = report.series.filter(
        (pl.col("signal") == Signal.NULL_RATE.value)
        & (pl.col("table") == incident.table)
        & (pl.col("column") == incident.column)
    )
    if segment is not None:
        s = s.filter(pl.col("segment") == segment)
    daily = s.group_by("day").agg(p=(pl.col("value") * pl.col("n")).sum() / pl.col("n").sum())
    lag = lags.get(incident.table, 1)
    return {d - timedelta(days=lag): float(p) for d, p in daily.iter_rows() if p and p > 0.001}


def _evidence(case: HealthCase, finding: DataFinding, restated: list[Restatement]) -> list[Evidence]:
    out = []
    weight = {Severity.CRITICAL: 2.0, Severity.HIGH: 1.5, Severity.MEDIUM: 0.5}.get(case.severity, 0.0)
    out.append(Evidence(f"{case.severity.value} health case {case.case_id} on a table feeding this metric", weight))

    incidents = all_incidents(case)
    integrity = sorted(
        {
            i.signal.value
            for i in incidents
            if i.signal in INTEGRITY_SIGNALS or (i.signal is Signal.VALUE_SHIFT and not i.evidence.get("mix_sensitive"))
        }
    )
    if integrity:
        out.append(Evidence(f"integrity signals: {', '.join(integrity)}", 1.0))
    else:
        out.append(Evidence("only shape changes (volume / mixed averages), no integrity signal", -1.0))
    new_values = [i for i in incidents if i.signal is Signal.NEW_VALUE]
    if new_values:
        text = f"a new category value appeared ({new_values[0].summary}): typical of a business change"
        out.append(Evidence(text, -2.5))

    check = finding.check or finding.estimate
    if check.alignment_z is not None:
        if abs(check.alignment_z) >= 3:
            out.append(
                Evidence(f"affected rows deviated on exactly the incident's days (z={check.alignment_z:+.1f})", 1.5)
            )
        elif abs(check.alignment_z) < 1.5 and finding.check is None:
            out.append(
                Evidence(f"affected rows did not deviate on the incident's days (z={check.alignment_z:+.1f})", -0.5)
            )

    days = set(finding.business_days)
    hits = [r for r in restated if r.metric_date in days]
    if hits:
        out.append(Evidence(f"{len(hits)} reported value(s) on these days were restated afterwards", 1.0))
    return out
