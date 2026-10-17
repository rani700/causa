"""Static reference data for the simulated company ("Northwind Systems", a B2B tech reseller)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl


@dataclass(frozen=True)
class Country:
    code: str
    region: str
    currency: str
    share: float  # share of customers within the region
    company_suffix: str


COUNTRIES: tuple[Country, ...] = (
    Country("DE", "EMEA", "EUR", 0.32, "GmbH"),
    Country("FR", "EMEA", "EUR", 0.20, "SAS"),
    Country("GB", "EMEA", "GBP", 0.26, "Ltd"),
    Country("NL", "EMEA", "EUR", 0.12, "B.V."),
    Country("ES", "EMEA", "EUR", 0.10, "S.L."),
    Country("US", "NA", "USD", 0.82, "Inc."),
    Country("CA", "NA", "CAD", 0.18, "Inc."),
    Country("JP", "APAC", "JPY", 0.34, "KK"),
    Country("AU", "APAC", "AUD", 0.26, "Pty Ltd"),
    Country("IN", "APAC", "INR", 0.24, "Pvt Ltd"),
    Country("SG", "APAC", "SGD", 0.16, "Pte Ltd"),
    Country("BR", "LATAM", "BRL", 1.00, "Ltda"),
    # Not sold in at simulation start; activated by a MarketLaunch event.
    Country("MX", "LATAM", "MXN", 0.00, "S.A. de C.V."),
)
COUNTRY_BY_CODE = {c.code: c for c in COUNTRIES}

REGION_SHARE = {"EMEA": 0.38, "NA": 0.34, "APAC": 0.20, "LATAM": 0.08}
REGIONS = tuple(REGION_SHARE)


def feed_for_region(region: str) -> str:
    return f"{region.lower()}_orders"


FX_BASE_TO_EUR = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
    "CAD": 0.68,
    "JPY": 0.0062,
    "AUD": 0.61,
    "INR": 0.011,
    "SGD": 0.69,
    "BRL": 0.17,
    "MXN": 0.051,
}

SEGMENTS = ("Enterprise", "Mid-Market", "SMB")
SEGMENT_SHARE = np.array([0.05, 0.25, 0.70])
SEGMENT_WEIGHT = np.array([12.0, 3.0, 1.0])  # relative ordering frequency
SEGMENT_LINES_LAMBDA = np.array([3.0, 1.5, 0.6])
SEGMENT_QTY_LAMBDA = np.array([3.0, 1.0, 0.3])
SEGMENT_DISCOUNT_MU = np.array([12.0, 6.0, 2.0])

CHANNELS = ("direct_sales", "partner", "web", "marketplace")
SEGMENT_CHANNEL_P = np.array(
    [
        [0.70, 0.25, 0.05, 0.00],  # Enterprise
        [0.30, 0.40, 0.20, 0.10],  # Mid-Market
        [0.02, 0.18, 0.50, 0.30],  # SMB
    ]
)

CATEGORIES: dict[str, tuple[float, float, tuple[str, ...]]] = {
    # category: (median list price EUR, price dispersion, product nouns)
    "Hardware": (850.0, 0.7, ("Server", "Workstation", "Switch", "Storage Array", "Laptop", "Firewall")),
    "Software Licenses": (420.0, 0.8, ("Suite", "Analytics Seat", "Security License", "IDE Seat")),
    "Cloud Credits": (1000.0, 0.3, ("Compute Credits", "Storage Credits", "Data Credits")),
    "Services": (1500.0, 0.6, ("Implementation Day", "Health Check", "Migration Package", "Training")),
    "Accessories": (45.0, 0.6, ("Dock", "Headset", "Keyboard", "Monitor Arm", "Cable Kit", "Webcam")),
}

# Named enterprise accounts; heavy hitters that make churn visible in the numbers.
WHALES = (
    ("Acme GmbH", "DE"),
    ("Globex Corporation Inc.", "US"),
    ("Initech KK", "JP"),
)

_NAME_A = (
    "Blue",
    "North",
    "Bright",
    "Silver",
    "Rapid",
    "Prime",
    "Urban",
    "Polar",
    "Vertex",
    "Crescent",
    "Summit",
    "Harbor",
    "Iron",
    "Golden",
    "Atlas",
    "Nimbus",
    "Cedar",
    "Quantum",
    "Lumen",
    "Delta",
)
_NAME_B = (
    "Logistics",
    "Analytics",
    "Foods",
    "Health",
    "Robotics",
    "Media",
    "Energy",
    "Retail",
    "Labs",
    "Finance",
    "Systems",
    "Mobility",
    "Pharma",
    "Textiles",
    "Studios",
    "Networks",
    "Holdings",
)


def build_customers(rng: np.random.Generator, n: int, sim_start: date) -> pl.DataFrame:
    """Customers in all launched markets. Weights are the relative propensity to order."""
    regions = rng.choice(REGIONS, size=n, p=list(REGION_SHARE.values()))
    countries = np.empty(n, dtype=object)
    for region in REGIONS:
        idx = np.flatnonzero(regions == region)
        cs = [c for c in COUNTRIES if c.region == region and c.share > 0]
        countries[idx] = rng.choice([c.code for c in cs], size=idx.size, p=[c.share for c in cs])
    seg_idx = rng.choice(len(SEGMENTS), size=n, p=SEGMENT_SHARE)
    weights = SEGMENT_WEIGHT[seg_idx] * rng.lognormal(0.0, 0.6, size=n)
    signup_days = rng.integers(30, 2000, size=n)

    names = [f"{rng.choice(_NAME_A)} {rng.choice(_NAME_B)} {COUNTRY_BY_CODE[c].company_suffix}" for c in countries]
    segments = [SEGMENTS[i] for i in seg_idx]
    for i, (whale, country) in enumerate(WHALES):
        names[i], countries[i], regions[i], segments[i] = whale, country, COUNTRY_BY_CODE[country].region, "Enterprise"
        weights[i] = SEGMENT_WEIGHT[0] * 6.0

    return pl.DataFrame(
        {
            "customer_id": np.arange(100_001, 100_001 + n, dtype=np.int64),
            "customer_name": _dedupe(names),
            "segment": segments,
            "region": regions.tolist(),
            "country": countries.tolist(),
            "signup_date": [sim_start - timedelta(days=int(d)) for d in signup_days],
            "weight": weights,
        }
    )


def build_new_market_customers(
    rng: np.random.Generator, country_code: str, n: int, first_id: int, launch: date
) -> pl.DataFrame:
    country = COUNTRY_BY_CODE[country_code]
    seg_idx = rng.choice(len(SEGMENTS), size=n, p=[0.02, 0.18, 0.80])
    names = [f"{rng.choice(_NAME_A)} {rng.choice(_NAME_B)} {country.company_suffix}" for _ in range(n)]
    return pl.DataFrame(
        {
            "customer_id": np.arange(first_id, first_id + n, dtype=np.int64),
            "customer_name": _dedupe(names),
            "segment": [SEGMENTS[i] for i in seg_idx],
            "region": [country.region] * n,
            "country": [country.code] * n,
            "signup_date": [launch] * n,
            "weight": SEGMENT_WEIGHT[seg_idx] * rng.lognormal(0.0, 0.6, size=n),
        }
    )


def build_products(rng: np.random.Generator, per_category: int = 40) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    pid = 5001
    for category, (median, sigma, nouns) in CATEGORIES.items():
        for i in range(per_category):
            noun = nouns[i % len(nouns)]
            rows.append(
                {
                    "product_id": pid,
                    "sku": f"{category[:3].upper()}-{pid}",
                    "product_name": f"{noun} {rng.choice(['Pro', 'Plus', 'Lite', 'X', 'Max', 'S'])} {100 + i}",
                    "category": category,
                    "list_price_eur": round(float(median * rng.lognormal(0.0, sigma)), 2),
                    # Zipf-like popularity inside each category.
                    "popularity": 1.0 / (i + 1) ** 0.8,
                }
            )
            pid += 1
    return pl.DataFrame(rows)


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for name in names:
        k = seen.get(name, 0)
        seen[name] = k + 1
        out.append(name if k == 0 else f"{name} ({k + 1})")
    return out
