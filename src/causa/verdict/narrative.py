"""Plain-language rendering of a verdict. Every figure comes from the engines and says how it was made."""

from __future__ import annotations

from causa.metrics.narrative import driver_lines, headline, material_floor, money, notable
from causa.metrics.snapshots import TOTAL
from causa.verdict.classifier import DataFinding, Verdict


def _span(days: list) -> str:  # type: ignore[type-arg]
    if not days:
        return "-"
    return str(days[0]) if len(days) == 1 else f"{days[0]} .. {days[-1]}"


def _share(part: float, whole: float) -> str:
    return f", {part / whole:.0%} of the change" if whole and 0 < part / whole <= 1.5 else ""


def finding_lines(f: DataFinding, v: Verdict) -> list[str]:
    unit = v.metric.unit
    e = f.estimate
    lines = [f"  {f.case.case_id}: {f.case.primary.signal.value}: {f.case.primary.summary}"]
    others = sorted({i.signal.value for i in f.case.incidents} - {f.case.primary.signal.value})
    if others:
        lines.append(f"    with {', '.join(others)}")
    scope = f.link.subset.label if not f.link.subset.everything else "all rows of the metric"
    where = "; ".join(f"{o.driver} ({o.coverage:.0%} of the affected value lands there)" for o in f.overlaps)
    lines.append(f"    affects {scope}{' -> ' + where if where else ''}")
    when = []
    if e.current_days:
        when.append(f"current period {_span(e.current_days)}")
    if e.baseline_days:
        when.append(f"baseline {_span(e.baseline_days)} (distorts the comparison)")
    lines.append(f"    on {' and '.join(when) or 'no compared day'}")
    band = f" (95% band {money(e.low, unit)} .. {money(e.high, unit)})" if e.sd else ""
    lines.append(f"    impact {money(e.impact, unit)}{band}: {e.method}")
    if f.check is not None:
        c = f.check
        band = f" ± {money(2 * c.sd, unit).lstrip('+')}" if c.sd else ""
        lines.append(f"    statistical cross-check: {money(c.impact, unit)}{band}")
    for note in e.notes[:2]:
        lines.append(f"    note: {note}")
    reasons = ", ".join(f"{'+' if x.weight > 0 else '-'} {x.text}" for x in f.evidence)
    lines.append(f"    evidence: {reasons}")
    return lines


def render(v: Verdict) -> list[str]:
    total = v.explanation.total.delta
    unit = v.metric.unit
    lines = [headline(v.explanation), ""]
    data = v.data_findings
    if data:
        lines.append(
            f"DATA INCIDENTS: {money(v.data_impact, unit)}{_share(v.data_impact, total)} "
            f"(confidence {min(f.confidence for f in data):.0%}"
            f"{'' if len(data) == 1 else ' or higher each'})"
        )
        for f in sorted(data, key=lambda f: -abs(f.estimate.impact)):
            lines += finding_lines(f, v)
            for twin in (c for c in v.corroborating if c.duplicate_of == f.case.case_id):
                lines.append(
                    f"    also seen as {twin.case.case_id}: {twin.case.primary.summary[:100]} (not counted twice)"
                )
        lines.append("")
    business_pct = v.business_change / v.explanation.total.baseline if v.explanation.total.baseline else None
    typical = v.explanation.typical_change
    judged = ""
    unusual = None
    z = v.explanation.judge(business_pct) if business_pct is not None else None
    if z is not None and typical:
        unusual = abs(z) >= 2.0
        judged = "unusual" if unusual else "within normal variation"
        judged = f"; {judged} (typically ±{typical:.1%})"
    pct = f" ({business_pct:+.1%})" if business_pct is not None else ""
    lines.append(
        f"REAL BUSINESS CHANGE: {money(v.business_change, unit)}{pct}{_share(v.business_change, total)}{judged}"
    )
    clean_days = v.explanation.current.days - sum(
        1 for d in v.excluded_days if v.explanation.current.start <= d <= v.explanation.current.end
    )
    if v.business is None or (v.excluded_days and clean_days < 3):
        lines.append(
            f"  only {max(clean_days, 0)} compared day(s) untouched by data incidents: too few to break it down"
        )
    else:
        b = v.business
        scope = f"on the {clean_days} days no incident touched, " if v.excluded_days else ""
        quiet = unusual is False
        drivers = [d for d in b.drivers if notable(d, material_floor(b))] if quiet else b.drivers
        if drivers:
            lines.append(f"  {scope}{'notable movements' if quiet else 'drivers'}:")
            for d in drivers:
                lines += ["  " + x for x in driver_lines(d, b, notable_only=quiet)]
        else:
            lines.append(f"  {scope}nothing concentrated stands out")
    dismissed = [f for f in v.findings if not f.is_data]
    if dismissed:
        lines += ["", "CONSIDERED AND NOT COUNTED AS DATA INCIDENTS:"]
        for f in dismissed:
            lines.append(
                f"  {f.case.case_id} ({f.confidence:.0%}): {f.case.primary.summary[:110]}; "
                f"would be {money(f.estimate.impact, unit)}"
            )
    if v.restatements:
        lines += ["", "REPORTED AT THE TIME, CORRECTED SINCE:"]
        for r in v.restatements:
            segment = "total" if r.dim_value == TOTAL else f"{r.dimension}={r.dim_value}"
            lines.append(
                f"  {r.metric_date} {segment}: first reported {money(r.first_reported, unit).lstrip('+')} "
                f"({r.first_observed_at:%Y-%m-%d %H:%M}), now {money(r.latest, unit).lstrip('+')}"
            )
    return lines
