"""A benchmark over many simulated companies, not one hand-written story.

Each seed draws its own half-year: which incidents and business changes happen, where (feed,
currency, channel, region, category, customer), when, how long, and how strong. Every company is
simulated from scratch, then scored like the default one: did the health engine alert on every data
incident and stay calm on business changes, and how close is each verdict's data impact to the true
gap from the simulator's ground truth.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from statistics import median

import numpy as np

from causa.evaluation import VerdictResult, evaluate_health, evaluate_verdicts, load_events
from causa.health import HealthScanner
from causa.lineage import build_lineage
from causa.metrics.definitions import load_config
from causa.metrics.snapshots import MetricObserver
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import (
    CustomerChurn,
    DemandShift,
    DuplicateLoad,
    Event,
    FxCorruption,
    JobFailure,
    LateArrival,
    MarketLaunch,
    NullForeignKey,
    PriceChange,
    Promotion,
    Scenario,
    SchemaDriftStall,
)
from causa.simulator.world import CATEGORIES, CHANNELS, FX_BASE_TO_EUR, REGIONS, SEGMENTS, WHALES
from causa.sources.catalog import Catalog
from causa.sources.query_log import TableQueryLog
from causa.warehouse.duckdb_backend import DuckDBWarehouse

FEEDS = tuple(f"{r.lower()}_orders" for r in REGIONS)
CURRENCIES = tuple(c for c in FX_BASE_TO_EUR if c not in ("EUR", "MXN"))  # MXN only trades after a launch
DATA_KINDS = (
    "schema_drift_stall",
    "duplicate_load",
    "fx_corruption",
    "null_foreign_key",
    "late_arrival",
    "job_failure",
)
BUSINESS_KINDS = ("customer_churn", "demand_shift", "promotion", "price_change", "market_launch")
FIRST_EVENT_DAY = 35  # the engines need a few weeks of history to know what normal looks like
SPACING_DAYS = 12


def random_scenario(seed: int, start: date = date(2026, 5, 4), days: int = 168, data_share: float = 0.6) -> Scenario:
    """One plausible half-year for one company, drawn from ``seed``."""
    rng = np.random.default_rng(seed)
    whales = [name for name, _ in WHALES]
    rng.shuffle(whales)
    launched = False
    events: list[Event] = []
    slot = FIRST_EVENT_DAY
    n = 0
    while slot <= days - SPACING_DAYS:
        n += 1
        day = slot + int(rng.integers(-2, 3))
        on = start + timedelta(days=day)
        if rng.random() < data_share:
            events.append(_data_event(rng, f"D{n}", on))
        else:
            event = _business_event(rng, f"B{n}", on, whales, launched)
            launched |= isinstance(event, MarketLaunch)
            events.append(event)
        slot += SPACING_DAYS
    return Scenario(name=f"random-{seed}", seed=seed, start=start, days=days, events=events)


def _pick(rng: np.random.Generator, options: tuple[str, ...] | list[str]) -> str:
    return str(options[int(rng.integers(0, len(options)))])


def _data_event(rng: np.random.Generator, event_id: str, on: date) -> Event:
    kind = _pick(rng, DATA_KINDS)

    def span(lo: int, hi: int) -> date:
        """The last day of an incident lasting lo..hi days."""
        return on + timedelta(days=int(rng.integers(lo, hi + 1)) - 1)

    match kind:
        case "schema_drift_stall":
            feed = _pick(rng, FEEDS)
            return SchemaDriftStall(
                event_id=event_id, start=on, end=span(3, 6), feed=feed, description=f"{feed}: order timestamp renamed"
            )
        case "duplicate_load":
            feed = _pick(rng, FEEDS)
            return DuplicateLoad(event_id=event_id, start=on, end=on, feed=feed, description=f"{feed} loaded twice")
        case "fx_corruption":
            currency = _pick(rng, CURRENCIES)
            factor = float(rng.uniform(1.25, 1.5) if rng.random() < 0.5 else rng.uniform(0.6, 0.8))
            return FxCorruption(
                event_id=event_id,
                start=on,
                end=span(2, 4),
                currency=currency,
                factor=round(factor, 2),
                description=f"{currency} rate off by x{factor:.2f}",
            )
        case "null_foreign_key":
            channel = _pick(rng, CHANNELS)
            fraction = round(float(rng.uniform(0.15, 0.4)), 2)
            return NullForeignKey(
                event_id=event_id,
                start=on,
                end=span(3, 7),
                channel=channel,
                fraction=fraction,
                description=f"{fraction:.0%} of {channel} lines missing PRODUCT_ID",
            )
        case "late_arrival":
            feed = _pick(rng, FEEDS)
            delay = int(rng.integers(1, 4))
            return LateArrival(
                event_id=event_id,
                start=on,
                end=on,
                feed=feed,
                delay_days=delay,
                description=f"{feed} batch {delay} days late",
            )
        case _:
            recovered = on + timedelta(days=int(rng.integers(1, 4)))
            return JobFailure(
                event_id=event_id,
                start=on,
                end=on,
                recovered_on=recovered,
                description=f"fct_order_lines load failed; re-run on {recovered}",
            )


def _business_event(rng: np.random.Generator, event_id: str, on: date, whales: list[str], launched: bool) -> Event:
    kinds = [k for k in BUSINESS_KINDS if (k != "customer_churn" or whales) and (k != "market_launch" or not launched)]
    kind = _pick(rng, kinds)
    match kind:
        case "customer_churn":
            name = whales.pop()
            return CustomerChurn(event_id=event_id, start=on, customer_name=name, description=f"{name} churned")
        case "demand_shift":
            region = _pick(rng, REGIONS)
            segment = _pick(rng, SEGMENTS) if rng.random() < 0.5 else None
            multiplier = round(float(rng.uniform(0.75, 0.9) if rng.random() < 0.6 else rng.uniform(1.1, 1.25)), 2)
            return DemandShift(
                event_id=event_id,
                start=on,
                region=region,
                segment=segment,
                multiplier=multiplier,
                description=f"{region} {segment or 'all segments'} demand x{multiplier}",
            )
        case "promotion":
            category = _pick(rng, tuple(CATEGORIES))
            return Promotion(
                event_id=event_id,
                start=on,
                end=on + timedelta(days=int(rng.integers(7, 15))),
                category=category,
                extra_discount_pct=round(float(rng.uniform(10, 20)), 1),
                volume_multiplier=round(float(rng.uniform(1.4, 2.0)), 2),
                description=f"{category} promotion",
            )
        case "price_change":
            category = _pick(rng, tuple(CATEGORIES))
            pct = round(float(rng.choice([-1, 1]) * rng.uniform(5, 10)), 1)
            return PriceChange(
                event_id=event_id, start=on, category=category, pct=pct, description=f"{category} price {pct:+}%"
            )
        case _:
            return MarketLaunch(
                event_id=event_id,
                start=on,
                country="MX",
                new_customers=int(rng.integers(120, 221)),
                description="Mexico market launch",
            )


# -- scoring ------------------------------------------------------------------------------------


@dataclass
class SeedResult:
    seed: int
    data_events: int
    business_events: int
    recall: float
    calm: float
    precision: float
    verdicts: list[VerdictResult]
    seconds: float

    @property
    def within(self) -> int:
        """Verdicts whose data impact is within 10% of the change, or whose 95% band holds the truth."""
        return sum(_ok(r) for r in self.verdicts)


def _ok(r: VerdictResult) -> bool:
    return r.relative_error <= 0.10 or (r.band is not None and r.band[0] <= r.true_data_gap <= r.band[1])


def score_seed(seed: int, config: str = "causa.yaml") -> SeedResult:
    """Simulate one random company in memory and score both engines on it."""
    started = time.perf_counter()
    scenario = random_scenario(seed)
    metrics = load_config(config)
    wh = DuckDBWarehouse(":memory:")
    try:
        observer = MetricObserver(wh, metrics.metrics)
        observer.reset()
        run_simulation(
            scenario,
            wh,
            on_loaded=lambda d: observer.observe(datetime.combine(d + timedelta(days=1), dtime(3, 30)), d),
        )
        log_frame = TableQueryLog(wh).fetch()
        catalog = Catalog.load(wh)
        graph = build_lineage(log_frame, catalog)
        since = scenario.start + timedelta(days=FIRST_EVENT_DAY - 7)
        until = scenario.start + timedelta(days=scenario.days - 2)
        report = HealthScanner(wh, catalog, graph).scan(since, until, log_frame)
        health = evaluate_health(report, load_events(wh))
        verdicts = evaluate_verdicts(wh, metrics.metric("net_revenue"))
    finally:
        wh.close()
    natures = [str(e.nature) for e in scenario.events]
    return SeedResult(
        seed,
        natures.count("data"),
        natures.count("business"),
        health.recall,
        health.calm,
        health.precision,
        verdicts,
        time.perf_counter() - started,
    )


def run_benchmark(seeds: list[int], workers: int = 4) -> list[SeedResult]:
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return sorted(pool.map(score_seed, seeds), key=lambda r: r.seed)


def report_markdown(results: list[SeedResult]) -> str:
    verdicts = [v for r in results for v in r.verdicts]
    errors = sorted(v.relative_error for v in verdicts)
    data_events = sum(r.data_events for r in results)
    business_events = sum(r.business_events for r in results)
    lines = [
        "# Causa benchmark: randomized companies",
        "",
        f"{len(results)} simulated companies (seeds {results[0].seed}-{results[-1].seed}), each a half-year with its "
        f"own randomly drawn incidents and business changes: {data_events} data incidents and {business_events} "
        "business changes in total. Generated by `causa eval bench`.",
        "",
        "| | |",
        "|---|---|",
        f"| Data incidents raised as alerts (recall) | **{_mean(r.recall for r in results):.0%}** |",
        f"| Business changes left calm | **{_mean(r.calm for r in results):.0%}** |",
        f"| Alerts that match a real incident (precision) | **{_mean(r.precision for r in results):.0%}** |",
        f"| Verdicts within 10% of the change (or inside their 95% band) | "
        f"**{sum(_ok(v) for v in verdicts)} / {len(verdicts)}** |",
        f"| Median verdict error, share of the change | **{median(errors or [0.0]):.1%}** |",
        f"| Worst verdict error | {max(errors or [0.0]):.1%} |",
        "",
        "## By company",
        "",
        "| Seed | Data / business events | Recall | Calm | Precision | Verdicts OK | Worst error | Time |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        worst = max((v.relative_error for v in r.verdicts), default=0.0)
        lines.append(
            f"| {r.seed} | {r.data_events} / {r.business_events} | {r.recall:.0%} | {r.calm:.0%} | "
            f"{r.precision:.0%} | {r.within}/{len(r.verdicts)} | {worst:.1%} | {r.seconds:.0f}s |"
        )
    lines += [
        "",
        "## By kind of event",
        "",
        "| Kind | Nature | Verdicts | OK | Median error |",
        "|---|---|---:|---:|---:|",
    ]
    for kind in sorted({v.kind for v in verdicts}):
        of = [v for v in verdicts if v.kind == kind]
        lines.append(
            f"| {kind} | {of[0].nature} | {len(of)} | {sum(_ok(v) for v in of)} | "
            f"{median(v.relative_error for v in of):.1%} |"
        )
    misses = [(r.seed, v) for r in results for v in r.verdicts if not _ok(v)]
    if misses:
        lines += [
            "",
            "## Verdicts outside the bar",
            "",
            "| Seed | Event | Kind | Reported change | True gap | Causa | Error |",
        ]
        lines.append("|---:|---|---|---:|---:|---:|---:|")
        for seed, v in misses:
            lines.append(
                f"| {seed} | {v.event_id} | {v.kind} | {v.reported_change:,.0f} | {v.true_data_gap:,.0f} | "
                f"{v.estimated_data_impact:,.0f} | {v.relative_error:.1%} |"
            )
    return "\n".join(lines) + "\n"


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0
