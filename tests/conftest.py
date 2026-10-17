from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date

import pytest

from causa.simulator.runner import SimulationResult, run_simulation
from causa.simulator.scenario import Event, Scenario
from causa.warehouse.duckdb_backend import DuckDBWarehouse

START = date(2026, 3, 2)

Simulate = Callable[..., tuple[DuckDBWarehouse, SimulationResult]]


def small_scenario(events: list[Event] | None = None, days: int = 12) -> Scenario:
    return Scenario(
        name="test",
        seed=11,
        start=START,
        days=days,
        base_orders_per_day=120,
        n_customers=400,
        products_per_category=8,
        events=events or [],
    )


@pytest.fixture
def simulate() -> Iterator[Simulate]:
    opened: list[DuckDBWarehouse] = []

    def _run(events: list[Event] | None = None, days: int = 12) -> tuple[DuckDBWarehouse, SimulationResult]:
        wh = DuckDBWarehouse(":memory:")
        opened.append(wh)
        return wh, run_simulation(small_scenario(events, days), wh)

    yield _run
    for wh in opened:
        wh.close()
