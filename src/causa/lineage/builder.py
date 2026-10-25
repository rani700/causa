"""Builds the lineage graph from a query log.

Statements are grouped by fingerprint first, so each distinct template is parsed exactly once no
matter how many times a job ran. That keeps the cost proportional to the number of distinct
statements, not the size of the log.
"""

from __future__ import annotations

from dataclasses import replace

import polars as pl

from causa.lineage.actors import parse_client
from causa.lineage.graph import LineageGraph
from causa.lineage.parser import ColumnRef, ParsedStatement, parse_statement
from causa.sources.catalog import Catalog, is_company_table
from causa.sqlutil import fingerprint_expr


def build_lineage(log: pl.DataFrame, catalog: Catalog) -> LineageGraph:
    ok = log.filter(pl.col("success")).with_columns(
        fingerprint=fingerprint_expr(pl.col("sql_text")),
        day=pl.col("start_time").dt.date(),
    )
    templates = ok.group_by("fingerprint").agg(pl.col("sql_text").first())
    parsed: dict[str, ParsedStatement] = {fp: parse_statement(sql, catalog) for fp, sql in templates.iter_rows()}
    company = {fp: _company_part(p) for fp, p in parsed.items()}

    activity = (
        ok.group_by("fingerprint", "client", "user_name", "day")
        .agg(
            n=pl.len(),
            first=pl.col("start_time").min(),
            last=pl.col("start_time").max(),
            duration=pl.col("duration").cast(pl.Float64).sum(),
        )
        .sort("first")
    )

    graph = LineageGraph()
    actors = {}
    for row in activity.iter_rows(named=True):
        stmt = company[row["fingerprint"]]
        if stmt is None:
            continue
        key = (row["client"], row["user_name"])
        if key not in actors:
            actors[key] = parse_client(*key)
        graph.add_statement(stmt, actors[key], row["n"], row["first"], row["last"], row["duration"] or 0.0)
    graph.parse_errors = {fp: p.error for fp, p in parsed.items() if p.error}
    return graph


def _company_part(stmt: ParsedStatement) -> ParsedStatement | None:
    """The statement without system objects and Causa's own tables; None when nothing is left.

    On Exasol's audit log, monitoring tools read system views all day; those are not data flow."""
    if stmt.target and not is_company_table(stmt.target):
        return None
    sources = {s for s in stmt.sources if is_company_table(s)}
    if sources == stmt.sources:
        return stmt
    if not sources and not stmt.target:
        return None

    def keep(c: ColumnRef) -> bool:
        return is_company_table(c.table)

    return replace(
        stmt,
        sources=sources,
        derivations={k: {c for c in v if keep(c)} for k, v in stmt.derivations.items()},
        gating={c for c in stmt.gating if keep(c)},
        join_pairs={p for p in stmt.join_pairs if keep(p[0]) and keep(p[1])},
        reads={c for c in stmt.reads if keep(c)},
    )
