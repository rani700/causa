"""Engine 5b: recall. Who consumed the wrong numbers, and what should they be told?

For each data finding of a verdict:

* **exposure**: from the first bad load until the incident healed, or until now if the warehouse
  still holds the damage (lost rows that were never backfilled stay lost);
* **misstatement**: exactly how wrong the metric was on each day: from the counterfactual replay
  when there is one, from restatements (first reported vs corrected) for healed incidents;
* **consumers**: every actor lineage finds downstream of the damaged table that read affected data
  (or rebuilt something from it) during exposure: dashboards, scheduled reports and their
  recipients, ML pipelines, reverse-ETL syncs, spreadsheets and analysts;
* **notices**: a correction note per consumer, worded for what that consumer does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from causa.lineage import LineageGraph
from causa.lineage.actors import ActorKind
from causa.lineage.graph import ImpactedActor
from causa.metrics.narrative import money
from causa.metrics.snapshots import TOTAL
from causa.verdict.classifier import HEALED, DataFinding, Verdict
from causa.verdict.linkage import all_incidents


@dataclass
class RecallItem:
    actor: ImpactedActor
    tool: str
    attributes: dict[str, str]
    notice: str


@dataclass
class Recall:
    finding: DataFinding
    origin: str
    exposure_start: date
    exposure_end: date | None  # None: still exposed
    misstatement: dict[date, float]  # reported minus correct, by business date
    carried: set[str] = field(default_factory=set)  # tables on the path from the damage to the metric
    items: list[RecallItem] = field(default_factory=list)

    @property
    def total_misstatement(self) -> float:
        return sum(self.misstatement.values())

    def of_kind(self, *kinds: ActorKind) -> list[RecallItem]:
        return [i for i in self.items if i.actor.kind in kinds]


def recall(verdict: Verdict, graph: LineageGraph, today: date) -> list[Recall]:
    out = []
    for finding in verdict.data_findings:
        incidents = all_incidents(finding.case)
        depth = {i.table: len(graph.upstream_tables(i.table)) for i in incidents}
        origin = min(incidents, key=lambda i: depth[i.table]).table
        load_days = sorted({d for i in incidents for d in (i.days or [i.start])})
        healed = finding.estimate.method == HEALED
        start = load_days[0]
        end = (max(i.end or i.start for i in incidents) + timedelta(days=1)) if healed else None
        misstatement = _misstatement(finding, verdict)
        impact = graph.impact(origin, since=start, until=end or today)
        items = [
            RecallItem(a, *_actor_details(graph, a), "")
            for a in impact.actors
            if a.kind is not ActorKind.SOURCE and (a.reads or a.writes)
        ]
        feeding = {verdict.metric.table, *graph.upstream_tables(verdict.metric.table)}
        carried = {t for t in graph.downstream_tables(origin) if t in feeding}
        r = Recall(finding, origin, start, end, misstatement, carried)
        for item in items:
            item.notice = notice(item, r, verdict, today)
        r.items = sorted(items, key=lambda i: (_ORDER.index(i.actor.kind), -i.actor.reads))
        out.append(r)
    return out


def short_cause(finding: DataFinding) -> str:
    """The incident in a few words, for people who do not read SQL."""
    case = finding.case
    by_signal = {i.signal: i for i in reversed(all_incidents(case))}
    first = case.primary

    def where(i):  # type: ignore[no-untyped-def]
        return f" for {i.segment_column}={i.segment}" if i.segment else ""

    from causa.health import Signal

    if Signal.SCHEMA_CHANGE in by_signal:
        change = by_signal[Signal.SCHEMA_CHANGE]
        actor = change.evidence.get("actor", "an upstream system")
        lost = by_signal.get(Signal.NULL_RATE)
        tail = f", which left {lost.column} empty{where(lost)}" if lost else ""
        return f"an upstream schema change on {change.table} by {actor}{tail}"
    match first.signal:
        case Signal.NULL_RATE:
            return f"missing {first.column} values in {first.table}{where(first)}"
        case Signal.DUPLICATE_RATE:
            return f"rows loaded twice into {first.table}{where(first)}"
        case Signal.VALUE_SHIFT:
            return f"a wrong {first.column} in {first.table}{where(first)}"
        case Signal.FAILED_WRITE:
            return f"a failed load into {first.table}"
        case Signal.LATENESS:
            return f"data arriving late in {first.table}{where(first)}"
        case _:
            return first.summary.split(";")[0]


def _misstatement(finding: DataFinding, verdict: Verdict) -> dict[date, float]:
    replay = finding.replay
    if replay is not None:
        # The replay's diff is correct minus reported; a misstatement is reported minus correct.
        return {d: -v for d, v in replay.daily_diff.items()}  # type: ignore[attr-defined]
    days = set(finding.business_days)
    return {
        r.metric_date: r.first_reported - r.latest
        for r in verdict.restatements
        if r.dimension == TOTAL and r.metric_date in days
    }


def _actor_details(graph: LineageGraph, actor: ImpactedActor) -> tuple[str, dict[str, str]]:
    data = graph.g.nodes.get(f"actor:{actor.actor}", {})
    return data.get("tool", ""), data.get("attributes", {})


_ORDER = [
    ActorKind.SCHEDULED_REPORT,
    ActorKind.DASHBOARD,
    ActorKind.SPREADSHEET,
    ActorKind.REVERSE_ETL,
    ActorKind.PIPELINE,
    ActorKind.AD_HOC,
    ActorKind.DEPLOYMENT,
    ActorKind.SOURCE,
]


def notice(item: RecallItem, r: Recall, verdict: Verdict, today: date) -> str:
    a = item.actor
    unit = verdict.metric.unit
    label = verdict.metric.label.lower()
    days = sorted(r.misstatement)
    span = (f"{days[0]}" if days[0] == days[-1] else f"{days[0]} to {days[-1]}") if days else "the affected days"
    total = r.total_misstatement
    direction = "overstated" if total > 0 else "understated"
    amount = money(abs(total), unit).lstrip("+")
    cause = short_cause(r.finding)
    status = (
        f"The data was complete again by {r.exposure_end}."
        if r.exposure_end
        else "The warehouse still holds the damage; figures stay wrong until the affected loads are re-run."
    )
    exposure = f"{r.exposure_start} and {r.exposure_end or today}"
    reload_days = ", ".join(str(d) for d in sorted({d for i in all_incidents(r.finding.case) for d in i.days})[:7])
    via = ", ".join(a.via) or ", ".join(a.writes)
    match a.kind:
        case ActorKind.SCHEDULED_REPORT:
            recipients = item.attributes.get("recipients")
            who = f" to {recipients} recipients" if recipients else ""
            return (
                f"'{a.name}' was generated {a.active_days} time(s){who} between {exposure} using {via}. "
                f"It {direction} {label} by {amount} for {span} because of a data incident ({cause}). "
                f"{status} Please re-issue the affected editions with a correction note."
            )
        case ActorKind.DASHBOARD | ActorKind.SPREADSHEET if not a.reads and a.writes:
            return (
                f"'{a.name}' ({item.tool}) refreshed its extract ({', '.join(a.writes)}) from affected data between "
                f"{exposure}, but nobody opened it in that period. Refresh it after the fix, or consider retiring it."
            )
        case ActorKind.DASHBOARD | ActorKind.SPREADSHEET:
            return (
                f"'{a.name}' ({item.tool}) was opened {a.reads} time(s) between {exposure} "
                f"while {label} for {span} was {direction} by {amount} ({cause}). {status} "
                f"Add a data-quality banner and tell its regular viewers which figures were wrong."
            )
        case ActorKind.REVERSE_ETL:
            return (
                f"'{a.name}' pushed records derived from affected data ({via}) in {a.active_days} sync(s) between "
                f"{exposure}. "
                f"Records written to the destination system in that period may be wrong; re-sync after the fix."
            )
        case ActorKind.PIPELINE if set(a.writes) & r.carried:
            loaded = ", ".join(sorted(set(a.writes) & r.carried))
            todo = (
                "Its re-run already completed the data."
                if r.exposure_end
                else f"After fixing the source, re-run its loads for {reload_days}."
            )
            return f"'{a.name}' carried the incident: it loaded {loaded} from the damaged data. {todo}"
        case ActorKind.PIPELINE:
            rebuilt = ", ".join(a.writes) or "its outputs"
            todo = (
                "Its later runs used complete data; check whether anything it produced in between was used."
                if r.exposure_end
                else "Re-run it after the fix: whatever it produced or trained in that period rests on wrong numbers."
            )
            return (
                f"'{a.name}' rebuilt {rebuilt} from affected data in {a.active_days} run(s) between {exposure}. {todo}"
            )
        case ActorKind.AD_HOC:
            return (
                f"{a.name} queried affected tables ({via}) {a.reads} time(s) between {exposure}. "
                f"Analyses from that period that used {label} for {span} should be re-checked."
            )
        case _:
            return f"{a.name} touched affected data ({via}) between {exposure}."
