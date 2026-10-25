"""The investigator's tools: thin, JSON-returning wrappers around Causa's engines.

Every figure the agent may quote comes out of these functions. They never ask the model to compute
anything; they return what the engines measured, rounded, with the method that produced it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import sqlglot
from sqlglot import exp

from causa.health import Severity
from causa.metrics.attribution import Attributor, Driver, Explanation, Period, Segment
from causa.metrics.definitions import MetricDefinition
from causa.recall import recall, short_cause
from causa.verdict import Verdict
from causa.workspace import Workspace

log_ = logging.getLogger(__name__)

MAX_SQL_ROWS = 50
REFRESH_SECONDS = 300  # how often a long-running server checks the query log for new activity
VERDICT_CACHE = 32  # investigations remembered per toolbox (a week takes seconds on a remote warehouse)


def _r(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(float(x), digits)


def segment_dict(s: Segment) -> dict[str, Any]:
    effects = s.effects
    return {
        "segment": s.label,
        "current": _r(s.current),
        "baseline": _r(s.baseline),
        "change": _r(s.delta),
        "change_pct": _r(s.pct, 4),
        "volume_effect": _r(effects[0]) if effects else None,
        "rate_effect": _r(effects[1]) if effects else None,
        "volume_current": _r(s.volume_current, 0),
        "volume_baseline": _r(s.volume_baseline, 0),
        "vanished": s.vanished,
    }


def driver_dict(d: Driver) -> dict[str, Any]:
    return {
        **segment_dict(d.segment),
        "share_of_total_change": _r(d.share, 4),
        "uniform_inside": d.uniform,
        "drill_down": [driver_dict(c) for c in d.children],
        "offsets": [segment_dict(o) for o in d.offsets],
    }


def explanation_dict(e: Explanation) -> dict[str, Any]:
    return {
        "metric": e.metric.name,
        "unit": e.metric.unit,
        "volume_label": e.metric.volume_label,
        "current_period": str(e.current),
        "baseline_period": str(e.baseline),
        "total": segment_dict(e.total),
        "significance_z": _r(e.significance),
        "typical_change_pct": _r(e.typical_change, 4),
        "unusual": e.unusual,
        "drivers": [driver_dict(d) for d in e.drivers],
        "offsets": [segment_dict(o) for o in e.offsets],
        "queries_pushed_down": e.queries,
    }


def verdict_dict(v: Verdict) -> dict[str, Any]:
    total = v.explanation.total
    business_pct = v.business_change / total.baseline if total.baseline else None
    z = v.explanation.judge(business_pct) if business_pct is not None else None
    findings = []
    for f in v.findings:
        e = f.estimate
        findings.append(
            {
                "case_id": f.case.case_id,
                "counted_as_data_incident": f.is_data and f.duplicate_of is None,
                "duplicate_of": f.duplicate_of,
                "confidence": _r(f.confidence, 3),
                "cause": short_cause(f),
                "health_summary": f.case.primary.summary,
                "signals": sorted({i.signal.value for i in f.case.incidents}),
                "affected_rows": f.link.subset.label,
                "lands_in": [{"driver": o.driver, "share_of_affected_value": _r(o.coverage, 3)} for o in f.overlaps],
                "affected_current_days": [str(d) for d in e.current_days],
                "affected_baseline_days": [str(d) for d in e.baseline_days],
                "impact_on_change": _r(e.impact),
                "impact_band_95": [_r(e.low), _r(e.high)] if e.sd else None,
                "method": e.method,
                "earlier_estimate": _r(f.replaced.impact) if f.replaced else None,
                "not_replayed_because": f.replay_note,
                "notes": e.notes,
                "evidence": [{"reason": x.text, "log_odds": x.weight} for x in f.evidence],
            }
        )
    return {
        "headline": explanation_dict(v.explanation) | {"drivers": "see explain_metric"},
        "total_change": _r(total.delta),
        "data_incident_impact": _r(v.data_impact),
        "business_change": _r(v.business_change),
        "business_change_pct": _r(business_pct, 4),
        "business_change_z": _r(z),
        "findings": findings,
        "business_drivers_on_clean_days": [driver_dict(d) for d in (v.business.drivers if v.business else [])],
        "days_excluded_as_affected": [str(d) for d in v.excluded_days],
        "restatements": [
            {
                "date": str(r.metric_date),
                "segment": r.dim_value if r.dimension == "*" else f"{r.dimension}={r.dim_value}",
                "first_reported": _r(r.first_reported),
                "first_reported_at": f"{r.first_observed_at:%Y-%m-%d %H:%M}",
                "now": _r(r.latest),
            }
            for r in v.restatements
        ],
    }


class Toolbox:
    """Binds the tools to one workspace. ``call(name, input)`` returns a JSON string."""

    def __init__(self, ws: Workspace, lock: threading.RLock | None = None) -> None:
        self.ws = ws
        # One warehouse connection: engine work is serialised. Agents call tools through ``call``,
        # so the lock is held while a tool runs, never while the model is thinking.
        self.lock = lock or threading.RLock()
        self._checked = time.monotonic()
        # The workspace pins one snapshot of the query log, lineage and health, so a verdict for the
        # same question cannot change while it lives: judge, recall and the web UI share one run.
        self._verdicts: OrderedDict[tuple[str, Period, Period], Verdict] = OrderedDict()
        self._handlers: dict[str, Callable[..., Any]] = {
            "list_metrics": self.list_metrics,
            "explain_metric": self.explain_metric,
            "judge_change": self.judge_change,
            "health_cases": self.health_cases,
            "recall_consumers": self.recall_consumers,
            "lineage": self.lineage,
            "run_sql": self.run_sql,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"unknown tool {name!r}")
        with self.lock:
            self.keep_fresh()
            return json.dumps(handler(**arguments), default=str)

    # -- helpers -----------------------------------------------------------------------------

    def _metric(self, name: str) -> MetricDefinition:
        return self.ws.config.metric(name)

    def _periods(self, current: str, baseline: str | None) -> tuple[Period, Period]:
        cur = Period.parse(current)
        return cur, Period.parse(baseline) if baseline else cur.same_weekdays_before()

    def keep_fresh(self, every: float = REFRESH_SECONDS) -> bool:
        """At most every ``every`` seconds, reload if the warehouse has new activity (call under the lock)."""
        if self.ws.switched:  # failed over to the local copy, or back: rebuild on the active backend
            self.ws.refresh(force=True)
        elif time.monotonic() - self._checked < every:
            return False
        else:
            self._checked = time.monotonic()
            if not self.ws.refresh():
                return False
        self._verdicts.clear()
        return True

    def warm_up(self) -> int:
        """Load lineage and health for the whole history, then replay every data incident once, so
        the first visitors get cached answers. Takes the lock per step: requests interleave.
        Returns the number of investigations prepared."""
        with self.lock:
            log = self.ws.query_log
            if log.is_empty():
                return 0
            first, today = log["start_time"].min().date(), self.ws.today  # type: ignore[union-attr]
            report = self.ws.health(first, today)
            cases = [c for c in report.cases if c.severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)]
            metrics = [m.name for m in self.ws.config.metrics]
        done = 0
        for case in cases:
            end = min(case.start + timedelta(days=6), today)
            for metric in metrics:
                with self.lock:
                    try:
                        self._verdict(metric, f"{end - timedelta(days=6)}..{end}", None)
                        done += 1
                    except Exception:
                        log_.exception("warm-up of %s for %s failed", metric, case.case_id)
        return done

    def context(self) -> str:
        """Opens a conversation: the date and the exact metric names, so no turn is spent guessing."""
        with self.lock:
            names = ", ".join(m.name for m in self.ws.config.metrics)
            return f"(Today is {self.ws.today}. Metrics: {names}.)"

    # -- tools -------------------------------------------------------------------------------

    def list_metrics(self) -> dict[str, Any]:
        return {
            "today": str(self.ws.today),
            "metrics": [
                {
                    "name": m.name,
                    "label": m.label,
                    "unit": m.unit,
                    "table": m.table,
                    "dimensions": m.dimensions,
                    "deep_dimensions": m.deep_dimensions,
                    "volume": m.volume_label if m.volume_column else None,
                }
                for m in self.ws.config.metrics
            ],
        }

    def explain_metric(
        self, metric: str, current: str, baseline: str | None = None, where: list[str] | None = None
    ) -> dict[str, Any]:
        cur, base = self._periods(current, baseline)
        filters = tuple((k.upper(), v) for k, _, v in (w.partition("=") for w in where or []))
        return explanation_dict(Attributor(self.ws.wh).explain(self._metric(metric), cur, base, filters))

    def _verdict(self, metric: str, current: str, baseline: str | None) -> Verdict:
        cur, base = self._periods(current, baseline)
        definition = self._metric(metric)
        key = (definition.name, cur, base)
        if key in self._verdicts:
            self._verdicts.move_to_end(key)
            return self._verdicts[key]
        self.ws.health(base.start, cur.end + timedelta(days=3))
        verdict = self.ws.investigator(replay=True).verdict(definition, cur, base)
        self._verdicts[key] = verdict
        if len(self._verdicts) > VERDICT_CACHE:
            self._verdicts.popitem(last=False)
        return verdict

    def judge_change(self, metric: str, current: str, baseline: str | None = None) -> dict[str, Any]:
        return verdict_dict(self._verdict(metric, current, baseline))

    def investigate(self, metric: str, current: str, baseline: str | None = None) -> dict[str, Any]:
        """Verdict and recall from one engine run (not an agent tool: the web UI's main view)."""
        verdict = self._verdict(metric, current, baseline)
        return {"verdict": verdict_dict(verdict), **self._recalls(verdict)}

    def health_cases(self, since: str, until: str, min_severity: str = "medium") -> dict[str, Any]:
        start, end = date.fromisoformat(since), date.fromisoformat(until)
        order = list(Severity)
        floor = order.index(Severity(min_severity))
        report = self.ws.health(start, end)
        cases = [c for c in report.cases if order.index(c.severity) <= floor and start <= c.start <= end]
        return {
            "monitored_tables": [m.table for m in report.monitors],
            "cases": [
                {
                    "case_id": c.case_id,
                    "severity": c.severity.value,
                    "start": str(c.start),
                    "end": str(c.end),
                    "headline": f"{c.primary.signal.value}: {c.primary.summary}",
                    "other_incidents": [f"{i.signal.value}: {i.summary}" for i in c.incidents if i is not c.primary],
                    "tables": c.tables,
                }
                for c in cases
            ],
        }

    def recall_consumers(self, metric: str, current: str, baseline: str | None = None) -> dict[str, Any]:
        return self._recalls(self._verdict(metric, current, baseline))

    def _recalls(self, verdict: Verdict) -> dict[str, Any]:
        out = []
        for r in recall(verdict, self.ws.graph, self.ws.today):
            out.append(
                {
                    "case_id": r.finding.case.case_id,
                    "cause": short_cause(r.finding),
                    "origin_table": r.origin,
                    "exposure": [str(r.exposure_start), str(r.exposure_end) if r.exposure_end else "still open"],
                    "misstatement_total": _r(r.total_misstatement),
                    "misstatement_by_day": {str(d): _r(v) for d, v in sorted(r.misstatement.items())},
                    "consumers": [
                        {
                            "kind": i.actor.kind.value,
                            "name": i.actor.name,
                            "tool": i.tool,
                            "reads": i.actor.reads,
                            "active_days": i.actor.active_days,
                            "last_read": str(i.actor.last_read) if i.actor.last_read else None,
                            "via": list(i.actor.via),
                            "rebuilt": list(i.actor.writes),
                            "attributes": i.attributes,
                            "draft_notice": i.notice,
                        }
                        for i in r.items
                    ],
                }
            )
        return {"recalls": out}

    def lineage(self, table: str, column: str | None = None) -> dict[str, Any]:
        g = self.ws.graph
        table = table.upper()
        out: dict[str, Any] = {
            "table": table,
            "built_from": g.upstream_tables(table),
            "feeds": g.downstream_tables(table),
            "loaded_by": g.loaders(table),
            "row_gating_columns": {t: sorted({str(c) for c in cols}) for t, cols in g.gating_columns(table).items()},
            "schema_changes": [
                {"at": str(c.at), "by": c.actor, "ddl": c.ddl} for c in g.schema_changes if c.table == table
            ],
        }
        if column:
            out["column_sources"] = [str(c) for c in g.column_sources(table, column.upper())]
        return out

    def run_sql(self, sql: str) -> dict[str, Any]:
        """Read-only: a single SELECT, capped at MAX_SQL_ROWS rows."""
        try:
            statements = sqlglot.parse(sql, read="exasol")
        except sqlglot.errors.SqlglotError as exc:
            return {"error": f"could not parse: {exc}"}
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            return {"error": "only a single SELECT statement is allowed"}
        if any(t.db.upper() == "SIM" for t in statements[0].find_all(exp.Table)):
            return {"error": "the SIM schema is the simulator's ground truth and is off limits"}
        query = statements[0].limit(MAX_SQL_ROWS + 1).sql(dialect="exasol")
        frame = self.ws.wh.query(query)
        return {
            "columns": frame.columns,
            "rows": frame.head(MAX_SQL_ROWS).rows(),
            "truncated": frame.height > MAX_SQL_ROWS,
        }


_PERIOD = "ISO date range 'YYYY-MM-DD..YYYY-MM-DD' (inclusive) or a single ISO date"
_BASELINE = "Comparison period of the same length. Omit to compare with the same weekdays one week earlier."

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_metrics",
        "description": "List the metrics Causa can explain (name, unit, dimensions) and today's date. "
        "Call this first when the user does not name a metric exactly.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "explain_metric",
        "description": "Explain where a metric's change between two periods sits: total change, how unusual it is "
        "versus comparable past changes, and drivers drilled down by dimension, each split into a volume effect and a "
        "rate effect. Does NOT say whether the data is trustworthy; use judge_change for that.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "Metric name from list_metrics."},
                "current": {"type": "string", "description": _PERIOD},
                "baseline": {"type": ["string", "null"], "description": _BASELINE},
                "where": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Optional filters like ['REGION=EMEA'] to explain inside one segment.",
                },
            },
            "required": ["metric", "current", "baseline", "where"],
            "additionalProperties": False,
        },
    },
    {
        "name": "judge_change",
        "description": "Decide how much of a metric's change is caused by data incidents (broken pipelines, lost or "
        "duplicated rows, wrong reference data) and how much is real business change. Returns each data finding "
        "with its cause, exact impact (from re-running the pipeline on repaired data when possible), method, "
        "confidence and evidence, the business remainder with its drivers on unaffected days, and restatements "
        "(numbers that were reported wrong at the time and corrected since). "
        "This is the main tool for 'is this real?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "Metric name from list_metrics."},
                "current": {"type": "string", "description": _PERIOD},
                "baseline": {"type": ["string", "null"], "description": _BASELINE},
            },
            "required": ["metric", "current", "baseline"],
            "additionalProperties": False,
        },
    },
    {
        "name": "health_cases",
        "description": "List data health cases (schema changes, null spikes, duplicates, lost rows, failed loads, "
        "late data, wrong reference values, new category values) detected between two dates, regardless of metric.",
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "First day, ISO date."},
                "until": {"type": "string", "description": "Last day, ISO date."},
                "min_severity": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                    "description": "Hide cases below this severity.",
                },
            },
            "required": ["since", "until", "min_severity"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recall_consumers",
        "description": "For the data incidents affecting a metric in a period: who consumed the wrong numbers "
        "(dashboards, scheduled reports and recipients, ML pipelines, reverse-ETL syncs, spreadsheets, analysts), "
        "the exposure window, the misstatement per day, and a drafted correction notice for each consumer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "Metric name from list_metrics."},
                "current": {"type": "string", "description": _PERIOD},
                "baseline": {"type": ["string", "null"], "description": _BASELINE},
            },
            "required": ["metric", "current", "baseline"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lineage",
        "description": "Where a table (or one of its columns) comes from, what it feeds, who loads it, which columns "
        "decide which of its rows survive, and its recorded schema changes. Reconstructed from the query log.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "SCHEMA.TABLE"},
                "column": {"type": ["string", "null"], "description": "Optional column to trace to its sources."},
            },
            "required": ["table", "column"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_sql",
        "description": f"Run one read-only SELECT (Exasol SQL dialect) against the warehouse; at most {MAX_SQL_ROWS} "
        "rows are returned. Use only for facts the other tools do not provide.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
]
for _tool in TOOL_DEFINITIONS:
    _tool["strict"] = True
