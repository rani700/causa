"""Scores Causa against the simulator's ground truth (``SIM.EVENTS``).

This is the only module allowed to read the SIM schema. For the health engine the questions are:
* recall: is every data incident raised as an *alerting* case (severity high or critical)?
* calm: do real business changes stay below alerting severity?
* precision: how many alerting cases match no data incident at all?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl

from causa.health import HealthReport, Severity
from causa.health.models import HealthCase
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
