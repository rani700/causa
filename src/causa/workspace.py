"""One place that loads (and caches) what every engine needs: query log, catalog, lineage, config."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property

import polars as pl

from causa.health import HealthReport, HealthScanner
from causa.lineage import LineageGraph, build_lineage
from causa.metrics.definitions import CausaConfig, load_config
from causa.sources.catalog import Catalog
from causa.sources.query_log import ExasolAuditLog, QueryLogSource, TableQueryLog
from causa.verdict import Investigator
from causa.warehouse import Warehouse


@dataclass
class Workspace:
    wh: Warehouse
    config_path: str = "causa.yaml"
    query_log_table: str = "OPS.QUERY_LOG"  # or "audit" for Exasol's own audit log
    _health: HealthReport | None = field(default=None, repr=False)
    # Counterfactual replays by incident and metric, shared by every investigation in this workspace.
    _replays: dict[tuple, tuple] = field(default_factory=dict, repr=False)

    @cached_property
    def config(self) -> CausaConfig:
        return load_config(self.config_path)

    @cached_property
    def source(self) -> QueryLogSource:
        return (
            ExasolAuditLog(self.wh) if self.query_log_table == "audit" else TableQueryLog(self.wh, self.query_log_table)
        )

    @cached_property
    def query_log(self) -> pl.DataFrame:
        return self.source.fetch()

    @cached_property
    def catalog(self) -> Catalog:
        return Catalog.load(self.wh)

    @cached_property
    def graph(self) -> LineageGraph:
        return build_lineage(self.query_log, self.catalog)

    @property
    def today(self) -> date:
        """The latest day the query log has seen (the simulator's 'now'; wall-clock date in production)."""
        latest = self.query_log["start_time"].max()
        return latest.date() if latest else date.today()  # type: ignore[union-attr]

    def health(self, since: date, until: date) -> HealthReport:
        # Nothing is loaded after today: asking past it must not force a rescan of everything before.
        until = min(until, self.today)
        since = min(since, until)
        h = self._health
        if h is None or h.since > since or h.until < until:
            # Scan generously so that follow-up questions about nearby days reuse the result.
            start = min(since, h.since) if h else since - timedelta(days=14)
            end = max(until, h.until) if h else until
            h = HealthScanner(self.wh, self.catalog, self.graph).scan(start, end, self.query_log)
            self._health = h
        return h

    def investigator(self, replay: bool = True) -> Investigator:
        return Investigator(
            self.wh, self.catalog, self.graph, self.query_log, self._health, replay=replay, replays=self._replays
        )
