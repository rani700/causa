"""Generates what *actually* happened each day, and separately what *landed* in the warehouse.

Keeping the two apart is what makes the simulator a benchmark: the true numbers become ground truth,
and the gap between true and landed data is exactly the effect of the injected data incidents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np
import polars as pl

from causa.simulator import world
from causa.simulator.scenario import (
    CustomerChurn,
    DemandShift,
    DuplicateLoad,
    FxCorruption,
    LateArrival,
    MarketLaunch,
    NullForeignKey,
    PriceChange,
    Promotion,
    Scenario,
    SchemaDriftStall,
)

WEEKDAY_FACTOR = np.array([1.10, 1.15, 1.10, 1.05, 1.00, 0.45, 0.35])
CANCEL_RATE = 0.03
FEED_LOAD_OFFSET_MIN = {"emea_orders": 5, "na_orders": 20, "apac_orders": 35, "latam_orders": 50}


@dataclass(frozen=True)
class TrueDay:
    day: date
    orders: pl.DataFrame
    lines: pl.DataFrame
    fx: pl.DataFrame
    revenue: pl.DataFrame  # true revenue at MART.DAILY_REVENUE grain


@dataclass(frozen=True)
class Landing:
    """Rows as delivered by connectors, stamped with LOADED_AT, after incidents were applied."""

    orders: pl.DataFrame
    lines: pl.DataFrame
    fx: pl.DataFrame


class CompanyGenerator:
    def __init__(self, scenario: Scenario) -> None:
        self.s = scenario
        rng = np.random.default_rng(scenario.seed)

        customers = world.build_customers(rng, scenario.n_customers, scenario.start)
        next_id = int(customers["customer_id"].max()) + 1  # type: ignore[arg-type]
        for launch in scenario.of(MarketLaunch):
            extra = world.build_new_market_customers(rng, launch.country, launch.new_customers, next_id, launch.start)
            customers = pl.concat([customers, extra])
            next_id += launch.new_customers
        self.customers = customers
        self.products = world.build_products(rng, scenario.products_per_category)

        self._cust_id = customers["customer_id"].to_numpy()
        self._cust_seg = np.array([world.SEGMENTS.index(s) for s in customers["segment"]])
        self._cust_region = customers["region"].to_numpy().astype(str)
        self._cust_country = customers["country"].to_numpy().astype(str)
        self._cust_signup = np.array([d.toordinal() for d in customers["signup_date"]])
        self._cust_weight = customers["weight"].to_numpy()
        self._cust_currency = np.array([world.COUNTRY_BY_CODE[c].currency for c in self._cust_country])
        self._cust_feed = np.array([world.feed_for_region(r) for r in self._cust_region])
        self._name_to_idx = {n: i for i, n in enumerate(customers["customer_name"])}

        self._prod_id = self.products["product_id"].to_numpy()
        self._prod_cat = self.products["category"].to_numpy().astype(str)
        self._prod_price = self.products["list_price_eur"].to_numpy()
        self._prod_pop = self.products["popularity"].to_numpy()

        initially_active = self._cust_signup <= scenario.start.toordinal()
        self._orders_per_weight = scenario.base_orders_per_day / self._cust_weight[initially_active].sum()
        self._fx = self._fx_paths(rng)
        self._next_order_id = 70_000_001

    # -- truth -----------------------------------------------------------------------------

    def _fx_paths(self, rng: np.random.Generator) -> dict[str, np.ndarray]:
        paths = {}
        for cur, base in world.FX_BASE_TO_EUR.items():
            if cur == "EUR":
                paths[cur] = np.ones(self.s.days)
                continue
            walk = np.cumsum(rng.normal(0.0, 0.003, size=self.s.days))
            paths[cur] = np.round(base * np.exp(walk), 8)
        return paths

    def true_fx(self, day: date) -> dict[str, float]:
        t = (day - self.s.start).days
        return {cur: float(path[t]) for cur, path in self._fx.items()}

    def _customer_weights(self, day: date) -> np.ndarray:
        w = self._cust_weight.copy()
        w[self._cust_signup > day.toordinal()] = 0.0
        for churn in self.s.of(CustomerChurn):
            if churn.active(day):
                w[self._name_to_idx[churn.customer_name]] = 0.0
        for shift in self.s.of(DemandShift):
            if shift.active(day):
                mask = np.ones_like(w, dtype=bool)
                if shift.region:
                    mask &= self._cust_region == shift.region
                if shift.country:
                    mask &= self._cust_country == shift.country
                if shift.segment:
                    mask &= world.SEGMENTS.index(shift.segment) == self._cust_seg
                w[mask] *= shift.multiplier
        return w

    def generate(self, day: date) -> TrueDay:
        rng = np.random.default_rng([self.s.seed, day.toordinal()])
        t = (day - self.s.start).days

        w = self._customer_weights(day)
        month_end = (day + timedelta(days=2)).month != day.month
        seasonal = (
            WEEKDAY_FACTOR[day.weekday()]
            * (1.0 + self.s.daily_trend) ** t
            * (1.25 if month_end else 1.0)
            * rng.lognormal(0.0, 0.05)
        )
        n_orders = int(rng.poisson(self._orders_per_weight * w.sum() * seasonal))
        cust = rng.choice(w.size, size=n_orders, p=w / w.sum())
        seg = self._cust_seg[cust]

        cum_channel = world.SEGMENT_CHANNEL_P.cumsum(axis=1)[seg]
        channel_idx = (rng.random(n_orders)[:, None] > cum_channel).sum(axis=1)
        order_ids = np.arange(self._next_order_id, self._next_order_id + n_orders, dtype=np.int64)
        self._next_order_id += n_orders
        midnight = np.datetime64(datetime.combine(day, time()), "us")
        order_ts = midnight + (rng.integers(6 * 3600, 22 * 3600, size=n_orders) * 1_000_000).astype("timedelta64[us]")
        cancelled = rng.random(n_orders) < CANCEL_RATE

        orders = pl.DataFrame(
            {
                "order_id": order_ids,
                "customer_id": self._cust_id[cust],
                "order_ts": order_ts,
                "channel": np.array(world.CHANNELS)[channel_idx],
                "currency": self._cust_currency[cust],
                "order_status": np.where(cancelled, "CANCELLED", "CONFIRMED"),
                "source_feed": self._cust_feed[cust],
            }
        )

        n_lines = 1 + rng.poisson(world.SEGMENT_LINES_LAMBDA[seg])
        line_order = np.repeat(np.arange(n_orders), n_lines)
        starts = np.repeat(np.cumsum(n_lines) - n_lines, n_lines)
        line_no = np.arange(line_order.size) - starts + 1

        pop = self._prod_pop.copy()
        price = self._prod_price.copy()
        extra_discount = np.zeros_like(price)
        for promo in self.s.of(Promotion):
            if promo.active(day):
                in_cat = self._prod_cat == promo.category
                pop[in_cat] *= promo.volume_multiplier
                extra_discount[in_cat] += promo.extra_discount_pct
        for change in self.s.of(PriceChange):
            if change.active(day):
                price[self._prod_cat == change.category] *= 1.0 + change.pct / 100.0
        prod = rng.choice(pop.size, size=line_order.size, p=pop / pop.sum())

        line_seg = seg[line_order]
        quantity = 1 + rng.poisson(world.SEGMENT_QTY_LAMBDA[line_seg])
        fx_today = self.true_fx(day)
        fx_line = np.array([fx_today[c] for c in self._cust_currency[cust]])[line_order]
        price_eur = price[prod] * rng.uniform(0.98, 1.02, size=prod.size)
        unit_price = np.round(price_eur / fx_line, 4)
        discount = np.round(
            np.clip(rng.normal(world.SEGMENT_DISCOUNT_MU[line_seg], 2.0), 0.0, 35.0) + extra_discount[prod], 2
        )

        lines = pl.DataFrame(
            {
                "order_id": order_ids[line_order],
                "line_no": line_no.astype(np.int64),
                "product_id": self._prod_id[prod],
                "quantity": quantity.astype(np.int64),
                "unit_price": unit_price,
                "discount_pct": discount,
                "source_feed": self._cust_feed[cust][line_order],
            }
        )
        fx = pl.DataFrame(
            {"rate_date": [day] * len(fx_today), "currency": list(fx_today), "rate_to_eur": list(fx_today.values())}
        )
        return TrueDay(day, orders, lines, fx, self._true_revenue(day, orders, lines, fx_today))

    def _true_revenue(self, day: date, orders: pl.DataFrame, lines: pl.DataFrame, fx: dict[str, float]) -> pl.DataFrame:
        cust = self.customers.select("customer_id", "segment", "region", "country")
        prod = self.products.select("product_id", "category")
        rates = pl.DataFrame({"currency": list(fx), "rate": list(fx.values())})
        return (
            lines.join(orders.filter(pl.col("order_status") != "CANCELLED"), on="order_id")
            .join(cust, on="customer_id")
            .join(prod, on="product_id")
            .join(rates, on="currency")
            .with_columns(
                net=pl.col("quantity") * pl.col("unit_price") * pl.col("rate") * (1 - pl.col("discount_pct") / 100)
            )
            .group_by("region", "country", "segment", "channel", "category")
            .agg(
                orders=pl.col("order_id").n_unique(),
                units=pl.col("quantity").sum(),
                net_revenue_eur=pl.col("net").sum(),
            )
            .with_columns(order_date=pl.lit(day))
            .select(
                "order_date",
                "region",
                "country",
                "segment",
                "channel",
                "category",
                "orders",
                "units",
                "net_revenue_eur",
            )
        )

    # -- landing (truth + incidents) -------------------------------------------------------

    def drift_started(self, day: date) -> bool:
        return any(e.start <= day for e in self.s.of(SchemaDriftStall))

    def land(self, true_day: TrueDay) -> Landing:
        day = true_day.day
        rng = np.random.default_rng([self.s.seed, day.toordinal(), 1])
        night = datetime.combine(day + timedelta(days=1), time(1, 0))

        orders = true_day.orders.with_columns(
            loaded_at=pl.col("source_feed").replace_strict(
                {f: night + timedelta(minutes=m) for f, m in FEED_LOAD_OFFSET_MIN.items()},
                return_dtype=pl.Datetime("us"),
            )
        )
        lines = true_day.lines.with_columns(
            loaded_at=pl.col("source_feed").replace_strict(
                {f: night + timedelta(minutes=m + 2) for f, m in FEED_LOAD_OFFSET_MIN.items()},
                return_dtype=pl.Datetime("us"),
            )
        )
        fx = true_day.fx.with_columns(loaded_at=pl.lit(night - timedelta(minutes=30)))

        for ev in self.s.events:
            if not ev.active(day):
                continue
            match ev:
                case LateArrival(feed=feed, delay_days=delay):
                    shift = pl.duration(days=delay)
                    orders = orders.with_columns(
                        loaded_at=pl.when(pl.col("source_feed") == feed)
                        .then(pl.col("loaded_at") + shift)
                        .otherwise(pl.col("loaded_at"))
                    )
                    lines = lines.with_columns(
                        loaded_at=pl.when(pl.col("source_feed") == feed)
                        .then(pl.col("loaded_at") + shift)
                        .otherwise(pl.col("loaded_at"))
                    )
                case DuplicateLoad(feed=feed):
                    retry = lines.filter(pl.col("source_feed") == feed).with_columns(
                        loaded_at=pl.col("loaded_at") + pl.duration(minutes=25)
                    )
                    lines = pl.concat([lines, retry])
                case NullForeignKey(channel=channel, fraction=fraction):
                    affected = orders.filter(pl.col("channel") == channel)["order_id"]
                    hit = pl.Series(rng.random(lines.height) < fraction)
                    lines = lines.with_columns(
                        product_id=pl.when(pl.col("order_id").is_in(affected.implode()) & hit)
                        .then(None)
                        .otherwise(pl.col("product_id"))
                    )
                case FxCorruption(currency=currency, factor=factor):
                    fx = fx.with_columns(
                        rate_to_eur=pl.when(pl.col("currency") == currency)
                        .then((pl.col("rate_to_eur") * factor).round(8))
                        .otherwise(pl.col("rate_to_eur"))
                    )
                case _:
                    pass

        if self.drift_started(day):
            orders = self._apply_schema_drift(orders, day)
        return Landing(orders, lines, fx)

    def _apply_schema_drift(self, orders: pl.DataFrame, day: date) -> pl.DataFrame:
        """After the upstream change the drifted feed always sends ORDER_TS_UTC; while the stall
        is active it no longer sends ORDER_TS. Other feeds leave the new column empty."""
        drifted = [e.feed for e in self.s.of(SchemaDriftStall) if e.start <= day]
        stalled = [e.feed for e in self.s.of(SchemaDriftStall) if e.active(day)]
        is_drifted = pl.col("source_feed").is_in(drifted)
        return orders.with_columns(
            order_ts_utc=pl.when(is_drifted).then(pl.col("order_ts")).otherwise(None),
            order_ts=pl.when(pl.col("source_feed").is_in(stalled)).then(None).otherwise(pl.col("order_ts")),
        )
