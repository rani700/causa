"""Groups root incidents into cases: the unit a human (or the classifier) reasons about.

Two incidents are linked when either
* they are the same signal on the same column of the same table, overlap in time and are both at
  least medium severity (one problem spreading across segments, e.g. every feed), or
* they *start* within ``window`` days of each other (symptoms of one cause begin together; long
  episodes must not absorb unrelated later events), their segments are compatible, and they are
  related: same table, one table feeds the other, or the same signal for the same segment value
  (e.g. orders and order lines of one feed both arriving late).
"""

from __future__ import annotations

from datetime import timedelta

import networkx as nx

from causa.health.models import HealthCase, HealthIncident, Severity, Signal
from causa.lineage import LineageGraph

# Which incident best explains a case, most telling first. Integrity signals outrank symptoms.
_PRIMARY_ORDER = (
    Signal.SCHEMA_CHANGE,
    Signal.FAILED_WRITE,
    Signal.NULL_RATE,
    Signal.DUPLICATE_RATE,
    Signal.LATENESS,
    Signal.VALUE_SHIFT,
    Signal.STALENESS,
    Signal.ROW_LOSS,
    Signal.VOLUME,
    Signal.NEW_VALUE,
)


def _rank(i: HealthIncident, depth: dict[str, int]) -> tuple[int, int, int, float]:
    mixed = int(bool(i.evidence.get("mix_sensitive")))
    return (mixed, _PRIMARY_ORDER.index(i.signal), depth[i.table], -abs(i.peak_score))


def build_cases(incidents: list[HealthIncident], graph: LineageGraph, window: int = 2) -> list[HealthCase]:
    depth = {i.table: len(graph.upstream_tables(i.table)) for i in incidents}
    related = {
        i.table: {i.table, *graph.upstream_tables(i.table), *graph.downstream_tables(i.table)} for i in incidents
    }
    links: nx.Graph[int] = nx.Graph()
    links.add_nodes_from(range(len(incidents)))
    for a, x in enumerate(incidents):
        for b in range(a + 1, len(incidents)):
            y = incidents[b]
            spreads = Severity.LOW not in (x.severity, y.severity)  # noise must not chain into a case
            if (
                spreads
                and x.table == y.table
                and x.signal is y.signal
                and x.column == y.column
                and _overlap(x, y, window)
            ):
                links.add_edge(a, b)  # one problem spreading across segments (e.g. every feed)
                continue
            if abs((x.start - y.start).days) > window or not _compatible_segments(x, y):
                continue
            same_segment_signal = x.signal is y.signal and x.segment is not None and x.segment == y.segment
            if y.table in related[x.table] or same_segment_signal:
                links.add_edge(a, b)

    cases = []
    for component in nx.connected_components(links):
        members = sorted((incidents[k] for k in component), key=lambda i: (i.start, depth[i.table]))
        primary = min(members, key=lambda i: _rank(i, depth))
        cases.append(HealthCase("", primary, members))
    cases.sort(key=lambda c: (c.start, c.primary.table))
    for n, case in enumerate(cases, 1):
        case.case_id = f"HC-{case.start:%Y%m%d}-{n:02d}"
    return cases


def _overlap(x: HealthIncident, y: HealthIncident, window: int) -> bool:
    gap = timedelta(days=window)
    return x.start - gap <= (y.end or y.start) and y.start - gap <= (x.end or x.start)


def _compatible_segments(x: HealthIncident, y: HealthIncident) -> bool:
    if x.segment is None or y.segment is None:
        return True
    if x.segment_column == y.segment_column:
        return x.segment == y.segment
    return True  # different segmentations (feed vs region) cannot be compared directly
