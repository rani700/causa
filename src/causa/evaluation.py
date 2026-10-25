"""Scores Causa against the simulator's ground truth (``SIM.EVENTS``).

This is the only module allowed to read the SIM schema. For the health engine the questions are:
* recall: is every data incident raised as an *alerting* case (severity high or critical)?
* calm: do real business changes stay below alerting severity?
* precision: how many alerting cases match no data incident at all?
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

import polars as pl

from causa.health import HealthReport, HealthScanner, Severity
from causa.health.models import HealthCase
from causa.lineage import build_lineage
from causa.metrics.attribution import Period
from causa.metrics.definitions import MetricDefinition
from causa.sources.catalog import Catalog
from causa.sources.query_log import TableQueryLog
from causa.verdict import Investigator
from causa.warehouse import Warehouse

ALERTING = (Severity.CRITICAL, Severity.HIGH)
LOAD_LAG_DAYS = 1  # business day d lands in the warehouse on d+1
SLACK_DAYS = 2


@dataclass
class EventResult:
    event_id: str
    kind: str
    nature: str
    start: date
    end: date | None
    description: str
    cases: list[HealthCase]

    @property
    def alerted(self) -> bool:
        return any(c.severity in ALERTING for c in self.cases)

    @property
    def correct(self) -> bool:
        return self.alerted if self.nature == "data" else not self.alerted


@dataclass
class HealthEvaluation:
    events: list[EventResult]
    false_alarms: list[HealthCase]

    def _of(self, nature: str) -> list[EventResult]:
        return [e for e in self.events if e.nature == nature]

    @property
    def recall(self) -> float:
        data = self._of("data")
        return sum(e.alerted for e in data) / len(data) if data else 1.0

    @property
    def calm(self) -> float:
        business = self._of("business")
        return sum(not e.alerted for e in business) / len(business) if business else 1.0

    @property
    def precision(self) -> float:
        alerting = {c.case_id for e in self._of("data") for c in e.cases if c.severity in ALERTING}
        total = len(alerting) + len(self.false_alarms)
        return len(alerting) / total if total else 1.0


def load_events(wh: Warehouse) -> pl.DataFrame:
    return wh.query(
        "SELECT EVENT_ID, KIND, NATURE, START_DATE, END_DATE, DESCRIPTION FROM SIM.EVENTS ORDER BY START_DATE"
    )


def evaluate_health(report: HealthReport, events: pl.DataFrame) -> HealthEvaluation:
    results = []
    matched: set[str] = set()
    for ev in events.iter_rows(named=True):
        if not report.since <= ev["start_date"] + timedelta(days=LOAD_LAG_DAYS) <= report.until:
            continue
        lo = ev["start_date"] + timedelta(days=LOAD_LAG_DAYS)
        # Permanent changes are judged on their onset only.
        hi = (ev["end_date"] or ev["start_date"]) + timedelta(days=LOAD_LAG_DAYS + SLACK_DAYS)
        # A case is matched by when its primary incident (its explanation) started.
        cases = [c for c in report.cases if lo - timedelta(days=1) <= c.primary.start <= hi]
        results.append(
            EventResult(
                ev["event_id"], ev["kind"], ev["nature"], ev["start_date"], ev["end_date"], ev["description"], cases
            )
        )
        if ev["nature"] == "data":
            matched |= {c.case_id for c in cases}
    false_alarms = [c for c in report.cases if c.severity in ALERTING and c.case_id not in matched]
    return HealthEvaluation(results, false_alarms)


# -- engine 4: verdicts ---------------------------------------------------------------------------


@dataclass
class VerdictResult:
    event_id: str
    nature: str
    kind: str
    current: str
    reported_change: float
    true_data_gap: float  # reported change minus true change: what data problems did to the number
    estimated_data_impact: float
    band: tuple[float, float] | None
    data_cases: int

    @property
    def error(self) -> float:
        return self.estimated_data_impact - self.true_data_gap

    @property
    def relative_error(self) -> float:
        """Error relative to the size of the change being explained."""
        return abs(self.error) / max(abs(self.reported_change), 1.0)


def true_data_gap(wh: Warehouse, metric_column: str, current: object, baseline: object) -> tuple[float, float]:
    """(reported change, reported change - true change) from the simulator's ground truth."""

    def total(table: str, period) -> float:
        value = wh.scalar(
            f"SELECT SUM({metric_column}) FROM {table} WHERE ORDER_DATE BETWEEN DATE '{period.start}' "
            f"AND DATE '{period.end}'"
        )
        return float(value or 0.0)  # type: ignore[arg-type]

    reported = total("MART.DAILY_REVENUE", current) - total("MART.DAILY_REVENUE", baseline)
    true = total("SIM.TRUE_DAILY_REVENUE", current) - total("SIM.TRUE_DAILY_REVENUE", baseline)
    return reported, reported - true


def evaluate_verdicts(
    wh: Warehouse, metric: MetricDefinition, replay: bool = True, on_event: Callable[[str], None] | None = None
) -> list[VerdictResult]:
    """Judge the week starting at each simulated event and compare the data impact with the truth."""
    events = load_events(wh)
    if events.is_empty():
        return []
    log_frame = TableQueryLog(wh).fetch()
    catalog = Catalog.load(wh)
    graph = build_lineage(log_frame, catalog)
    first = cast(date, events["start_date"].min())
    last = cast(date, events["start_date"].max())
    report = HealthScanner(wh, catalog, graph).scan(first - timedelta(days=7), last + timedelta(days=10), log_frame)
    investigator = Investigator(wh, catalog, graph, log_frame, report, replay=replay)
    results = []
    for ev in events.iter_rows(named=True):
        if on_event:
            on_event(ev["event_id"])
        current = Period(ev["start_date"], ev["start_date"] + timedelta(days=6))
        baseline = current.same_weekdays_before()
        v = investigator.verdict(metric, current, baseline)
        reported, gap = true_data_gap(wh, metric.measure_column, current, baseline)
        sds = [f.estimate.sd for f in v.data_findings if f.estimate.sd]
        band = None
        if sds and len(sds) == len(v.data_findings):
            spread = 2 * sum(sd**2 for sd in sds) ** 0.5
            band = (v.data_impact - spread, v.data_impact + spread)
        results.append(
            VerdictResult(
                ev["event_id"],
                ev["nature"],
                ev["kind"],
                str(current),
                reported,
                gap,
                v.data_impact,
                band,
                len(v.data_findings),
            )
        )
    return results
