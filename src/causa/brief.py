"""The morning brief: what changed in a metric over the last week, what of it is real, who saw wrong numbers.

Deterministic on purpose: an alert that lands in a channel every morning must not depend on how a
language model phrases things on a given day. Every sentence is assembled from engine output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from causa.health import Severity
from causa.health.models import HealthCase
from causa.lineage.actors import ActorKind
from causa.metrics.attribution import Driver, Period
from causa.metrics.narrative import material_floor, money, notable
from causa.recall import Recall, recall, short_cause
from causa.verdict import Verdict
from causa.workspace import Workspace


@dataclass
class Brief:
    day: date
    verdict: Verdict
    recalls: list[Recall]
    new_cases: list[HealthCase] = field(default_factory=list)

    @property
    def unit(self) -> str:
        return self.verdict.metric.unit


def morning_brief(ws: Workspace, metric_name: str, day: date | None = None) -> Brief:
    day = day or ws.today
    current = Period(day - timedelta(days=7), day - timedelta(days=1))
    baseline = current.same_weekdays_before()
    report = ws.health(baseline.start, day)
    verdict = ws.investigator(replay=True).verdict(ws.config.metric(metric_name), current, baseline)
    new_cases = [
        c
        for c in report.cases
        if day - timedelta(days=1) <= c.start <= day and c.severity in (Severity.CRITICAL, Severity.HIGH)
    ]
    return Brief(day, verdict, recall(verdict, ws.graph, day), new_cases)


def _headline(b: Brief) -> str:
    e = b.verdict.explanation
    t = e.total
    verb = "up" if t.delta > 0 else "down"
    pct = f"{t.pct:+.1%}" if t.pct is not None else "n/a"
    judged = ""
    if e.unusual is not None:
        judged = "unusual" if e.unusual else "within normal variation"
        judged = f" ({judged}; typically ±{e.typical_change:.1%})"
    return f"{e.metric.label} {verb} {money(abs(t.delta), b.unit).lstrip('+')} ({pct}) in {e.current}{judged}."


def _split(b: Brief) -> str:
    v = b.verdict
    if not v.data_findings:
        return "No data incident affects this change: it is real business movement."
    total = v.explanation.total.delta
    share = f" ({v.data_impact / total:.0%} of the change)" if total and 0 < v.data_impact / total <= 1.5 else ""
    return (
        f"{money(v.data_impact, b.unit)}{share} comes from data incidents; "
        f"the real business change is {money(v.business_change, b.unit)}."
    )


def _finding_line(b: Brief, r: Recall | None, f) -> str:  # type: ignore[no-untyped-def]
    method = "replayed" if f.estimate.method.startswith("counterfactual") else f.estimate.method.split(":")[0]
    line = f"{short_cause(f)}: {money(f.estimate.impact, b.unit)} ({method}, confidence {f.confidence:.0%})"
    if r is not None:
        reports = r.of_kind(ActorKind.SCHEDULED_REPORT)
        dashboards = [i for i in r.of_kind(ActorKind.DASHBOARD) if i.actor.reads]
        seen = []
        if reports:
            seen.append(", ".join(f"{i.actor.name} ({i.actor.active_days}x)" for i in reports))
        if dashboards:
            seen.append(f"{len(dashboards)} dashboards ({sum(i.actor.reads for i in dashboards)} views)")
        status = "still wrong in the warehouse" if r.exposure_end is None else f"healed {r.exposure_end}"
        line += f"; seen in {'; '.join(seen) or 'no reports'}; {status}"
    return line


def _business_lines(b: Brief) -> list[str]:
    """Same rules as the verdict: enough unaffected days, and only notable movements when the
    business change itself is within normal variation."""
    v = b.verdict
    e = v.explanation
    clean = e.current.days - sum(1 for d in v.excluded_days if e.current.start <= d <= e.current.end)
    if v.business is None or (v.excluded_days and clean < 3):
        return []
    pct = v.business_change / e.total.baseline if e.total.baseline else None
    z = e.judge(pct) if pct is not None else None
    floor = material_floor(v.business)
    drivers = v.business.drivers
    if z is not None and abs(z) < 2:
        drivers = [d for d in drivers if notable(d, floor)]

    def line(d: Driver, pad: str) -> str:
        dim, value = d.segment.leaf
        tail = " (stopped entirely)" if d.segment.vanished else ""
        return f"{pad}{dim}={value}: {money(d.segment.delta, b.unit)}{tail}"

    out = []
    for d in drivers[:3]:
        out.append(line(d, ""))
        out += [line(c, "  ") for c in d.children[:2] if not (z is not None and abs(z) < 2) or notable(c, floor)]
    return out


def to_text(b: Brief) -> str:
    recalls = {r.finding.case.case_id: r for r in b.recalls}
    lines = [f"Causa morning brief, {b.day}", "", _headline(b), _split(b)]
    if b.verdict.data_findings:
        lines.append("")
        lines.append("Data incidents:")
        lines += [f"- {_finding_line(b, recalls.get(f.case.case_id), f)}" for f in b.verdict.data_findings]
    business = _business_lines(b)
    if business:
        lines += ["", "Real business movement on unaffected days:"]
        lines += [f"  - {x.strip()}" if x.startswith("  ") else f"- {x}" for x in business]
    if b.new_cases:
        lines += ["", "New since yesterday:", *[f"- {c.case_id}: {c.primary.summary[:140]}" for c in b.new_cases]]
    return "\n".join(lines)


def to_slack(b: Brief) -> dict[str, Any]:
    """Slack Block Kit payload for an incoming webhook."""
    recalls = {r.finding.case.case_id: r for r in b.recalls}
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Causa morning brief · {b.day}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_headline(b)}*\n{_split(b)}"}},
    ]
    if b.verdict.data_findings:
        items = "\n".join(
            f":red_circle: {_finding_line(b, recalls.get(f.case.case_id), f)}" for f in b.verdict.data_findings
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Data incidents*\n{items}"}})
    business = _business_lines(b)
    if business:
        items = "\n".join(f":large_green_circle: {x}" for x in business)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Real business movement*\n{items}"}})
    if b.new_cases:
        items = "\n".join(f"• {c.case_id}: {c.primary.summary[:140]}" for c in b.new_cases)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*New since yesterday*\n{items}"}})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Figures from Causa's engines; data impacts replayed on repaired data where possible. "
                    "Ask follow-ups with `causa ask`.",
                }
            ],
        }
    )
    return {"text": _headline(b), "blocks": blocks}
