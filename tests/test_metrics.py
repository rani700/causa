from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from causa.metrics.attribution import Attributor, Period, Segment, shapley_split
from causa.metrics.definitions import MetricDefinition, load_config
from causa.metrics.narrative import render
from causa.metrics.snapshots import MetricObserver, restatements
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import CustomerChurn, Event, JobFailure, LateArrival, SchemaDriftStall
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from tests.conftest import START, small_scenario

DAYS = 84  # 12 weeks: enough history for significance (needs 6 comparable windows)
WEEK = (77, 83)  # the week being explained


def day(offset: int) -> date:
    return START + timedelta(days=offset)


@pytest.fixture(scope="module")
def metric() -> MetricDefinition:
    return load_config("causa.yaml").metric("net_revenue")


def simulate(events: list[Event], metric: MetricDefinition | None = None) -> DuckDBWarehouse:
    wh = DuckDBWarehouse(":memory:")
    scenario = small_scenario(events, days=DAYS)
    scenario.base_orders_per_day = 250
    observer = MetricObserver(wh, [metric]) if metric else None
    run_simulation(
        scenario,
        wh,
        on_loaded=(lambda d: observer.observe(datetime.combine(d + timedelta(days=1), time(3, 30)), d))
        if observer
        else None,
    )
    return wh


# -- pure functions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("m0", "m1", "v0", "v1"),
    [(100.0, 150.0, 10.0, 12.0), (100.0, 0.0, 10.0, 0.0), (0.0, 80.0, 0.0, 4.0), (100.0, 90.0, 10.0, 10.0)],
)
def test_shapley_effects_add_up_exactly(m0: float, m1: float, v0: float, v1: float) -> None:
    vol, rate = shapley_split(m0, m1, v0, v1)  # type: ignore[misc]
    assert vol + rate == pytest.approx(m1 - m0)


def test_pure_volume_and_pure_rate_changes() -> None:
    assert shapley_split(100.0, 50.0, 10.0, 5.0) == pytest.approx((-50.0, 0.0))  # lost half the orders
    assert shapley_split(100.0, 135.0, 10.0, 10.0) == pytest.approx((0.0, 35.0))  # same orders, worth more


def test_periods() -> None:
    week = Period.parse("2026-10-06..2026-10-12")
    assert (week.days, str(week.same_weekdays_before())) == (7, "2026-09-29..2026-10-05")
    assert Period.parse("2026-10-06..2026-10-08").same_weekdays_before() == Period.parse("2026-09-29..2026-10-01")
    assert Segment((), current=5.0, baseline=0.0).vanished is False
    assert Segment((), current=0.0, baseline=5.0).vanished is True


# -- on the simulator -------------------------------------------------------------------------


def test_lost_feed_is_one_uniform_volume_driver(metric: MetricDefinition) -> None:
    wh = simulate([SchemaDriftStall(event_id="D", start=day(78), end=day(82), feed="emea_orders")])
    e = Attributor(wh).explain(metric, Period(day(WEEK[0]), day(WEEK[1])))
    assert e.unusual
    (driver,) = e.drivers
    assert driver.segment.filters == (("REGION", "EMEA"),)
    assert driver.share > 0.8
    vol, rate = driver.segment.effects  # type: ignore[misc]
    assert abs(vol) > 5 * abs(rate)  # lost rows, not cheaper orders
    assert driver.uniform
    assert e.queries <= 6


def test_churned_customer_found_under_a_normal_total(metric: MetricDefinition) -> None:
    wh = simulate([CustomerChurn(event_id="B", start=day(WEEK[0]), customer_name="Acme GmbH")])
    e = Attributor(wh).explain(metric, Period(day(WEEK[0]), day(WEEK[1])))
    vanished = [d for _, d in e.walk() if d.segment.vanished]
    assert [d.segment.leaf for d in vanished] == [("CUSTOMER_NAME", "Acme GmbH")]
    assert any("stopped entirely" in line for line in render(e))


def test_quiet_week_has_no_drivers_or_says_so(metric: MetricDefinition) -> None:
    wh = simulate([])
    e = Attributor(wh).explain(metric, Period(day(WEEK[0]), day(WEEK[1])))
    assert e.unusual is False
    assert not e.drivers or all(not d.segment.vanished for _, d in e.walk())


def test_overlapping_or_unequal_periods_are_rejected(metric: MetricDefinition) -> None:
    wh = DuckDBWarehouse(":memory:")
    with pytest.raises(ValueError, match="overlap"):
        Attributor(wh).explain(metric, Period.parse("2026-10-06..2026-10-12"), Period.parse("2026-10-10..2026-10-16"))
    with pytest.raises(ValueError, match="differ"):
        Attributor(wh).explain(metric, Period.parse("2026-10-06..2026-10-12"), Period.parse("2026-09-01..2026-09-03"))


def test_restatements_reveal_self_healing_incidents(metric: MetricDefinition) -> None:
    wh = simulate(
        [
            LateArrival(event_id="D1", start=day(70), end=day(70), feed="na_orders", delay_days=2),
            JobFailure(event_id="D2", start=day(75), end=day(75), recovered_on=day(77)),
        ],
        metric,
    )
    totals = restatements(wh, metric, day(60), day(83))
    assert [r.metric_date for r in totals] == [day(70), day(75)]
    assert totals[1].first_reported == 0.0 and totals[1].latest > 0
    regions = restatements(wh, metric, day(60), day(72), "REGION")
    assert [(r.metric_date, r.dim_value, r.first_reported) for r in regions] == [(day(70), "NA", 0.0)]
