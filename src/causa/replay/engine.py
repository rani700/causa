"""Engine 5a: counterfactual replay.

What would the metric have shown if the incident had not happened? Causa answers by re-running the
company's *own* pipeline, not by modelling it:

1. infer a repair for the damaged table from the health evidence (``repairs.py``);
2. copy the repaired rows of the affected load window into a sandbox schema;
3. take the exact INSERT/MERGE statements the query log recorded for the affected load days on
   every table between the damaged one and the metric's table, and re-run them twice with their
   table names rewritten into the sandbox: a **control** run on the original data and a **repaired**
   run. The control run must reproduce the logged row counts, which proves the replay is faithful;
4. the metric computed on the repaired run minus the control run is the incident's exact effect.
   Differencing cancels anything the replay does not model.

Nothing outside the CAUSA_REPLAY schema is written.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl
import sqlglot
from sqlglot import exp

from causa.health import HealthReport, Signal
from causa.health.models import HealthCase, HealthIncident
from causa.lineage import LineageGraph
from causa.lineage.parser import DIALECT, ColumnRef, parse_statement, table_name
from causa.metrics.attribution import Period, _quote
from causa.metrics.definitions import MetricDefinition
from causa.replay.repairs import (
    CoalesceRenamed,
    Dedupe,
    Interpolate,
    QuarantineKey,
    Repair,
    quarantine_dimension,
    sentinel_for,
)
from causa.sources.catalog import Catalog
from causa.sqlutil import command_name, dt
from causa.verdict.linkage import all_incidents
from causa.warehouse import Warehouse

SANDBOX = "CAUSA_REPLAY"
_ADD_COLUMN = re.compile(r"ADD\s+(?:COLUMN\s+)?\"?(\w+)\"?", re.I)
_INCREMENTAL = {"INSERT", "MERGE"}
_FULL_REFRESH = {"CREATE TABLE AS", "TRUNCATE", "DELETE", "UPDATE"}


@dataclass
class ReplayPlan:
    case: HealthCase
    repair: Repair
    load_column: str
    load_days: list[date]
    path: list[str]  # tables rebuilt, upstream first; ends with the metric's table
    statements: pl.DataFrame  # sql_text, start_time, row_count, target


@dataclass
class ReplayResult:
    plan: ReplayPlan
    daily_diff: dict[date, float]  # metric: repaired minus control, by business date
    fidelity: list[tuple[str, int | None, int]]  # (target, logged rows, control rows)
    notes: list[str] = field(default_factory=list)

    @property
    def faithful(self) -> bool:
        return all(logged is None or logged == control for _, logged, control in self.fidelity)

    def impact(self, current: Period, baseline: Period) -> float:
        """Effect of the incident on the change current - baseline (observed minus counterfactual)."""

        def total(p: Period) -> float:
            return sum(v for d, v in self.daily_diff.items() if p.start <= d <= p.end)

        return -(total(current) - total(baseline))


class Replayer:
    def __init__(
        self,
        wh: Warehouse,
        catalog: Catalog,
        graph: LineageGraph,
        query_log: pl.DataFrame,
        report: HealthReport,
    ) -> None:
        self._wh = wh
        self._catalog = catalog
        self._graph = graph
        self._log = query_log
        self._report = report
        self._parsed: dict[str, str | None] = {}

    # -- planning --------------------------------------------------------------------------------

    def plan(self, case: HealthCase, metric: MetricDefinition) -> ReplayPlan | str:
        """A plan, or the reason the case cannot be replayed."""
        repair = self.infer_repair(case)
        if isinstance(repair, str):
            return repair
        monitor = next((m for m in self._report.monitors if m.table == repair.table), None)
        if monitor is None:
            return f"{repair.table} is not monitored, so its load window is unknown"
        downstream = self._graph.downstream_tables(repair.table)
        if metric.table not in downstream:
            return f"{metric.table} is not downstream of {repair.table}"
        feeding = set(self._graph.upstream_tables(metric.table))
        path = [t for t in downstream if t == metric.table or t in feeding]
        path.sort(key=lambda t: len(self._graph.upstream_tables(t)))

        days = sorted({d for i in all_incidents(case) if i.table == repair.table for d in (i.days or [i.start])})
        writes = self._writes(days, set(path))
        refreshed = writes.filter(pl.col("command").is_in(list(_FULL_REFRESH)))
        if refreshed.height:
            return f"{refreshed['target'][0]} is fully rebuilt by its pipeline; only incremental loads are replayed"
        incremental = writes.filter(pl.col("command").is_in(list(_INCREMENTAL)))
        if incremental.is_empty():
            return "no statements on the affected load days wrote the tables between incident and metric"
        return ReplayPlan(case, repair, monitor.load_column, days, path, incremental)

    def _writes(self, days: list[date], targets: set[str]) -> pl.DataFrame:
        log = self._log.filter(pl.col("success") & pl.col("start_time").dt.date().is_in(days)).sort(
            "start_time", "stmt_id"
        )
        rows = []
        for r in log.iter_rows(named=True):
            command = command_name(r["sql_text"])
            if command not in _INCREMENTAL | _FULL_REFRESH:
                continue
            if r["sql_text"] not in self._parsed:
                self._parsed[r["sql_text"]] = parse_statement(r["sql_text"], self._catalog).target
            target = self._parsed[r["sql_text"]]
            if target in targets:
                rows.append({**r, "command": command, "target": target})
        return pl.DataFrame(rows) if rows else pl.DataFrame(schema={"command": pl.String, "target": pl.String})

    def infer_repair(self, case: HealthCase) -> Repair | str:
        incidents = all_incidents(case)
        depth = {i.table: len(self._graph.upstream_tables(i.table)) for i in incidents}
        by_depth = sorted(incidents, key=lambda i: depth[i.table])
        monitors = {m.table: m for m in self._report.monitors}

        for i in by_depth:
            if i.signal is Signal.DUPLICATE_RATE and monitors.get(i.table) and monitors[i.table].key_columns:
                m = monitors[i.table]
                return Dedupe(
                    "dedupe",
                    i.table,
                    f"kept the first load of each ({', '.join(m.key_columns)}) in {i.table}",
                    keys=m.key_columns,
                    load_column=m.load_column,
                )
        for i in by_depth:
            if i.signal is Signal.NULL_RATE and i.column:
                renamed = self._renamed_to(i)
                if renamed:
                    return CoalesceRenamed(
                        "renamed column",
                        i.table,
                        f"{i.table}.{i.column} stopped being filled when {renamed} was added; "
                        f"used COALESCE({i.column}, {renamed})",
                        column=i.column,
                        replacement=renamed,
                    )
                partners = self._graph.join_partners(ColumnRef(i.table, i.column))
                if partners:
                    dim = partners[0]
                    schema, name = dim.table.split(".")
                    columns = self._catalog.mapping[schema][name]
                    own_type = self._catalog.mapping[i.table.split(".")[0]][i.table.split(".")[1]][i.column]
                    return QuarantineKey(
                        "quarantine missing keys",
                        i.table,
                        f"rows with NULL {i.column} kept under an '(unknown)' member of {dim.table}",
                        aux=[quarantine_dimension(dim.table, dim.column, columns)],
                        column=i.column,
                        sentinel=sentinel_for(own_type),
                    )
        for i in by_depth:
            if i.signal is Signal.VALUE_SHIFT and not i.evidence.get("mix_sensitive") and i.column and i.segment:
                m = monitors.get(i.table)
                if m and m.event_column and m.segment_column:
                    replacements = self._interpolate(i, m.event_column, m.segment_column)
                    if replacements:
                        return Interpolate(
                            "interpolate reference value",
                            i.table,
                            f"{i.column} for {m.segment_column}={i.segment} interpolated between the last good "
                            f"value before and the first good value after",
                            value_column=i.column,
                            segment_column=m.segment_column,
                            segment_value=i.segment,
                            event_column=m.event_column,
                            replacements=replacements,
                        )
        signals = sorted({i.signal.value for i in incidents})
        return f"no automatic repair for {', '.join(signals)}"

    def _renamed_to(self, incident: HealthIncident) -> str | None:
        """A column added right before ``incident.column`` went NULL, holding the missing values."""
        monitor = next((m for m in self._report.monitors if m.table == incident.table), None)
        if monitor is None:
            return None
        for change in self._graph.schema_changes:
            if change.table != incident.table or not change.at.date() <= incident.start <= change.at.date() + timedelta(
                days=1
            ):
                continue
            for added in _ADD_COLUMN.findall(change.ddl.split(incident.table, 1)[-1]):
                added = added.upper()
                if added == incident.column or added not in self._catalog.columns(incident.table):
                    continue
                window = _window(monitor.load_column, incident.days or [incident.start])
                row = self._wh.query(
                    f"""SELECT COUNT(*) AS NULLS_, SUM(CASE WHEN {added} IS NOT NULL THEN 1 ELSE 0 END) AS FILLED
                    FROM {incident.table} WHERE {incident.column} IS NULL AND {window}"""
                ).row(0, named=True)
                if row["nulls_"] and (row["filled"] or 0) >= 0.9 * row["nulls_"]:
                    return added
        return None

    def _interpolate(self, incident: HealthIncident, event: str, segment: str) -> dict[date, float]:
        lag = 1
        bad = sorted({d - timedelta(days=lag) for d in (incident.days or [incident.start])})
        first, last = bad[0] - timedelta(days=14), bad[-1] + timedelta(days=14)
        daily = self._wh.query(
            f"""SELECT CAST({event} AS DATE) AS D, AVG(CAST({incident.column} AS DOUBLE)) AS V FROM {incident.table}
            WHERE {segment} = '{_quote(incident.segment or "")}' AND {event} BETWEEN {dt(first)} AND {dt(last)}
            GROUP BY CAST({event} AS DATE)"""
        )
        good = {d: v for d, v in daily.iter_rows() if d not in bad and v is not None}
        before = [d for d in good if d < bad[0]]
        after = [d for d in good if d > bad[-1]]
        if not before:
            return {}
        b = max(before)
        a = min(after) if after else None
        out = {}
        for d in bad:
            if a is None:
                out[d] = good[b]
            else:
                w = (d - b).days / (a - b).days
                out[d] = good[b] + w * (good[a] - good[b])
        return out

    # -- running ---------------------------------------------------------------------------------

    def run(self, plan: ReplayPlan, metric: MetricDefinition, keep: bool = False) -> ReplayResult:
        run_id = hashlib.sha1(f"{plan.case.case_id}{plan.load_days}".encode()).hexdigest()[:6].upper()

        def sandbox(kind: str, table: str) -> str:
            return f"{SANDBOX}.{kind}{run_id}_{table.replace('.', '__')}"

        created: list[str] = []

        def create(name: str, select: str) -> None:
            self._wh.execute(f"DROP TABLE IF EXISTS {name}")
            self._wh.execute(f"CREATE TABLE {name} AS {select}")
            created.append(name)

        self._wh.execute(f"CREATE SCHEMA IF NOT EXISTS {SANDBOX}")
        try:
            repaired_map: dict[str, str] = {}
            control_map: dict[str, str] = {}
            columns = self._catalog.columns(plan.repair.table)
            fixed = sandbox("F", plan.repair.table)
            create(fixed, plan.repair.select(columns, _window(plan.load_column, plan.load_days)))
            repaired_map[plan.repair.table] = fixed
            for aux in plan.repair.aux:
                name = sandbox("A", aux.original)
                create(name, aux.select)
                repaired_map[aux.original] = name
            for table in plan.path:
                for kind, mapping in (("R", repaired_map), ("C", control_map)):
                    name = sandbox(kind, table)
                    create(name, f"SELECT * FROM {table} WHERE 1 = 0")
                    mapping[table] = name

            fidelity = []
            for r in plan.statements.iter_rows(named=True):
                control_rows = self._wh.execute(rewrite(r["sql_text"], control_map))
                self._wh.execute(rewrite(r["sql_text"], repaired_map))
                logged = int(r["row_count"]) if r["row_count"] is not None else None
                fidelity.append((r["target"], logged, control_rows))

            diff = self._daily(metric, repaired_map[metric.table])
            for d, v in self._daily(metric, control_map[metric.table]).items():
                diff[d] = diff.get(d, 0.0) - v
            return ReplayResult(plan, {d: v for d, v in diff.items() if abs(v) > 1e-9}, fidelity)
        finally:
            if not keep:
                for name in reversed(created):
                    self._wh.execute(f"DROP TABLE IF EXISTS {name}")

    def _daily(self, metric: MetricDefinition, table: str) -> dict[date, float]:
        where = f"WHERE {metric.filter}" if metric.filter else ""
        frame = self._wh.query(
            f"SELECT {metric.time_column} AS D, SUM({metric.measure_column}) AS V FROM {table} {where} "
            f"GROUP BY {metric.time_column}"
        )
        return {d: float(v or 0.0) for d, v in frame.iter_rows()}


def rewrite(sql: str, mapping: dict[str, str]) -> str:
    """The statement with every table in ``mapping`` replaced (aliases kept)."""

    def swap(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and table_name(node) in mapping:
            schema, name = mapping[table_name(node)].split(".")
            return exp.Table(this=exp.to_identifier(name), db=exp.to_identifier(schema), alias=node.args.get("alias"))
        return node

    return sqlglot.parse_one(sql, read=DIALECT).transform(swap).sql(dialect=DIALECT)


def _window(load_column: str, days: list[date]) -> str:
    return f"CAST({load_column} AS DATE) IN ({', '.join(dt(d) for d in days)})"
