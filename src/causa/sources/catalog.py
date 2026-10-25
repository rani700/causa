from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from causa import store
from causa.warehouse import Warehouse

# Schemas that hold no company data flow: Exasol's system objects, Causa's own state, and the
# simulator's bookkeeping (its query log and the ground truth Causa must never see).
NOT_COMPANY_SCHEMAS = store.SCHEMAS | {"SYS", "EXA_STATISTICS", "SIM", "OPS"}


def is_company_table(name: str) -> bool:
    """False for system objects (incl. unqualified EXA_* views and $-internal tables) and the above."""
    schema, _, table = name.upper().rpartition(".")
    if schema in NOT_COMPANY_SCHEMAS or schema.startswith("$") or table.startswith("$"):
        return False
    return bool(schema) or not table.startswith("EXA_")


@dataclass
class Catalog:
    """Column metadata keyed by ``SCHEMA -> TABLE -> {COLUMN: TYPE}`` (insertion order = ordinal order)."""

    mapping: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, df: pl.DataFrame) -> Catalog:
        mapping: dict[str, dict[str, dict[str, str]]] = {}
        for row in df.sort("table_schema", "table_name", "ordinal_position").iter_rows(named=True):
            table = mapping.setdefault(row["table_schema"], {}).setdefault(row["table_name"], {})
            table[row["column_name"]] = row["data_type"]
        return cls(mapping)

    @classmethod
    def load(cls, wh: Warehouse) -> Catalog:
        return cls.from_frame(wh.columns())

    def columns(self, table: str) -> list[str]:
        schema, _, name = table.upper().rpartition(".")
        return list(self.mapping.get(schema, {}).get(name, {}))

    def has(self, table: str) -> bool:
        return bool(self.columns(table))

    def tables(self) -> list[str]:
        return [f"{s}.{t}" for s, tables in self.mapping.items() for t in tables]
