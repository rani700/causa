"""Data health: what is monitored and what was found."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Signal(StrEnum):
    VOLUME = "volume"  # rows landed per day
    NULL_RATE = "null_rate"  # share of NULLs in a column that matters downstream
    DUPLICATE_RATE = "duplicate_rate"  # share of rows repeating the (inferred) key
    VALUE_SHIFT = "value_shift"  # mean of a numeric column moved abruptly
    LATENESS = "lateness"  # load time minus event time grew
    NEW_VALUE = "new_value"  # a category never seen before (new market, new currency, new code)
    ROW_LOSS = "row_loss"  # a pipeline step keeps a different share of rows than usual
    STALENESS = "staleness"  # no data for longer than the usual cadence
    SCHEMA_CHANGE = "schema_change"
    FAILED_WRITE = "failed_write"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class TableMonitor:
    """How one table is profiled. Discovered automatically; every field can be overridden."""

    table: str
    load_column: str
    segment_column: str | None = None
    event_column: str | None = None
    key_columns: list[str] = field(default_factory=list)
    null_columns: list[str] = field(default_factory=list)
    value_columns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Anomaly:
    """One anomalous observation (one day of one series)."""

    signal: Signal
    table: str
    column: str | None
    segment: str | None
    day: date
    observed: float
    expected: float
    score: float  # robust z-score; sign = direction


@dataclass
class HealthIncident:
    """Consecutive anomalies of one series, merged into an episode."""

    signal: Signal
    table: str
    column: str | None
    segment: str | None
    segment_column: str | None
    start: date
    end: date | None
    peak_score: float
    observed: float  # at the peak
    expected: float  # at the peak
    summary: str
    days: list[date] = field(default_factory=list)
    evidence: dict[str, object] = field(default_factory=dict)
    # The same anomaly observed further down the pipeline, folded into this (root) incident.
    propagated: list[HealthIncident] = field(default_factory=list)

    @property
    def direction(self) -> str:
        return "up" if self.peak_score > 0 else "down"

    @property
    def severity(self) -> Severity:
        if self.signal in (Signal.SCHEMA_CHANGE, Signal.FAILED_WRITE):
            return Severity.HIGH
        score = abs(self.peak_score)
        if score >= 25:
            severity = Severity.CRITICAL
        elif score >= 12:
            severity = Severity.HIGH
        else:
            severity = Severity.MEDIUM if score >= 7 else Severity.LOW
        if self.evidence.get("mix_sensitive") and severity in (Severity.CRITICAL, Severity.HIGH):
            return Severity.MEDIUM  # a mean over a mixed population moves whenever the mix moves
        return severity

    @property
    def key(self) -> str:
        parts = [self.signal.value, self.table, self.column or "", self.segment or "", f"{self.start}"]
        return "|".join(parts)


@dataclass
class HealthCase:
    """Root incidents that belong together: linked by lineage, overlapping in time and segment."""

    case_id: str
    primary: HealthIncident
    incidents: list[HealthIncident]

    @property
    def start(self) -> date:
        return min(i.start for i in self.incidents)

    @property
    def end(self) -> date:
        return max((i.end or i.start) for i in self.incidents)

    @property
    def tables(self) -> list[str]:
        names = {i.table for i in self.incidents} | {p.table for i in self.incidents for p in i.propagated}
        return sorted(names)

    @property
    def severity(self) -> Severity:
        order = list(Severity)
        return min((i.severity for i in self.incidents), key=order.index)

    @property
    def signals(self) -> set[Signal]:
        return {i.signal for i in self.incidents}
