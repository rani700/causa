"""Onboarding a real warehouse: propose metric definitions and check what Causa can see.

``causa init`` looks for tables that read like business facts (a date and a money or quantity
column), proposes a metric for the best ones with low-cardinality columns as dimensions and named
high-cardinality ones (customers, products) as deep dimensions, and checks whether the audit log is
readable (lineage). It writes a commented ``causa.yaml`` to review, never a final answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from causa.metrics.definitions import MetricDefinition, VolumeAggregation
from causa.sources.catalog import Catalog, is_company_table
from causa.warehouse import Warehouse

NUMERIC = ("DECIMAL", "DOUBLE", "BIGINT", "INTEGER", "INT", "SMALLINT", "FLOAT", "NUMERIC", "REAL", "HUGEINT")
TEXT = ("VARCHAR", "CHAR", "STRING")
TIME = ("DATE", "TIMESTAMP")
# Measure names, best first: amounts beat prices and quantities.
MEASURE_HINTS = (
    ("REVENUE", 10), ("SALES", 9), ("AMOUNT", 9), ("GMV", 9), ("VALUE", 6), ("MARGIN", 5),
    ("COST", 4), ("QUANTITY", 4), ("QTY", 4), ("UNITS", 4), ("PRICE", 1), ("NET", 2),
)  # fmt: skip
FACT_HINTS = ("FCT", "FACT", "ORDER", "SALE", "TRANSACTION", "INVOICE", "BOOKING", "REVENUE")
CURATED_SCHEMAS = ("MART", "MARTS", "ANALYTICS", "REPORTING", "DWH", "GOLD", "CORE")
CURRENCIES = ("EUR", "USD", "GBP", "CHF", "INR", "JPY", "AUD", "CAD", "SGD")
MAX_DIMENSION_VALUES = 50
DEEP_HINTS = ("CUSTOMER", "CLIENT", "ACCOUNT", "PRODUCT", "SKU", "SUPPLIER", "STORE")


def _is(type_name: str, families: tuple[str, ...]) -> bool:
    return type_name.upper().startswith(families)


@dataclass
class Candidate:
    table: str
    time_column: str
    measure_column: str
    score: float
    volume_column: str | None = None
    dimensions: list[str] = field(default_factory=list)
    deep_dimensions: list[str] = field(default_factory=list)

    def metric(self) -> MetricDefinition:
        currency = re.search(rf"_({'|'.join(CURRENCIES)})$", self.measure_column)
        base = self.measure_column[: currency.start()] if currency else self.measure_column
        noun = (self.volume_column or "").removesuffix("_ID").lower()
        return MetricDefinition(
            name=base.lower(),
            label=base.replace("_", " ").lower().capitalize(),
            unit=currency.group(1) if currency else "",
            table=self.table,
            time_column=self.time_column,
            measure_column=self.measure_column,
            volume_column=self.volume_column,
            volume_aggregation=VolumeAggregation.COUNT_DISTINCT,
            volume_label=f"{noun}s" if noun else "units",
            dimensions=self.dimensions,
            deep_dimensions=self.deep_dimensions,
        )


def candidates(catalog: Catalog) -> list[Candidate]:
    """Every (table, date, measure) that reads like a business fact, best first (no queries)."""
    found = []
    for table in catalog.tables():
        if not is_company_table(table):
            continue
        schema, name = table.split(".")
        columns = catalog.mapping[schema][name]
        times = [c for c, t in columns.items() if _is(t, TIME)]
        # A measure scores the sum of its hints: NET_REVENUE beats GROSS_REVENUE beats UNIT_PRICE.
        measures = [
            (c, sum(weight for hint, weight in MEASURE_HINTS if hint in c))
            for c, t in columns.items()
            if _is(t, NUMERIC) and not c.endswith("_ID")
        ]
        measures = [m for m in measures if m[1] > 0]
        if not times or not measures:
            continue
        time = sorted(times, key=lambda c: (not _is(columns[c], ("DATE",)) or "TS" in c, "DATE" not in c))[0]
        ids = [c for c in columns if c.endswith("_ID")]
        volume = next((c for h in ("ORDER", "TRANSACTION", "INVOICE", "BOOKING") for c in ids if h in c), None)
        # Line-level fact tables beat pre-aggregated ones: their ids allow the volume/rate split and
        # drilling down to customers and products.
        table_score = (
            4 * any(h in name for h in ("FCT", "FACT"))
            + 2 * any(h in name for h in FACT_HINTS)
            + 2 * (schema in CURATED_SCHEMAS)
            + 3 * (volume is not None)
        )
        found += [Candidate(table, time, measure, weight + table_score, volume) for measure, weight in measures]
    return sorted(found, key=lambda c: -c.score)


def profile_dimensions(wh: Warehouse, catalog: Catalog, c: Candidate) -> Candidate:
    """Low-cardinality text columns become dimensions; named high-cardinality ones deep dimensions.
    One query per table."""
    schema, name = c.table.split(".")
    texts = [col for col, t in catalog.mapping[schema][name].items() if _is(t, TEXT)]
    if not texts:
        return c
    counts = wh.query(
        "SELECT " + ", ".join(f"COUNT(DISTINCT {col}) AS N{i}" for i, col in enumerate(texts)) + f" FROM {c.table}"
    ).row(0)
    distinct = dict(zip(texts, counts, strict=True))
    c.dimensions = [col for col in texts if 2 <= (distinct[col] or 0) <= MAX_DIMENSION_VALUES][:6]
    c.deep_dimensions = [
        col for col in texts if (distinct[col] or 0) > MAX_DIMENSION_VALUES and any(h in col for h in DEEP_HINTS)
    ][:2]
    return c


def audit_access(wh: Warehouse) -> dict[str, Any]:
    """Can Causa read Exasol's audit log (lineage, consumers)? How much is there?"""
    try:
        n = wh.scalar(
            "SELECT COUNT(*) FROM EXA_STATISTICS.EXA_DBA_AUDIT_SQL WHERE START_TIME > ADD_DAYS(CURRENT_TIMESTAMP, -7)"
        )
    except Exception as exc:
        return {"readable": False, "reason": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"}
    return {"readable": True, "statements_last_7_days": int(n or 0)}  # type: ignore[call-overload]


def propose(wh: Warehouse, limit: int = 3) -> tuple[list[MetricDefinition], list[Candidate]]:
    """Up to ``limit`` metrics, each a different measure taken from its best table (the same
    revenue column in a daily or monthly roll-up is the same metric at a coarser grain)."""
    catalog = Catalog.load(wh)
    ranked = candidates(catalog)
    profiled: dict[str, Candidate] = {}
    best: list[MetricDefinition] = []
    for c in ranked:
        if any(m.measure_column == c.measure_column for m in best):
            continue
        if c.table not in profiled:
            profiled[c.table] = profile_dimensions(wh, catalog, Candidate(c.table, c.time_column, "", 0))
        p = profiled[c.table]
        c.dimensions, c.deep_dimensions = p.dimensions, p.deep_dimensions
        if not c.dimensions:
            continue
        metric = c.metric()
        if any(m.name == metric.name for m in best):
            metric.name = f"{metric.name}_{c.table.split('.')[1].lower()}"
        best.append(metric)
        if len(best) == limit:
            break
    return best, ranked


def render(metrics: list[MetricDefinition], audit: dict[str, Any] | None = None) -> str:
    header = [
        "# Proposed by `causa init`. Review before use: names, units, dimensions and filters are guesses",
        "# from column names and value counts. SQL identifiers use the Exasol dialect.",
    ]
    if audit is not None:
        if audit.get("readable"):
            header.append(
                f"# Audit log readable ({audit['statements_last_7_days']:,} statements in 7 days): "
                "use --query-log audit for lineage."
            )
        else:
            header.append(
                "# Audit log NOT readable: lineage needs auditing enabled and SELECT ANY DICTIONARY for this user."
            )
    body = yaml.safe_dump(
        {"metrics": [m.model_dump(mode="json", exclude_none=True, exclude_defaults=True) for m in metrics]},
        sort_keys=False,
        default_flow_style=None,
        width=110,
    )
    return "\n".join(header) + "\n" + body
