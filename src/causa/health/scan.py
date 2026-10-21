"""Engine 2 entry point: scan the warehouse for data health incidents over a date window."""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import polars as pl

from causa.health.cases import build_cases
from causa.health.detectors import Sensitivity, detect, to_incidents
from causa.health.discovery import MonitorDiscovery
from causa.health.models import HealthCase, HealthIncident, Signal, TableMonitor
from causa.health.profiles import SERIES_SCHEMA, conservation_series, profile_table
from causa.lineage import LineageGraph
from causa.lineage.graph import EdgeKind
from causa.lineage.parser import ColumnRef, parse_statement
from causa.sources.catalog import Catalog
from causa.sqlutil import command_name, ts
from causa.warehouse import Warehouse

log = logging.getLogger(__name__)
MAX_CATEGORIES = 200
_WRITE_COMMANDS = {"INSERT", "MERGE", "UPDATE", "DELETE", "CREATE TABLE AS", "IMPORT", "TRUNCATE"}


@dataclass
class HealthReport:
    since: date
    until: date
    monitors: list[TableMonitor]
    incidents: list[HealthIncident]
    series: pl.DataFrame = field(repr=False)
    cases: list[HealthCase] = field(default_factory=list)

    def by_signal(self, signal: Signal) -> list[HealthIncident]:
        return [i for i in self.incidents if i.signal is signal]

    def to_frame(self) -> pl.DataFrame:
        return pl.DataFrame(
            [
                {
                    "incident_key": i.key,
                    "signal": i.signal.value,
                    "severity": i.severity.value,
                    "table_name": i.table,
                    "column_name": i.column,
                    "segment_column": i.segment_column,
                    "segment": i.segment,
                    "start_date": i.start,
                    "end_date": i.end,
                    "peak_score": i.peak_score,
                    "observed": i.observed,
                    "expected": i.expected,
                    "summary": i.summary,
                }
                for i in self.incidents
            ],
            schema_overrides={
                "end_date": pl.Date,
                "column_name": pl.String,
                "segment": pl.String,
                "segment_column": pl.String,
            },
        )


class HealthScanner:
    def __init__(
        self,
        wh: Warehouse,
        catalog: Catalog,
        graph: LineageGraph,
        sensitivity: Sensitivity | None = None,
        monitors: list[TableMonitor] | None = None,
    ) -> None:
        self._wh = wh
        self._catalog = catalog
        self._graph = graph
        self._sensitivity = sensitivity or Sensitivity()
        self._monitors = monitors

    def monitors(self, since: date, until: date) -> list[TableMonitor]:
        if self._monitors is None:
            discovery = MonitorDiscovery(self._wh, self._catalog, self._graph)
            self._monitors = discovery.discover(_start(since), _start(until + timedelta(days=1)))
        return self._monitors

    def scan(self, since: date, until: date, query_log: pl.DataFrame | None = None) -> HealthReport:
        s = self._sensitivity
        history_start = since - timedelta(days=max(s.lookback_days, s.volume_lookback_days))
        monitors = self.monitors(history_start, until)
        frames = [profile_table(self._wh, m, history_start, until) for m in monitors]
        series = pl.concat(frames) if frames else pl.DataFrame(schema=SERIES_SCHEMA)
        edges = [
            (self._graph.g.nodes[u]["name"], self._graph.g.nodes[v]["name"])
            for u, v, d in self._graph.g.edges(data=True)
            if d["kind"] == EdgeKind.FEEDS
        ]
        series = pl.concat([series, conservation_series(series, edges, monitors)])

        anomalies = detect(series, since, until, self._sensitivity)
        segment_columns = {m.table: m.segment_column for m in monitors}
        incidents = fold_propagated(to_incidents(anomalies, segment_columns), self._graph)
        incidents += self._new_values(monitors, since, until)
        incidents += self._staleness(series, until, segment_columns)
        incidents += self._schema_changes(since, until)
        if query_log is not None:
            incidents += self._failed_writes(query_log, since, until)
        reference_tables = {m.table for m in monitors if m.segment_column and m.segment_column in m.key_columns}
        for incident in incidents:
            if incident.signal is Signal.VALUE_SHIFT and incident.table not in reference_tables:
                incident.evidence["mix_sensitive"] = True
        incidents.sort(key=lambda i: (i.start, -abs(i.peak_score)))
        return HealthReport(since, until, monitors, incidents, series, build_cases(incidents, self._graph))

    def _new_values(self, monitors: list[TableMonitor], since: date, until: date) -> list[HealthIncident]:
        """Category values never seen before the window: a new market, currency or status code.
        Usually a business change rather than a data incident, which is exactly why it is evidence."""
        out = []
        for m in monitors:
            schema, name = m.table.split(".")
            types = self._catalog.mapping[schema][name]
            columns = [
                c
                for c in dict.fromkeys([m.segment_column, *m.null_columns])
                if c and c not in m.key_columns and types.get(c, "").upper().startswith(("VARCHAR", "CHAR"))
            ]
            end = ts(_start(until + timedelta(days=1)))
            for column in columns:
                firsts = self._wh.query(
                    f"""SELECT {column} AS VAL, MIN(CAST({m.load_column} AS DATE)) AS FIRST_DAY, COUNT(*) AS N
                    FROM {m.table} WHERE {m.load_column} < {end} AND {column} IS NOT NULL
                    GROUP BY {column}"""
                )
                if firsts.height > MAX_CATEGORIES or firsts.is_empty():
                    continue
                if firsts["first_day"].min() >= since:  # no history before the window to compare against
                    continue
                for row in firsts.filter(pl.col("first_day") >= since).iter_rows(named=True):
                    out.append(
                        HealthIncident(
                            signal=Signal.NEW_VALUE,
                            table=m.table,
                            column=column,
                            segment=str(row["val"]),
                            segment_column=column,
                            start=row["first_day"],
                            end=row["first_day"],
                            peak_score=6.0,
                            observed=float(row["n"]),
                            expected=0.0,
                            summary=f"{m.table}.{column}: new value '{row['val']}' first seen {row['first_day']} "
                            f"({row['n']:,} rows since)",
                            days=[row["first_day"]],
                        )
                    )
        return fold_propagated(out, self._graph)

    # -- query-log based checks ------------------------------------------------------------------

    def _schema_changes(self, since: date, until: date) -> list[HealthIncident]:
        out = []
        for change in self._graph.schema_changes:
            if not since <= change.at.date() <= until:
                continue
            downstream = self._graph.downstream_tables(change.table)
            out.append(
                HealthIncident(
                    signal=Signal.SCHEMA_CHANGE,
                    table=change.table,
                    column=None,
                    segment=None,
                    segment_column=None,
                    start=change.at.date(),
                    end=change.at.date(),
                    peak_score=10.0,
                    observed=float(len(downstream)),
                    expected=0.0,
                    summary=f"{change.ddl} (by {change.actor} at {change.at:%Y-%m-%d %H:%M}); "
                    f"{len(downstream)} downstream tables",
                    days=[change.at.date()],
                    evidence={"actor": change.actor, "ddl": change.ddl, "at": change.at, "downstream": downstream},
                )
            )
        return out

    def _failed_writes(self, query_log: pl.DataFrame, since: date, until: date) -> list[HealthIncident]:
        window = query_log.filter(pl.col("start_time").dt.date().is_between(since, until) | ~pl.col("success"))
        failed = window.filter(~pl.col("success") & pl.col("start_time").dt.date().is_between(since, until))
        out = []
        for row in failed.iter_rows(named=True):
            if command_name(row["sql_text"]) not in _WRITE_COMMANDS:
                continue
            target = parse_statement(row["sql_text"], self._catalog).target or "?"
            retry = query_log.filter(
                pl.col("success") & (pl.col("sql_text") == row["sql_text"]) & (pl.col("start_time") > row["start_time"])
            ).sort("start_time")
            recovered = retry["start_time"][0] if retry.height else None
            note = (
                f"re-run succeeded {recovered:%Y-%m-%d %H:%M} by {retry['client'][0]}" if recovered else "never re-run"
            )
            out.append(
                HealthIncident(
                    signal=Signal.FAILED_WRITE,
                    table=target,
                    column=None,
                    segment=None,
                    segment_column=None,
                    start=row["start_time"].date(),
                    end=recovered.date() if recovered else None,
                    peak_score=10.0,
                    observed=1.0,
                    expected=0.0,
                    summary=f"write to {target} failed at {row['start_time']:%Y-%m-%d %H:%M} "
                    f"({row['client']}): {(row['error_text'] or '')[:120]}; {note}",
                    days=[row["start_time"].date()],
                    evidence={"client": row["client"], "error": row["error_text"], "recovered_at": recovered},
                )
            )
        return out

    def _staleness(
        self, series: pl.DataFrame, until: date, segment_columns: dict[str, str | None]
    ) -> list[HealthIncident]:
        """Segments that normally load every day but have been silent up to ``until``."""
        volume = series.filter((pl.col("signal") == Signal.VOLUME.value) & (pl.col("n") > 0))
        out = []
        for (table, segment), g in volume.group_by(["table", "segment"]):
            days = g["day"].sort().to_list()
            if len(days) < Sensitivity().min_history:
                continue
            gaps = [(b - a).days for a, b in itertools.pairwise(days)]
            cadence = sorted(gaps)[len(gaps) // 2] if gaps else 1
            silent = (until - days[-1]).days
            if silent > max(2, 2 * cadence):
                out.append(
                    HealthIncident(
                        signal=Signal.STALENESS,
                        table=table,  # type: ignore[arg-type]
                        column=None,
                        segment=None if segment == "*" else segment,  # type: ignore[arg-type]
                        segment_column=segment_columns.get(table),  # type: ignore[arg-type]
                        start=days[-1] + timedelta(days=1),
                        end=None,
                        peak_score=float(silent) / cadence,
                        observed=float(silent),
                        expected=float(cadence),
                        summary=f"{table} [{segment}]: no data for {silent} days (usually every {cadence} day(s))",
                        days=[],
                    )
                )
        return out


def fold_propagated(incidents: list[HealthIncident], graph: LineageGraph) -> list[HealthIncident]:
    """Folds an incident into an upstream incident that explains it: same signal and direction,
    overlapping days, the upstream table feeds the downstream one, and (for column signals) the
    downstream column is computed from the upstream column. Returns the root incidents."""
    depth = {i.table: len(graph.upstream_tables(i.table)) for i in incidents}
    roots: list[HealthIncident] = []
    for incident in sorted(incidents, key=lambda i: (depth[i.table], i.start)):
        root = next((r for r in roots if _explains(r, incident, graph)), None)
        if root:
            root.propagated.append(incident)
        else:
            roots.append(incident)
    return roots


def _explains(root: HealthIncident, incident: HealthIncident, graph: LineageGraph) -> bool:
    if root.signal is not incident.signal or root.table == incident.table:
        return False
    if (root.peak_score > 0) != (incident.peak_score > 0) or not set(root.days) & set(incident.days):
        return False
    if root.table not in graph.upstream_tables(incident.table):
        return False
    if root.segment_column == incident.segment_column and root.segment != incident.segment:
        return False
    if root.column and incident.column and root.column != incident.column:
        if root.signal is Signal.ROW_LOSS:
            return False  # different pipeline steps
        sources = graph.column_sources(incident.table, incident.column)
        if ColumnRef(root.table, root.column) not in sources:
            return False
    return True


def _start(d: date) -> datetime:
    return datetime.combine(d, time())
