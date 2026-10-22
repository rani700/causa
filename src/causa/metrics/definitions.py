"""Metric definitions: the small semantic layer Causa reasons about.

Only additive measures (SUM) are decomposed exactly. An optional *volume* (e.g. distinct orders)
lets every change be split into a volume effect and a rate effect (value per unit of volume).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class VolumeAggregation(StrEnum):
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    COUNT = "count"


class MetricDefinition(BaseModel):
    name: str
    label: str
    table: str
    time_column: str
    measure_column: str  # summed
    volume_column: str | None = None
    volume_aggregation: VolumeAggregation = VolumeAggregation.COUNT_DISTINCT
    volume_label: str = "units"
    unit: str = ""
    dimensions: list[str]
    # High-cardinality dimensions (customers, products): explored only inside a driver.
    deep_dimensions: list[str] = Field(default_factory=list)
    filter: str | None = None  # extra SQL predicate in the Exasol dialect

    @model_validator(mode="after")
    def _upper(self) -> MetricDefinition:
        self.table = self.table.upper()
        self.time_column = self.time_column.upper()
        self.measure_column = self.measure_column.upper()
        self.volume_column = self.volume_column.upper() if self.volume_column else None
        self.dimensions = [d.upper() for d in self.dimensions]
        self.deep_dimensions = [d.upper() for d in self.deep_dimensions]
        return self

    @property
    def all_dimensions(self) -> list[str]:
        return [*self.dimensions, *self.deep_dimensions]


class CausaConfig(BaseModel):
    metrics: list[MetricDefinition] = Field(default_factory=list)

    def metric(self, name: str) -> MetricDefinition:
        for m in self.metrics:
            if m.name == name:
                return m
        known = ", ".join(m.name for m in self.metrics) or "none"
        raise KeyError(f"unknown metric {name!r} (known: {known})")


def load_config(path: Path | str = "causa.yaml") -> CausaConfig:
    with open(path) as fh:
        return CausaConfig.model_validate(yaml.safe_load(fh) or {})
