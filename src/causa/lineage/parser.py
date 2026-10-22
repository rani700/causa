"""Turns one SQL statement (Exasol dialect) into its data-flow facts.

Besides "which columns derive which", the parser records **gating columns**: columns used in JOIN
conditions, WHERE, GROUP BY or HAVING. They decide *which rows survive*, so a null spike or a type
change in a gating column is how rows silently disappear downstream. Most lineage tools ignore them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from causa.sources.catalog import Catalog
from causa.sqlutil import command_name

log = logging.getLogger(__name__)
# A production log is full of statements sqlglot models only as opaque commands (users, grants, ...).
logging.getLogger("sqlglot").setLevel(logging.ERROR)
DIALECT = "exasol"

_IMPORT = re.compile(
    r"^\s*IMPORT\s+INTO\s+(?P<table>[\w.\"]+)(?:\s*\((?P<cols>[^)]*)\))?.*?\bFROM\s+(?P<source>.*)$", re.I | re.S
)
_FILE = re.compile(r"\b(?:FILE|TABLE|STATEMENT)\s+'([^']*)'", re.I)


@dataclass(frozen=True, order=True)
class ColumnRef:
    table: str
    column: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass
class ParsedStatement:
    command: str
    target: str | None = None
    target_kind: str | None = None  # "table" | "view"
    sources: set[str] = field(default_factory=set)
    derivations: dict[str, set[ColumnRef]] = field(default_factory=dict)  # target column -> source columns
    gating: set[ColumnRef] = field(default_factory=set)
    join_pairs: set[tuple[ColumnRef, ColumnRef]] = field(default_factory=set)  # equi-join keys, sorted pairs
    reads: set[ColumnRef] = field(default_factory=set)
    external_source: str | None = None
    ddl: str | None = None  # schema change applied to target (ALTER TABLE ...)
    error: str | None = None

    @property
    def writes(self) -> bool:
        return self.target is not None


def table_name(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts).upper()


def parse_statement(sql: str, catalog: Catalog) -> ParsedStatement:
    """Parse one logged statement. Never raises: a production log holds statements of every kind
    (sessions, grants, schema DDL, client tools' metadata probes), and one odd statement must not
    stop lineage for the rest."""
    command = command_name(sql)
    if command == "IMPORT":
        return _parse_import(sql)
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except SqlglotError as exc:
        return ParsedStatement(command, error=f"parse: {exc}"[:500])
    try:
        return _parse_tree(command, tree, catalog)
    except Exception as exc:  # see docstring
        return ParsedStatement(command, error=f"analyse: {type(exc).__name__}: {exc}"[:500])


def _is_relation(tree: exp.Expression) -> bool:
    """CREATE/DROP/ALTER of a table or view (not of a schema, user, role, function, ...)."""
    kind = (tree.args.get("kind") or "TABLE").upper()
    return kind in ("TABLE", "VIEW") and isinstance(_relation(tree), exp.Table | exp.Schema)


def _relation(tree: exp.Expression) -> exp.Expression | None:
    tables = tree.args.get("tables")  # DROP keeps its object list here
    return tables[0] if tables else tree.this


def _parse_tree(command: str, tree: exp.Expression, catalog: Catalog) -> ParsedStatement:

    stmt = ParsedStatement(command)
    select: exp.Expression | None = None
    target_columns: list[str] | None = None

    match tree:
        case exp.Insert():
            target = tree.this
            if isinstance(target, exp.Schema):
                target_columns = [c.name.upper() for c in target.expressions]
                target = target.this
            stmt.target, stmt.target_kind = table_name(target), "table"
            target_columns = target_columns or catalog.columns(stmt.target) or None
            select = tree.expression
        case exp.Create() if _is_relation(tree):
            kind = (tree.args.get("kind") or "TABLE").upper()
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            stmt.target, stmt.target_kind = table_name(target), "view" if kind == "VIEW" else "table"
            select = tree.expression
        case exp.Alter() if _is_relation(tree):
            stmt.target, stmt.target_kind = table_name(tree.this), "table"
            stmt.ddl = tree.sql(dialect=DIALECT)
        case exp.TruncateTable():
            stmt.target, stmt.target_kind = table_name(tree.expressions[0]), "table"
        case exp.Drop() if _is_relation(tree):
            stmt.target, stmt.ddl = table_name(_relation(tree)), "DROP"
        case exp.Delete() | exp.Update() | exp.Merge():
            stmt.target, stmt.target_kind = table_name(tree.this), "table"
            select = tree
        case exp.Query():
            select = tree
        case _:
            return stmt

    if select is not None and isinstance(select, exp.Expression):
        _analyse_query(stmt, select, catalog, target_columns)
    return stmt


def _parse_import(sql: str) -> ParsedStatement:
    m = _IMPORT.match(sql)
    if not m:
        return ParsedStatement("IMPORT", error="unrecognised IMPORT statement")
    file_match = _FILE.search(m["source"])
    return ParsedStatement(
        "IMPORT",
        target=m["table"].replace('"', "").upper(),
        target_kind="table",
        external_source=file_match.group(1) if file_match else m["source"].strip()[:200],
    )


def _analyse_query(
    stmt: ParsedStatement, query: exp.Expression, catalog: Catalog, target_columns: list[str] | None
) -> None:
    schema = catalog.mapping
    try:
        qualified = qualify(query.copy(), schema=schema, dialect=DIALECT, validate_qualify_columns=False)
    except SqlglotError as exc:
        stmt.error = f"qualify: {exc}"[:500]
        qualified = query

    for scope in traverse_scope(qualified):
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and table_name(source) != stmt.target:
                stmt.sources.add(table_name(source))
        for join in scope.expression.args.get("joins") or []:
            for eq in (join.args.get("on") or exp.true()).find_all(exp.EQ):
                left, right = eq.this, eq.expression
                if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                    a, b = scope.sources.get(left.table), scope.sources.get(right.table)
                    if isinstance(a, exp.Table) and isinstance(b, exp.Table):
                        pair = sorted(
                            (
                                ColumnRef(table_name(a), left.name.upper()),
                                ColumnRef(table_name(b), right.name.upper()),
                            )
                        )
                        stmt.join_pairs.add((pair[0], pair[1]))
        for column in scope.columns:
            source = scope.sources.get(column.table)
            if not isinstance(source, exp.Table):
                continue
            ref = ColumnRef(table_name(source), column.name.upper())
            stmt.reads.add(ref)
            if column.find_ancestor(exp.Where, exp.Join, exp.Group, exp.Having) is not None:
                stmt.gating.add(ref)

    if stmt.target is None or not isinstance(qualified, exp.Query):
        return
    selects = qualified.selects
    for i, projection in enumerate(selects):
        out_name = projection.alias_or_name.upper()
        target_col = target_columns[i] if target_columns and i < len(target_columns) else out_name
        stmt.derivations[target_col] = _column_sources(out_name, qualified, projection, schema)


def _column_sources(
    name: str, query: exp.Query, projection: exp.Expression, schema: dict[str, dict[str, dict[str, str]]]
) -> set[ColumnRef]:
    try:
        root = lineage(name, query, schema=schema, dialect=DIALECT)
        return {
            ColumnRef(table_name(node.source), node.name.rsplit(".", 1)[-1].strip('"').upper())
            for node in root.walk()
            if not node.downstream and isinstance(node.source, exp.Table) and node.source.name
        }
    except (SqlglotError, KeyError, ValueError) as exc:
        log.debug("column lineage failed for %s: %s", name, exc)
        return {ColumnRef("?", c.name.upper()) for c in projection.find_all(exp.Column)}
