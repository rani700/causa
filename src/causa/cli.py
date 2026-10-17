"""``causa`` command-line interface."""

from __future__ import annotations

import time
from datetime import date
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from causa.lineage import LineageGraph, build_lineage
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import default_scenario
from causa.sources.catalog import Catalog
from causa.sources.query_log import ExasolAuditLog, QueryLogSource, TableQueryLog
from causa.warehouse import Warehouse, connect

app = typer.Typer(no_args_is_help=True, help="Causa: root-cause engine for business metrics.")
sim_app = typer.Typer(no_args_is_help=True, help="Synthetic company simulator (benchmark + demo data).")
lineage_app = typer.Typer(no_args_is_help=True, help="Lineage reconstructed from the query log.")
app.add_typer(sim_app, name="sim")
app.add_typer(lineage_app, name="lineage")
console = Console()


@sim_app.command("scenario")
def show_scenario(
    start: Annotated[str, typer.Option(help="First simulated day (ISO date).")] = "2026-05-04",
    days: int = 168,
) -> None:
    """List the business events and data incidents in the default scenario."""
    scenario = default_scenario(date.fromisoformat(start), days)
    table = Table(title=f"{scenario.name}: {scenario.start} -> {scenario.end}")
    for col in ("id", "nature", "kind", "start", "end", "description"):
        table.add_column(col)
    for ev in sorted(scenario.events, key=lambda e: e.start):
        style = "red" if ev.nature == "data" else "green"
        table.add_row(
            ev.event_id, f"[{style}]{ev.nature}[/]", ev.kind, str(ev.start), str(ev.end or ""), ev.description
        )
    console.print(table)


@sim_app.command("run")
def run(
    start: Annotated[str, typer.Option(help="First simulated day (ISO date).")] = "2026-05-04",
    days: int = 168,
    seed: int = 7,
) -> None:
    """Build the simulated company's warehouse (drops and recreates its schemas)."""
    scenario = default_scenario(date.fromisoformat(start), days, seed)
    t0 = time.perf_counter()
    with connect(client_name="causa-simulator") as wh, Progress(console=console) as progress:
        task = progress.add_task(f"Simulating on {wh.name}", total=scenario.days)
        result = run_simulation(scenario, wh, on_day=lambda _: progress.advance(task))

    gap = result.reported_net_revenue - result.true_net_revenue
    console.print(
        f"\n[bold]{result.days} days[/] simulated in {time.perf_counter() - t0:.1f}s\n"
        f"  true net revenue      EUR {result.true_net_revenue:,.0f}\n"
        f"  reported net revenue  EUR {result.reported_net_revenue:,.0f}  "
        f"([yellow]{gap:+,.0f}[/] from data incidents)\n"
        f"  query log statements  {result.query_log_rows:,}\n"
        f"  pipeline task runs    {result.pipeline_runs:,} ({len(result.failed_tasks)} failed)"
    )


QueryLogOption = Annotated[
    str, typer.Option("--query-log", help="'audit' for Exasol's audit log, or a SCHEMA.TABLE holding a query log.")
]


def _load_lineage(wh: Warehouse, query_log: str) -> LineageGraph:
    source: QueryLogSource = ExasolAuditLog(wh) if query_log == "audit" else TableQueryLog(wh, query_log)
    with console.status("Reconstructing lineage from the query log"):
        return build_lineage(source.fetch(), Catalog.load(wh))


@lineage_app.command("trace")
def trace(
    table: str,
    column: Annotated[str | None, typer.Option(help="Trace one column to its source columns.")] = None,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
) -> None:
    """Show where a table (or column) comes from and which columns gate its rows."""
    table = table.upper()
    with connect() as wh:
        graph = _load_lineage(wh, query_log)
    console.print(f"[bold]{table}[/] is built from: {', '.join(graph.upstream_tables(table)) or '-'}")
    if column:
        sources = graph.column_sources(table, column.upper())
        console.print(f"[bold]{column.upper()}[/] is computed from: {', '.join(map(str, sources)) or '-'}")
    for gated, columns in graph.gating_columns(table).items():
        console.print(f"  rows of {gated} are gated by {', '.join(sorted({str(c) for c in columns}))}")


@lineage_app.command("impact")
def impact(
    table: str,
    since: Annotated[str | None, typer.Option(help="Only count reads from this date (ISO).")] = None,
    until: Annotated[str | None, typer.Option(help="Only count reads up to this date (ISO).")] = None,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
) -> None:
    """Blast radius: everything downstream of TABLE and every actor that used it."""
    with connect() as wh:
        graph = _load_lineage(wh, query_log)
    result = graph.impact(
        table.upper(),
        date.fromisoformat(since) if since else None,
        date.fromisoformat(until) if until else None,
    )
    console.print(f"[bold]{len(result.tables) - 1}[/] downstream tables: {', '.join(result.tables[1:])}")
    out = Table(title=f"Actors affected by {result.origin}")
    for col in ("kind", "name", "reads", "last read", "via", "rebuilt"):
        out.add_column(col)
    for a in sorted(result.actors, key=lambda a: (a.kind, -a.reads)):
        last = f"{a.last_read:%Y-%m-%d %H:%M}" if a.last_read else ""
        out.add_row(a.kind, a.name, str(a.reads), last, ", ".join(a.via), ", ".join(a.writes))
    console.print(out)


if __name__ == "__main__":
    app()
