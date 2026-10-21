"""``causa`` command-line interface."""

from __future__ import annotations

import time
from datetime import date
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from causa.evaluation import evaluate_health, load_events
from causa.health import HealthReport, HealthScanner, Severity
from causa.lineage import LineageGraph, build_lineage
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import default_scenario
from causa.sources.catalog import Catalog
from causa.sources.query_log import ExasolAuditLog, QueryLogSource, TableQueryLog
from causa.warehouse import Warehouse, connect

app = typer.Typer(no_args_is_help=True, help="Causa: root-cause engine for business metrics.")
sim_app = typer.Typer(no_args_is_help=True, help="Synthetic company simulator (benchmark + demo data).")
lineage_app = typer.Typer(no_args_is_help=True, help="Lineage reconstructed from the query log.")
health_app = typer.Typer(no_args_is_help=True, help="Data health incidents, focused by lineage.")
eval_app = typer.Typer(no_args_is_help=True, help="Score Causa against the simulator's ground truth.")
app.add_typer(sim_app, name="sim")
app.add_typer(lineage_app, name="lineage")
app.add_typer(health_app, name="health")
app.add_typer(eval_app, name="eval")
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


def _query_log(wh: Warehouse, query_log: str) -> QueryLogSource:
    return ExasolAuditLog(wh) if query_log == "audit" else TableQueryLog(wh, query_log)


def _load_lineage(wh: Warehouse, query_log: str) -> LineageGraph:
    with console.status("Reconstructing lineage from the query log"):
        return build_lineage(_query_log(wh, query_log).fetch(), Catalog.load(wh))


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


def _health_report(wh: Warehouse, since: date, until: date, query_log: str) -> HealthReport:
    log_frame = _query_log(wh, query_log).fetch()
    catalog = Catalog.load(wh)
    graph = build_lineage(log_frame, catalog)
    with console.status("Profiling monitored tables"):
        return HealthScanner(wh, catalog, graph).scan(since, until, log_frame)


_SEVERITY_STYLE = {Severity.CRITICAL: "bold red", Severity.HIGH: "red", Severity.MEDIUM: "yellow", Severity.LOW: "dim"}


@health_app.command("scan")
def health_scan(
    since: Annotated[str, typer.Option(help="First day to report (ISO).")],
    until: Annotated[str, typer.Option(help="Last day to report (ISO).")],
    min_severity: Annotated[Severity, typer.Option(help="Hide incidents below this severity.")] = Severity.LOW,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
) -> None:
    """Find data health incidents between SINCE and UNTIL."""
    order = list(Severity)
    with connect() as wh:
        report = _health_report(wh, date.fromisoformat(since), date.fromisoformat(until), query_log)
    shown = [c for c in report.cases if order.index(c.severity) <= order.index(min_severity)]
    echoes = sum(len(i.propagated) for i in report.incidents)
    console.print(
        f"Monitored {len(report.monitors)} tables: {len(report.incidents)} root incidents "
        f"({echoes} downstream echoes folded in), grouped into [bold]{len(report.cases)}[/] cases\n"
    )
    for case in shown:
        style = _SEVERITY_STYLE[case.severity]
        span = f"{case.start}" if case.end == case.start else f"{case.start} -> {case.end}"
        console.print(f"[{style}]{case.severity.value.upper():8}[/] [bold]{case.case_id}[/]  {span}")
        console.print(f"  [bold]{case.primary.signal.value}[/]: {case.primary.summary}")
        for i in case.incidents:
            if i is not case.primary:
                console.print(f"  [dim]+ {i.signal.value}: {i.summary}[/]")
        console.print(f"  [dim]tables: {', '.join(case.tables)}[/]\n")


@eval_app.command("health")
def eval_health(
    since: Annotated[str, typer.Option(help="First day to score (ISO).")] = "2026-06-01",
    until: Annotated[str, typer.Option(help="Last day to score (ISO).")] = "2026-10-17",
) -> None:
    """Did the health engine alert on every data incident, and stay calm on business changes?"""
    with connect() as wh:
        report = _health_report(wh, date.fromisoformat(since), date.fromisoformat(until), "OPS.QUERY_LOG")
        result = evaluate_health(report, load_events(wh))
    table = Table(title="Health engine vs ground truth")
    for col in ("event", "nature", "kind", "start", "verdict", "cases (severity: headline)"):
        table.add_column(col)
    for e in result.events:
        verdict = "[green]alerted[/]" if e.alerted else "[green]calm[/]" if e.nature == "business" else "[red]missed[/]"
        if e.nature == "business" and e.alerted:
            verdict = "[red]false alarm[/]"
        cases = "\n".join(f"{c.severity.value}: {c.primary.signal.value} {c.primary.table}" for c in e.cases)
        table.add_row(e.event_id, e.nature, e.kind, str(e.start), verdict, cases or "-")
    console.print(table)
    console.print(
        f"data-incident recall [bold]{result.recall:.0%}[/]   business calm [bold]{result.calm:.0%}[/]   "
        f"alert precision [bold]{result.precision:.0%}[/] ({len(result.false_alarms)} unmatched alerting cases)"
    )
    for c in result.false_alarms:
        console.print(f"  [yellow]unmatched[/] {c.case_id} {c.severity.value}: {c.primary.summary}")


if __name__ == "__main__":
    app()
