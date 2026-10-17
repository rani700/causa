from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from causa.warehouse import Warehouse


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
