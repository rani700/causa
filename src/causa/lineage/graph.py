"""The lineage graph: how data moves from connectors through tables to the people and systems that use it.

Node ids
  ``table:SCHEMA.NAME``            tables and views
  ``column:SCHEMA.NAME.COLUMN``    columns
  ``actor:<key>``                  connectors, pipelines, dashboards, reports, analysts

Edges (attribute ``kind``)
  loads    actor  -> table    connector bulk-loads a table
  feeds    table  -> table    a statement writes the target from the source
  reads    table  -> actor    an actor queries the table
  derives  column -> column   a target column is computed from a source column
  gates    column -> table    a column decides which rows reach the target (JOIN / WHERE / GROUP BY)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

import networkx as nx
import polars as pl

from causa.lineage.actors import Actor, ActorKind
from causa.lineage.parser import ColumnRef, ParsedStatement


class EdgeKind(StrEnum):
    LOADS = "loads"
    FEEDS = "feeds"
    READS = "reads"
    DERIVES = "derives"
    GATES = "gates"


@dataclass
class Activity:
    """Aggregated statement activity along an edge."""

    statements: int = 0
    duration_s: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    actors: set[str] = field(default_factory=set)
    daily: Counter[date] = field(default_factory=Counter)
    daily_last: dict[date, datetime] = field(default_factory=dict)

    def add(self, n: int, first: datetime, last: datetime, duration: float, actor: str) -> None:
        self.statements += n
        self.duration_s += duration
        self.first_seen = first if self.first_seen is None else min(self.first_seen, first)
        self.last_seen = last if self.last_seen is None else max(self.last_seen, last)
        self.actors.add(actor)
        self.daily[first.date()] += n
        day_last = self.daily_last.get(first.date())
        self.daily_last[first.date()] = last if day_last is None else max(day_last, last)

    def _days(self, since: date | None, until: date | None) -> list[date]:
        return [d for d in self.daily if (since is None or d >= since) and (until is None or d <= until)]

    def in_window(self, since: date | None, until: date | None) -> int:
        return sum(self.daily[d] for d in self._days(since, until))

    def last_in_window(self, since: date | None, until: date | None) -> datetime | None:
        return max((self.daily_last[d] for d in self._days(since, until)), default=None)


@dataclass(frozen=True)
class SchemaChange:
    table: str
    at: datetime
    actor: str
    ddl: str


@dataclass(frozen=True)
class ImpactedActor:
    actor: str
    kind: ActorKind
    name: str
    via: tuple[str, ...]  # tables through which the actor is affected
    reads: int  # reads in the window (0 for writers)
    last_read: datetime | None
    writes: tuple[str, ...] = ()  # downstream tables this actor (re)built from affected data


@dataclass
class Impact:
    origin: str
    tables: list[str]
    actors: list[ImpactedActor]

    def of_kind(self, *kinds: ActorKind) -> list[ImpactedActor]:
        return [a for a in self.actors if a.kind in kinds]


def table_id(name: str) -> str:
    return f"table:{name}"


def column_id(ref: ColumnRef) -> str:
    return f"column:{ref.table}.{ref.column}"


class LineageGraph:
    def __init__(self) -> None:
        self.g = nx.DiGraph()
        self.schema_changes: list[SchemaChange] = []
        self.parse_errors: dict[str, str] = {}

    # -- construction --------------------------------------------------------------------------

    def _table(self, name: str, kind: str | None = None) -> str:
        node = table_id(name)
        if node not in self.g:
            self.g.add_node(node, type="table", name=name, object_type=kind or "table", writers=set())
        elif kind == "view":
            self.g.nodes[node]["object_type"] = "view"
        return node

    def _actor(self, actor: Actor) -> str:
        if actor.node_id not in self.g:
            self.g.add_node(
                actor.node_id, type="actor", name=actor.name, kind=actor.kind, tool=actor.tool, key=actor.key
            )
        return actor.node_id

    def _column(self, ref: ColumnRef) -> str:
        node = column_id(ref)
        if node not in self.g:
            self.g.add_node(node, type="column", table=ref.table, name=ref.column)
        return node

    def _edge(self, u: str, v: str, kind: EdgeKind) -> Activity:
        if not self.g.has_edge(u, v):
            self.g.add_edge(u, v, kind=kind, activity=Activity())
        return self.g.edges[u, v]["activity"]

    def add_statement(
        self, stmt: ParsedStatement, actor: Actor, n: int, first: datetime, last: datetime, duration: float
    ) -> None:
        actor_node = self._actor(actor)
        activity = (n, first, last, duration, actor.key)

        if stmt.target is None:
            for source in stmt.sources:
                self._edge(self._table(source), actor_node, EdgeKind.READS).add(*activity)
            return

        target = self._table(stmt.target, stmt.target_kind)
        self.g.nodes[target]["writers"].add(actor.key)
        if stmt.ddl:
            self.schema_changes.append(SchemaChange(stmt.target, first, actor.key, stmt.ddl))
        if stmt.command == "IMPORT":
            self._edge(actor_node, target, EdgeKind.LOADS).add(*activity)
        for source in stmt.sources:
            self._edge(self._table(source), target, EdgeKind.FEEDS).add(*activity)
        for target_col, sources in stmt.derivations.items():
            tgt = self._column(ColumnRef(stmt.target, target_col))
            for src in sources:
                if src.table != "?":
                    self._edge(self._column(src), tgt, EdgeKind.DERIVES).add(*activity)
        for gate in stmt.gating:
            if gate.table != stmt.target:
                self._edge(self._column(gate), target, EdgeKind.GATES).add(*activity)

    # -- queries -------------------------------------------------------------------------------

    def _successors(self, node: str, kind: EdgeKind) -> Iterable[str]:
        return (v for v in self.g.successors(node) if self.g.edges[node, v]["kind"] == kind)

    def _predecessors(self, node: str, kind: EdgeKind) -> Iterable[str]:
        return (u for u in self.g.predecessors(node) if self.g.edges[u, node]["kind"] == kind)

    def _walk(self, start: str, step) -> list[str]:  # type: ignore[no-untyped-def]
        seen: list[str] = []
        stack = [start]
        while stack:
            for nxt in step(stack.pop()):
                if nxt not in seen and nxt != start:
                    seen.append(nxt)
                    stack.append(nxt)
        return seen

    def tables(self) -> list[str]:
        return sorted(d["name"] for _, d in self.g.nodes(data=True) if d["type"] == "table")

    def actors(self, *kinds: ActorKind) -> list[dict[str, object]]:
        return [
            {"node": n, **d}
            for n, d in self.g.nodes(data=True)
            if d["type"] == "actor" and (not kinds or d["kind"] in kinds)
        ]

    def downstream_tables(self, table: str) -> list[str]:
        nodes = self._walk(table_id(table), lambda n: self._successors(n, EdgeKind.FEEDS))
        return [self.g.nodes[n]["name"] for n in nodes]

    def upstream_tables(self, table: str) -> list[str]:
        nodes = self._walk(table_id(table), lambda n: self._predecessors(n, EdgeKind.FEEDS))
        return [self.g.nodes[n]["name"] for n in nodes]

    def loaders(self, table: str) -> list[str]:
        return [self.g.nodes[a]["key"] for a in self._predecessors(table_id(table), EdgeKind.LOADS)]

    def column_sources(self, table: str, column: str) -> list[ColumnRef]:
        """Leaf columns (usually in RAW) a column is ultimately computed from."""
        start = column_id(ColumnRef(table, column))
        if start not in self.g:
            return []
        upstream = self._walk(start, lambda n: self._predecessors(n, EdgeKind.DERIVES))
        leaves = [n for n in upstream if not any(True for _ in self._predecessors(n, EdgeKind.DERIVES))]
        return sorted(ColumnRef(self.g.nodes[n]["table"], self.g.nodes[n]["name"]) for n in leaves)

    def gating_columns(self, table: str) -> dict[str, list[ColumnRef]]:
        """For ``table`` and everything upstream of it: the columns that decide which rows survive."""
        out: dict[str, list[ColumnRef]] = {}
        for name in [table, *self.upstream_tables(table)]:
            gates = sorted(
                ColumnRef(self.g.nodes[c]["table"], self.g.nodes[c]["name"])
                for c in self._predecessors(table_id(name), EdgeKind.GATES)
            )
            if gates:
                out[name] = gates
        return out

    def impact(self, table: str, since: date | None = None, until: date | None = None) -> Impact:
        """Blast radius of bad data in ``table``: every downstream table, and every actor that read
        affected data (within the window) or rebuilt something from it."""
        affected = [table, *self.downstream_tables(table)]
        reads: dict[str, list[str]] = {}
        writes: dict[str, list[str]] = {}
        for name in affected:
            node = table_id(name)
            for actor_node in self._successors(node, EdgeKind.READS):
                if self.g.edges[node, actor_node]["activity"].in_window(since, until):
                    reads.setdefault(actor_node, []).append(name)
            for target in self._successors(node, EdgeKind.FEEDS):
                activity: Activity = self.g.edges[node, target]["activity"]
                if activity.in_window(since, until):
                    for key in activity.actors:
                        writes.setdefault(f"actor:{key}", []).append(self.g.nodes[target]["name"])

        actors = []
        for actor_node in sorted(set(reads) | set(writes)):
            data = self.g.nodes[actor_node]
            via = tuple(sorted(set(reads.get(actor_node, []))))
            activities = [self.g.edges[table_id(t), actor_node]["activity"] for t in via]
            last = max((t for a in activities if (t := a.last_in_window(since, until))), default=None)
            actors.append(
                ImpactedActor(
                    actor=data["key"],
                    kind=data["kind"],
                    name=data["name"],
                    via=via,
                    reads=sum(a.in_window(since, until) for a in activities),
                    last_read=last,
                    writes=tuple(sorted(set(writes.get(actor_node, [])))),
                )
            )
        return Impact(origin=table, tables=affected, actors=actors)

    # -- export --------------------------------------------------------------------------------

    def edges_frame(self) -> pl.DataFrame:
        rows = []
        for u, v, d in self.g.edges(data=True):
            a: Activity = d["activity"]
            rows.append(
                {
                    "source": u,
                    "target": v,
                    "kind": str(d["kind"]),
                    "statements": a.statements,
                    "duration_s": a.duration_s,
                    "first_seen": a.first_seen,
                    "last_seen": a.last_seen,
                    "actors": ",".join(sorted(a.actors)),
                }
            )
        return pl.DataFrame(rows)
