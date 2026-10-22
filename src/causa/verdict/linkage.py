"""Connects a health case to the rows of a metric.

A case is *relevant* to a metric when one of its tables feeds the metric's table. To know *which*
metric rows it touches, Causa looks for a **bridge**: the case's root table, or any table between it
and the metric's table, that carries the affected segment column (e.g. ``SOURCE_FEED``, ``CURRENCY``)
and whose key columns also exist in the metric's table. Metric rows are then linked to the segment
with ``LEFT JOIN (SELECT DISTINCT keys FROM bridge WHERE segment IN (...))``; DISTINCT keeps a
duplicated bridge from fanning out the metric.
"""

from __future__ import annotations

from dataclasses import dataclass

from causa.health.models import HealthCase, HealthIncident, TableMonitor
from causa.lineage import LineageGraph
from causa.metrics.attribution import BLANK, Filters, _quote
from causa.metrics.definitions import MetricDefinition
from causa.sources.catalog import Catalog

_ID_SUFFIXES = ("_ID", "_NO", "_KEY", "_NUMBER")


@dataclass(frozen=True)
class Subset:
    """A slice of the metric's rows, expressed as SQL against alias ``m`` (and ``l`` for a bridge)."""

    label: str
    predicate: str  # boolean SQL, true for rows in the subset
    join: str = ""  # optional LEFT JOIN providing alias ``l``

    @property
    def everything(self) -> bool:
        return self.predicate == "1 = 1"


EVERYTHING = Subset("all rows", "1 = 1")


@dataclass(frozen=True)
class CaseLink:
    case: HealthCase
    tables: frozenset[str]  # every table the case touched (roots and downstream echoes)
    subset: Subset  # metric rows the case affects; EVERYTHING when no bridge was found
    segment_column: str | None
    segment_values: tuple[str, ...]
    bridge: str | None


def all_incidents(case: HealthCase) -> list[HealthIncident]:
    return [x for i in case.incidents for x in (i, *i.propagated)]


def relevant(case: HealthCase, metric: MetricDefinition, graph: LineageGraph) -> bool:
    feeding = {metric.table, *graph.upstream_tables(metric.table)}
    return any(i.table in feeding for i in all_incidents(case))


def link_case(
    case: HealthCase,
    metric: MetricDefinition,
    graph: LineageGraph,
    catalog: Catalog,
    monitors: list[TableMonitor],
) -> CaseLink:
    incidents = all_incidents(case)
    tables = frozenset(i.table for i in incidents)
    segmented = [i for i in incidents if i.segment_column and i.segment]
    if not segmented:
        return CaseLink(case, tables, EVERYTHING, None, (), None)

    depth = {i.table: len(graph.upstream_tables(i.table)) for i in segmented}
    root = min(segmented, key=lambda i: (depth[i.table], -abs(i.peak_score)))
    values = tuple(
        sorted({i.segment for i in segmented if (i.table, i.segment_column) == (root.table, root.segment_column)})  # type: ignore[misc]
    )
    column = root.segment_column
    assert column is not None

    if root.table == metric.table:
        predicate = " OR ".join(f"m.{column} = '{_quote(v)}'" for v in values)
        return CaseLink(case, tables, Subset(_label(column, values), f"({predicate})"), column, values, root.table)

    feeding = set(graph.upstream_tables(metric.table))
    candidates = [root.table, *(t for t in graph.downstream_tables(root.table) if t in feeding)]
    metric_columns = set(catalog.columns(metric.table))
    keys_by_table = {m.table: m.key_columns for m in monitors}
    for bridge in candidates:
        columns = catalog.columns(bridge)
        if column not in columns:
            continue
        keys = keys_by_table.get(bridge) or [c for c in columns if c.endswith(_ID_SUFFIXES)]
        keys = [k for k in keys if k != column]
        if keys and all(k in metric_columns for k in keys):
            in_list = ", ".join(f"'{_quote(v)}'" for v in values)
            join = (
                f"LEFT JOIN (SELECT DISTINCT {', '.join(keys)} FROM {bridge} WHERE {column} IN ({in_list})) l "
                f"ON {' AND '.join(f'l.{k} = m.{k}' for k in keys)}"
            )
            subset = Subset(_label(column, values), f"l.{keys[0]} IS NOT NULL", join)
            return CaseLink(case, tables, subset, column, values, bridge)
    return CaseLink(case, tables, EVERYTHING, column, values, None)


def filters_subset(filters: Filters) -> Subset:
    """A metric segment from attribution (e.g. REGION=EMEA) as a subset."""
    if not filters:
        return EVERYTHING
    parts = [f"m.{d} IS NULL" if v == BLANK else f"CAST(m.{d} AS VARCHAR(200)) = '{_quote(v)}'" for d, v in filters]
    predicate = " AND ".join(parts)
    return Subset(" & ".join(f"{d}={v}" for d, v in filters), f"({predicate})")


def _label(column: str, values: tuple[str, ...]) -> str:
    shown = ", ".join(values[:4]) + (f" +{len(values) - 4}" if len(values) > 4 else "")
    return f"{column}={shown}"


def link_case_value(link: CaseLink, value: str) -> Subset:
    """The same bridge as ``link``, restricted to one segment value."""
    assert link.bridge is not None and link.segment_column is not None
    old = "IN (" + ", ".join(f"'{_quote(v)}'" for v in link.segment_values) + ")"
    new = f"IN ('{_quote(value)}')"
    return Subset(
        f"{link.segment_column}={value}", link.subset.predicate.replace(old, new), link.subset.join.replace(old, new)
    )
