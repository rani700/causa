"""``causa`` command-line interface."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from causa.evaluation import VerdictResult, evaluate_health, load_events, true_data_gap
from causa.health import HealthReport, HealthScanner, Severity
from causa.lineage import LineageGraph, build_lineage
from causa.metrics.attribution import Attributor, Period
from causa.metrics.definitions import load_config
from causa.metrics.narrative import money, render
from causa.metrics.snapshots import TOTAL, MetricObserver, restatements
from causa.recall import recall as build_recall
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import default_scenario
from causa.sources.catalog import Catalog
from causa.sources.query_log import ExasolAuditLog, QueryLogSource, TableQueryLog
from causa.verdict import Investigator
from causa.verdict.narrative import render as render_verdict
from causa.warehouse import Warehouse, connect
from causa.workspace import Workspace

app = typer.Typer(no_args_is_help=True, help="Causa: root-cause engine for business metrics.")
sim_app = typer.Typer(no_args_is_help=True, help="Synthetic company simulator (benchmark + demo data).")
lineage_app = typer.Typer(no_args_is_help=True, help="Lineage reconstructed from the query log.")
health_app = typer.Typer(no_args_is_help=True, help="Data health incidents, focused by lineage.")
metrics_app = typer.Typer(no_args_is_help=True, help="Metric history: snapshots and restatements.")
eval_app = typer.Typer(no_args_is_help=True, help="Score Causa against the simulator's ground truth.")
app.add_typer(sim_app, name="sim")
app.add_typer(lineage_app, name="lineage")
app.add_typer(health_app, name="health")
app.add_typer(eval_app, name="eval")
app.add_typer(metrics_app, name="metrics")
console = Console()
ConfigOption = Annotated[str, typer.Option(help="Causa configuration file (metric definitions).")]


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
    observe: Annotated[bool, typer.Option(help="Record nightly metric snapshots, as Causa would.")] = True,
    config: ConfigOption = "causa.yaml",
) -> None:
    """Build the simulated company's warehouse (drops and recreates its schemas)."""
    scenario = default_scenario(date.fromisoformat(start), days, seed)
    t0 = time.perf_counter()
    with connect(client_name="causa-simulator") as wh, Progress(console=console) as progress:
        observer = None
        if observe:
            observer = MetricObserver(wh, load_config(config).metrics)
            observer.reset()
        task = progress.add_task(f"Simulating on {wh.name}", total=scenario.days)
        result = run_simulation(
            scenario,
            wh,
            on_day=lambda _: progress.advance(task),
            # Causa's nightly snapshot, taken after the ETL and before the business reads the numbers.
            on_loaded=(lambda d: observer.observe(datetime.combine(d + timedelta(days=1), dtime(3, 30)), d))
            if observer
            else None,
        )

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


@app.command()
def explain(
    metric: str,
    current: Annotated[str, typer.Option(help="Period to explain, e.g. 2026-10-06..2026-10-12.")],
    baseline: Annotated[
        str | None, typer.Option(help="Comparison period (default: same weekdays one week earlier).")
    ] = None,
    where: Annotated[list[str] | None, typer.Option(help="Restrict to DIMENSION=VALUE (repeatable).")] = None,
    config: ConfigOption = "causa.yaml",
) -> None:
    """Explain why METRIC changed: drivers, volume vs rate, and whether the change is unusual."""
    definition = load_config(config).metric(metric)
    filters = tuple((k.upper(), v) for k, _, v in (w.partition("=") for w in where or []))
    with connect() as wh, console.status("Drilling down"):
        explanation = Attributor(wh).explain(
            definition, Period.parse(current), Period.parse(baseline) if baseline else None, filters
        )
    for line in render(explanation):
        console.print(line, highlight=False)
    console.print(f"[dim]{explanation.queries} queries pushed down to the warehouse[/]")


@app.command()
def verdict(
    metric: str,
    current: Annotated[str, typer.Option(help="Period to judge, e.g. 2026-10-06..2026-10-12.")],
    baseline: Annotated[
        str | None, typer.Option(help="Comparison period (default: same weekdays one week earlier).")
    ] = None,
    replay: Annotated[bool, typer.Option(help="Re-run the pipeline on repaired data for exact impacts.")] = True,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
    config: ConfigOption = "causa.yaml",
) -> None:
    """Is the change in METRIC real, or broken data? Splits it, with evidence and confidence."""
    definition = load_config(config).metric(metric)
    with connect() as wh:
        log_frame = _query_log(wh, query_log).fetch()
        catalog = Catalog.load(wh)
        graph = build_lineage(log_frame, catalog)
        with console.status("Investigating"):
            result = Investigator(wh, catalog, graph, log_frame, replay=replay).verdict(
                definition, Period.parse(current), Period.parse(baseline) if baseline else None
            )
    for line in render_verdict(result):
        console.print(line, highlight=False, soft_wrap=True)


@app.command()
def recall(
    metric: str,
    current: Annotated[str, typer.Option(help="Period whose data incidents to recall, e.g. 2026-10-06..2026-10-12.")],
    out: Annotated[str | None, typer.Option(help="Also write the correction notices to this Markdown file.")] = None,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
    config: ConfigOption = "causa.yaml",
) -> None:
    """Who consumed the wrong numbers, how wrong they were, and a correction notice for each."""
    definition = load_config(config).metric(metric)
    with connect() as wh:
        log_frame = _query_log(wh, query_log).fetch()
        catalog = Catalog.load(wh)
        graph = build_lineage(log_frame, catalog)
        today = log_frame["start_time"].max().date()  # type: ignore[union-attr]
        with console.status("Investigating and replaying"):
            result = Investigator(wh, catalog, graph, log_frame, replay=True).verdict(definition, Period.parse(current))
    recalls = build_recall(result, graph, today)
    if not recalls:
        console.print("No data incidents in this period: nothing to recall.")
        return
    markdown = []
    for r in recalls:
        status = f"healed {r.exposure_end}" if r.exposure_end else "[red]still wrong in the warehouse[/]"
        console.print(
            f"\n[bold]{r.finding.case.case_id}[/] origin {r.origin}; exposure {r.exposure_start} -> "
            f"{r.exposure_end or today} ({status}); {definition.label} misstated by "
            f"{money(r.total_misstatement, definition.unit)} over {len(r.misstatement)} day(s)"
        )
        table = Table()
        for col in ("kind", "consumer", "reads", "days", "last read", "via / rebuilt"):
            table.add_column(col)
        for item in r.items:
            a = item.actor
            last = f"{a.last_read:%Y-%m-%d %H:%M}" if a.last_read else ""
            table.add_row(a.kind.value, a.name, str(a.reads), str(a.active_days), last, ", ".join(a.via or a.writes))
        console.print(table)
        markdown.append(f"## {r.finding.case.case_id}: {r.finding.case.primary.summary}\n")
        markdown += [f"- **{i.actor.name}** ({i.actor.kind.value}): {i.notice}" for i in r.items]
        markdown.append("")
    if out:
        Path(out).write_text("# Correction notices\n\n" + "\n".join(markdown))
        console.print(f"\nNotices written to {out}")
    else:
        console.print("\n[bold]Draft notices[/]")
        for line in markdown:
            console.print(line, highlight=False, soft_wrap=True)


@app.command()
def ask(
    question: Annotated[str | None, typer.Argument(help="A question; omit for an interactive session.")] = None,
    show_tools: Annotated[bool, typer.Option(help="Print every tool call the agent makes.")] = True,
    effort: Annotated[
        str | None, typer.Option(help="Reasoning effort: low, medium, high, xhigh, max (default per provider).")
    ] = None,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
    config: ConfigOption = "causa.yaml",
) -> None:
    """Ask the investigator agent (OPENAI_API_KEY or ANTHROPIC_API_KEY). Figures come from Causa's engines."""
    from causa.agent import NO_KEY, Toolbox, agent_provider, make_agent

    if agent_provider() is None:
        raise typer.BadParameter(NO_KEY)
    with connect() as wh:
        agent = make_agent(Toolbox(Workspace(wh, config, query_log)), effort=effort)
        prompts = iter([question]) if question else iter(lambda: console.input("[bold]ask> [/]"), None)
        for q in prompts:
            if q.strip() in {"", "exit", "quit"}:
                break
            with console.status("Investigating"):
                answer = agent.ask(q)
            if show_tools:
                for call in answer.tool_calls:
                    args = ", ".join(f"{k}={v!r}" for k, v in call.input.items() if v is not None)
                    flag = " [red]error[/]" if call.error else ""
                    console.print(f"[dim]  {call.name}({args}) {call.seconds:.1f}s{flag}[/]")
            console.print(answer.text, highlight=False, soft_wrap=True)
            cost = f", ${answer.cost_usd:.4f}" if answer.cost_usd is not None else ""
            used = answer.warehouse
            console.print(
                f"[dim]{answer.model}: {answer.input_tokens:,} in ({answer.cache_read_tokens:,} cached), "
                f"{answer.output_tokens:,} out{cost}; warehouse: {used['statements']} statements, "
                f"{used['warehouse_seconds']}s[/]"
            )


@app.command()
def brief(
    metric: str,
    day: Annotated[str | None, typer.Option(help="Morning of this day (ISO); default: latest day in the log.")] = None,
    post: Annotated[bool, typer.Option(help="Post to Slack via CAUSA_SLACK_WEBHOOK_URL.")] = False,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
    config: ConfigOption = "causa.yaml",
) -> None:
    """The morning brief: last 7 days, data vs business, who saw wrong numbers, new incidents."""
    from causa import slack
    from causa.brief import morning_brief, to_slack, to_text
    from causa.config import get_settings

    with connect() as wh, console.status("Preparing the brief"):
        b = morning_brief(Workspace(wh, config, query_log), metric, date.fromisoformat(day) if day else None)
    console.print(to_text(b), highlight=False, soft_wrap=True)
    if post:
        url = get_settings().slack_webhook_url
        if url is None:
            raise typer.BadParameter("set CAUSA_SLACK_WEBHOOK_URL to post to Slack")
        status = slack.post(url.get_secret_value(), to_slack(b))
        console.print(f"[dim]posted to Slack (HTTP {status})[/]")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    query_log: QueryLogOption = "OPS.QUERY_LOG",
    config: ConfigOption = "causa.yaml",
    warm: Annotated[bool, typer.Option(help="Prepare every incident's investigation in the background.")] = True,
) -> None:
    """Serve the engines and the agent over HTTP (FastAPI; docs at /docs)."""
    import uvicorn

    from causa.api import create_app

    wh = connect()
    app_ = create_app(Workspace(wh, config, query_log), warm=warm)
    uvicorn.run(app_, host=host, port=port)


@app.command()
def mcp(query_log: QueryLogOption = "OPS.QUERY_LOG", config: ConfigOption = "causa.yaml") -> None:
    """Serve the investigator's tools over MCP (stdio) for Claude Desktop, Claude Code and other clients."""
    from causa.agent import Toolbox
    from causa.mcp_server import serve_stdio

    # stdout carries the protocol: nothing else may print to it. Connect on the first tool call.
    serve_stdio(lambda: Toolbox(Workspace(connect(client_name="causa-mcp"), config, query_log)))


@metrics_app.command("restatements")
def show_restatements(
    metric: str,
    since: Annotated[str, typer.Option(help="First metric date (ISO).")],
    until: Annotated[str, typer.Option(help="Last metric date (ISO).")],
    dimension: Annotated[str, typer.Option(help="Dimension to check, or '*' for the total.")] = TOTAL,
    config: ConfigOption = "causa.yaml",
) -> None:
    """Days whose numbers changed after they were first reported (late data, re-runs, backfills)."""
    definition = load_config(config).metric(metric)
    with connect() as wh:
        found = restatements(wh, definition, date.fromisoformat(since), date.fromisoformat(until), dimension.upper())
    table = Table(title=f"Restatements of {definition.label}")
    for col in ("date", "segment", "first reported", "at", "now", "change"):
        table.add_column(col)
    for r in found:
        segment = "total" if r.dim_value == TOTAL else f"{r.dimension}={r.dim_value}"
        table.add_row(
            str(r.metric_date),
            segment,
            money(r.first_reported, definition.unit).lstrip("+"),
            f"{r.first_observed_at:%Y-%m-%d %H:%M}",
            money(r.latest, definition.unit).lstrip("+"),
            money(r.change, definition.unit),
        )
    console.print(table if found else "No restatements.")


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


@eval_app.command("verdict")
def eval_verdict(
    replay: Annotated[bool, typer.Option(help="Use counterfactual replay where possible.")] = True,
    config: ConfigOption = "causa.yaml",
) -> None:
    """Does the estimated data impact match what data problems really did to net revenue?"""
    definition = load_config(config).metric("net_revenue")
    results = []
    with connect() as wh:
        events = load_events(wh)
        log_frame = TableQueryLog(wh).fetch()
        catalog = Catalog.load(wh)
        graph = build_lineage(log_frame, catalog)
        first = cast(date, events["start_date"].min())
        last = cast(date, events["start_date"].max())
        with console.status("Scanning health once for all windows"):
            report = HealthScanner(wh, catalog, graph).scan(
                first - timedelta(days=7), last + timedelta(days=10), log_frame
            )
        investigator = Investigator(wh, catalog, graph, log_frame, report, replay=replay)
        for ev in events.iter_rows(named=True):
            current = Period(ev["start_date"], ev["start_date"] + timedelta(days=6))
            baseline = current.same_weekdays_before()
            with console.status(f"Judging {ev['event_id']} ({current})"):
                v = investigator.verdict(definition, current, baseline)
            reported, gap = true_data_gap(wh, "NET_REVENUE_EUR", current, baseline)
            sds = [f.estimate.sd for f in v.data_findings if f.estimate.sd]
            band = None
            if sds and len(sds) == len(v.data_findings):
                spread = 2 * sum(sd**2 for sd in sds) ** 0.5
                band = (v.data_impact - spread, v.data_impact + spread)
            results.append(
                VerdictResult(
                    ev["event_id"],
                    ev["nature"],
                    ev["kind"],
                    str(current),
                    reported,
                    gap,
                    v.data_impact,
                    band,
                    len(v.data_findings),
                )
            )
    table = Table(title="Verdict engine vs ground truth (net revenue, week starting at each event)")
    for col in ("event", "nature", "kind", "reported change", "true data gap", "Causa data impact", "error"):
        table.add_column(col, justify="right" if "change" in col or "gap" in col or "impact" in col else "left")
    table.add_column("error / change", justify="right")
    for r in results:
        ok = r.relative_error <= 0.10 or (r.band is not None and r.band[0] <= r.true_data_gap <= r.band[1])
        style = "green" if ok else "red"
        table.add_row(
            r.event_id,
            r.nature,
            r.kind,
            money(r.reported_change, "EUR"),
            money(r.true_data_gap, "EUR"),
            money(r.estimated_data_impact, "EUR"),
            money(r.error, "EUR"),
            f"[{style}]{r.relative_error:.1%}[/]",
        )
    console.print(table)
    errors = sorted(r.relative_error for r in results)
    good = sum(1 for r in results if r.relative_error <= 0.10 or (r.band and r.band[0] <= r.true_data_gap <= r.band[1]))
    console.print(
        f"median error {errors[len(errors) // 2]:.1%} of the change explained; "
        f"worst {errors[-1]:.1%}; within 10% or the 95% band: "
        f"{good}/{len(results)}"
    )


if __name__ == "__main__":
    app()
