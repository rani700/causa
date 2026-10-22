"""Plain-language rendering of an explanation. Every number comes from the engine; nothing is invented."""

from __future__ import annotations

from causa.metrics.attribution import Driver, Explanation, Segment

_CURRENCY = {"EUR": "€", "USD": "$", "GBP": "£"}


def money(value: float, unit: str) -> str:
    symbol = _CURRENCY.get(unit, "")
    sign = "-" if value < 0 else "+"
    v = abs(value)
    if v >= 1e9:
        text = f"{v / 1e9:.2f}B"
    elif v >= 1e6:
        text = f"{v / 1e6:.2f}M"
    elif v >= 1e3:
        text = f"{v / 1e3:.1f}K"
    else:
        text = f"{v:,.0f}"
    suffix = f" {unit}" if unit and not symbol else ""
    return f"{sign}{symbol}{text}{suffix}"


def _pct(value: float | None) -> str:
    return "new" if value is None else f"{value:+.1%}"


def headline(e: Explanation) -> str:
    unit = e.metric.unit
    verb = "rose" if e.total.delta > 0 else "fell" if e.total.delta < 0 else "was flat"
    amount = money(abs(e.total.delta), unit).lstrip("+")
    text = f"{e.metric.label} {verb} {amount} ({_pct(e.total.pct)}) in {e.current} vs {e.baseline}"
    if e.unusual is None:
        return text + " (not enough history to judge how unusual this is)."
    if e.unusual:
        return text + f": unusual (z={e.significance:+.1f}; changes are typically ±{e.typical_change:.1%})."
    return text + f": within normal variation (typically ±{e.typical_change:.1%})."


def effects_line(seg: Segment, e: Explanation) -> str | None:
    effects = seg.effects
    if not effects or not e.metric.volume_column:
        return None
    vol, rate = effects
    label = e.metric.volume_label
    counts = f"{seg.volume_baseline:,.0f} -> {seg.volume_current:,.0f} {label}"
    dominant = "volume" if abs(vol) >= abs(rate) else f"value per {label.rstrip('s')}"
    return (
        f"volume {money(vol, e.metric.unit)} ({counts}), value per {label.rstrip('s')} "
        f"{money(rate, e.metric.unit)}; mostly {dominant}"
    )


NOTABLE_CHANGE = 0.25


def _notable(d: Driver) -> bool:
    """When the total is normal, only movements that are large in their own right are worth showing."""
    pct = d.segment.pct
    own = d.segment.vanished or (pct is not None and abs(pct) >= NOTABLE_CHANGE)
    return own or any(_notable(c) for c in d.children)


def driver_lines(d: Driver, e: Explanation, depth: int = 0, notable_only: bool = False) -> list[str]:
    pad = "  " * depth
    seg = d.segment
    dim, value = seg.leaf
    share = f", {d.share:.0%} of the change" if e.unusual and 0 < d.share <= 1.5 else ""
    lines = [f"{pad}- {dim}={value}: {money(seg.delta, e.metric.unit)} ({_pct(seg.pct)}){share}"]
    if seg.vanished:
        before = f"{seg.volume_baseline:,.0f} {e.metric.volume_label} -> 0" if seg.volume_baseline else "-> 0"
        lines.append(f"{pad}    stopped entirely ({before})")
    else:
        effects = effects_line(seg, e)
        if effects:
            lines.append(f"{pad}    {effects}")
    if d.uniform:
        dims = ", ".join(v.dimension.lower() for v in d.inner if v.dimension in e.metric.dimensions)
        lines.append(f"{pad}    spread evenly across its {dims}: every part moved by about the same %")
    for off in [] if notable_only else d.offsets:
        lines.append(f"{pad}    partly offset by {off.label}: {money(off.delta, e.metric.unit)}")
    for child in d.children:
        if not notable_only or _notable(child):
            lines += driver_lines(child, e, depth + 1, notable_only)
    return lines


def render(e: Explanation) -> list[str]:
    lines = [headline(e)]
    quiet = e.unusual is False
    drivers = [d for d in e.drivers if _notable(d)] if quiet else e.drivers
    if drivers:
        lines.append("Underneath the normal-looking total, notable movements:" if quiet else "Drivers:")
        for d in drivers:
            lines += driver_lines(d, e, notable_only=quiet)
    elif quiet:
        lines.append("Nothing underneath stands out either.")
    else:
        lines.append("No concentrated driver: the change is spread across every dimension in proportion to size.")
    if drivers or not quiet:
        for off in e.offsets:
            lines.append(f"Partly offset by {off.label}: {money(off.delta, e.metric.unit)} ({_pct(off.pct)}).")
    return lines
