"""Scenario definition: the business events and data incidents injected into the simulated company.

Every event carries its ``nature`` (a real business change vs. a data incident). That label is the
ground truth Causa is evaluated against; Causa itself never reads it.
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Nature(StrEnum):
    BUSINESS = "business"
    DATA = "data"


class _Event(BaseModel):
    event_id: str
    start: date
    end: date | None = None  # inclusive; None means permanent
    description: str = ""

    def active(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


# --- Real business changes -------------------------------------------------------------------


class CustomerChurn(_Event):
    kind: Literal["customer_churn"] = "customer_churn"
    nature: Literal[Nature.BUSINESS] = Nature.BUSINESS
    customer_name: str


class DemandShift(_Event):
    """Multiplies ordering propensity of customers matching every given attribute."""

    kind: Literal["demand_shift"] = "demand_shift"
    nature: Literal[Nature.BUSINESS] = Nature.BUSINESS
    region: str | None = None
    country: str | None = None
    segment: str | None = None
    multiplier: float


class Promotion(_Event):
    kind: Literal["promotion"] = "promotion"
    nature: Literal[Nature.BUSINESS] = Nature.BUSINESS
    category: str
    extra_discount_pct: float
    volume_multiplier: float


class PriceChange(_Event):
    kind: Literal["price_change"] = "price_change"
    nature: Literal[Nature.BUSINESS] = Nature.BUSINESS
    category: str
    pct: float


class MarketLaunch(_Event):
    kind: Literal["market_launch"] = "market_launch"
    nature: Literal[Nature.BUSINESS] = Nature.BUSINESS
    country: str
    new_customers: int


# --- Data incidents ---------------------------------------------------------------------------


class SchemaDriftStall(_Event):
    """Upstream renames ORDER_TS -> ORDER_TS_UTC. The connector keeps reporting success, but the
    staging guard ``ORDER_TS IS NOT NULL`` silently drops the feed's rows until it is fixed."""

    kind: Literal["schema_drift_stall"] = "schema_drift_stall"
    nature: Literal[Nature.DATA] = Nature.DATA
    feed: str


class DuplicateLoad(_Event):
    """The connector retries a batch and appends order lines twice."""

    kind: Literal["duplicate_load"] = "duplicate_load"
    nature: Literal[Nature.DATA] = Nature.DATA
    feed: str


class FxCorruption(_Event):
    """The FX feed publishes a wrong rate for one currency (e.g. quoted against the wrong base)."""

    kind: Literal["fx_corruption"] = "fx_corruption"
    nature: Literal[Nature.DATA] = Nature.DATA
    currency: str
    factor: float


class NullForeignKey(_Event):
    """A share of order lines arrives without PRODUCT_ID and is lost in the product join."""

    kind: Literal["null_foreign_key"] = "null_foreign_key"
    nature: Literal[Nature.DATA] = Nature.DATA
    channel: str
    fraction: float


class LateArrival(_Event):
    """A feed's batch lands ``delay_days`` late; numbers dip, then silently self-heal."""

    kind: Literal["late_arrival"] = "late_arrival"
    nature: Literal[Nature.DATA] = Nature.DATA
    feed: str
    delay_days: int


class JobFailure(_Event):
    """The fact-table load fails; an operator re-runs it on ``recovered_on``."""

    kind: Literal["job_failure"] = "job_failure"
    nature: Literal[Nature.DATA] = Nature.DATA
    task_id: str = "fct_order_lines"
    recovered_on: date | None = None


Event = Annotated[
    CustomerChurn
    | DemandShift
    | Promotion
    | PriceChange
    | MarketLaunch
    | SchemaDriftStall
    | DuplicateLoad
    | FxCorruption
    | NullForeignKey
    | LateArrival
    | JobFailure,
    Field(discriminator="kind"),
]


class Scenario(BaseModel):
    name: str
    seed: int = 7
    start: date
    days: int
    base_orders_per_day: int = 900
    n_customers: int = 2500
    products_per_category: int = 40
    daily_trend: float = 0.0008
    events: list[Event] = Field(default_factory=list)

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]


def default_scenario(start: date = date(2026, 5, 4), days: int = 168, seed: int = 7) -> Scenario:
    """Half a year of trading with overlapping business changes and data incidents.

    The final fortnight is the headline case: EMEA revenue falls because a whale churns (real)
    *and* the EMEA feed silently stalls after an upstream schema change (data).
    """

    def d(offset: int) -> date:
        return start + timedelta(days=offset)

    events: list[Event] = [
        LateArrival(
            event_id="D1",
            start=d(40),
            end=d(40),
            feed="na_orders",
            delay_days=2,
            description="NA order batch landed two days late",
        ),
        MarketLaunch(event_id="B1", start=d(60), country="MX", new_customers=180, description="Mexico market launch"),
        JobFailure(
            event_id="D2",
            start=d(75),
            end=d(75),
            recovered_on=d(77),
            description="fct_order_lines load failed; re-run two days later",
        ),
        FxCorruption(
            event_id="D3",
            start=d(88),
            end=d(90),
            currency="JPY",
            factor=1.35,
            description="JPY rate quoted against the wrong base currency",
        ),
        Promotion(
            event_id="B2",
            start=d(100),
            end=d(113),
            category="Accessories",
            extra_discount_pct=15.0,
            volume_multiplier=1.8,
            description="Back-to-school accessories promotion",
        ),
        PriceChange(
            event_id="B3", start=d(120), category="Cloud Credits", pct=8.0, description="Cloud Credits list price +8%"
        ),
        DuplicateLoad(
            event_id="D4",
            start=d(125),
            end=d(125),
            feed="apac_orders",
            description="APAC order lines loaded twice after connector retry",
        ),
        DemandShift(
            event_id="B4",
            start=d(130),
            region="APAC",
            segment="SMB",
            multiplier=0.8,
            description="APAC SMB demand slowdown",
        ),
        NullForeignKey(
            event_id="D5",
            start=d(140),
            end=d(146),
            channel="marketplace",
            fraction=0.3,
            description="Marketplace lines missing PRODUCT_ID",
        ),
        CustomerChurn(
            event_id="B5",
            start=d(150),
            customer_name="Acme GmbH",
            description="Enterprise customer Acme GmbH did not renew",
        ),
        SchemaDriftStall(
            event_id="D6",
            start=d(155),
            end=d(160),
            feed="emea_orders",
            description="EMEA connector: ORDER_TS renamed to ORDER_TS_UTC upstream",
        ),
    ]
    return Scenario(name="northwind-default", seed=seed, start=start, days=days, events=events)
