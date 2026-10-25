from __future__ import annotations

import pytest
import yaml

from causa.metrics.definitions import CausaConfig
from causa.onboarding import propose, render
from causa.simulator.runner import run_simulation
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from tests.conftest import small_scenario


@pytest.fixture(scope="module")
def wh():
    wh = DuckDBWarehouse(":memory:")
    run_simulation(small_scenario(days=10), wh)
    yield wh
    wh.close()


def test_init_finds_the_revenue_fact_without_being_told(wh) -> None:
    proposed, ranked = propose(wh, limit=3)
    assert all(not c.table.startswith(("SIM.", "OPS.", "CAUSA")) for c in ranked)  # never ground truth
    best = proposed[0]
    assert (best.table, best.measure_column, best.time_column) == (
        "MART.FCT_ORDER_LINES",
        "NET_REVENUE_EUR",
        "ORDER_DATE",
    )
    assert (best.name, best.label, best.unit, best.volume_column) == ("net_revenue", "Net revenue", "EUR", "ORDER_ID")
    assert {"REGION", "SEGMENT", "CHANNEL", "CATEGORY"} <= set(best.dimensions)
    assert "CUSTOMER_NAME" in best.deep_dimensions
    # different measures, unique names: not the same revenue again at a daily or monthly grain
    assert len({m.measure_column for m in proposed}) == len({m.name for m in proposed}) == len(proposed) == 3

    # what it writes is a valid configuration Causa can load
    text = render(proposed, {"readable": False, "reason": "x"})
    assert text.startswith("# Proposed by `causa init`") and "NOT readable" in text
    config = CausaConfig.model_validate(yaml.safe_load(text))
    assert config.metric("net_revenue").dimensions == best.dimensions
