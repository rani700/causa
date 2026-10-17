"""Builds the lineage graph from a query log.

Statements are grouped by fingerprint first, so each distinct template is parsed exactly once no
matter how many times a job ran. That keeps the cost proportional to the number of distinct
statements, not the size of the log.
"""

from __future__ import annotations

import polars as pl

from causa.lineage.actors import parse_client
from causa.lineage.graph import LineageGraph
from causa.lineage.parser import ParsedStatement, parse_statement
from causa.sources.catalog import Catalog
from causa.sqlutil import fingerprint_expr


def build_lineage(log: pl.DataFrame, catalog: Catalog) -> LineageGraph:
    ok = log.filter(pl.col("success")).with_columns(
        fingerprint=fingerprint_expr(pl.col("sql_text")),
        day=pl.col("start_time").dt.date(),
    )
    templates = ok.group_by("fingerprint").agg(pl.col("sql_text").first())
    parsed: dict[str, ParsedStatement] = {fp: parse_statement(sql, catalog) for fp, sql in templates.iter_rows()}

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
        key = (row["client"], row["user_name"])
        if key not in actors:
            actors[key] = parse_client(*key)
        graph.add_statement(
            parsed[row["fingerprint"]], actors[key], row["n"], row["first"], row["last"], row["duration"] or 0.0
        )
    graph.parse_errors = {fp: p.error for fp, p in parsed.items() if p.error}
    return graph
