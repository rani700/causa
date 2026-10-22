"""Repairs: how to turn the rows an incident damaged into what they should have been.

Each repair produces a ``SELECT`` over the damaged table returning every column, repaired, for the
affected load window. Repairs are inferred from the evidence the health engine collected, never
configured by hand, and each one says exactly what it assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from causa.metrics.attribution import _quote
from causa.sqlutil import dt


@dataclass
class AuxTable:
    """A repaired copy of another table the replay must read instead of the original."""

    original: str
    select: str


@dataclass
class Repair:
    kind: str
    table: str
    description: str
    aux: list[AuxTable] = field(default_factory=list)

    def select(self, columns: list[str], window: str) -> str:
        return f"SELECT {', '.join(columns)} FROM {self.table} WHERE {window}"


@dataclass
class Dedupe(Repair):
    keys: list[str] = field(default_factory=list)
    load_column: str = "LOADED_AT"

    def select(self, columns: list[str], window: str) -> str:
        cols = ", ".join(columns)
        return (
            f"SELECT {cols} FROM (SELECT {cols}, ROW_NUMBER() OVER (PARTITION BY {', '.join(self.keys)} "
            f"ORDER BY {self.load_column}) AS CAUSA_RN FROM {self.table} WHERE {window}) x WHERE CAUSA_RN = 1"
        )


@dataclass
class CoalesceRenamed(Repair):
    column: str = ""
    replacement: str = ""

    def select(self, columns: list[str], window: str) -> str:
        cols = [f"COALESCE({c}, {self.replacement}) AS {c}" if c == self.column else c for c in columns]
        return f"SELECT {', '.join(cols)} FROM {self.table} WHERE {window}"


@dataclass
class QuarantineKey(Repair):
    column: str = ""
    sentinel: str = "-1"  # SQL literal

    def select(self, columns: list[str], window: str) -> str:
        cols = [f"COALESCE({c}, {self.sentinel}) AS {c}" if c == self.column else c for c in columns]
        return f"SELECT {', '.join(cols)} FROM {self.table} WHERE {window}"


@dataclass
class Interpolate(Repair):
    value_column: str = ""
    segment_column: str = ""
    segment_value: str = ""
    event_column: str = ""
    replacements: dict[date, float] = field(default_factory=dict)

    def select(self, columns: list[str], window: str) -> str:
        whens = " ".join(
            f"WHEN {self.segment_column} = '{_quote(self.segment_value)}' AND CAST({self.event_column} AS DATE) = "
            f"{dt(d)} THEN {v!r}"
            for d, v in sorted(self.replacements.items())
        )
        cols = [f"CASE {whens} ELSE {c} END AS {c}" if c == self.value_column and whens else c for c in columns]
        return f"SELECT {', '.join(cols)} FROM {self.table} WHERE {window}"


def sentinel_for(type_name: str) -> str:
    upper = type_name.upper()
    if upper.startswith(("VARCHAR", "CHAR")):
        return "'(unknown)'"
    if upper.startswith(("DATE", "TIMESTAMP")):
        return "NULL"
    return "-1"


def quarantine_dimension(table: str, key: str, columns: dict[str, str]) -> AuxTable:
    """The dimension plus one ``(unknown)`` member, so rows with a missing key survive the join."""
    values = []
    for column, type_name in columns.items():
        if column == key:
            values.append(f"{sentinel_for(type_name)} AS {column}")
        elif type_name.upper().startswith(("VARCHAR", "CHAR")):
            values.append(f"'(unknown)' AS {column}")
        else:
            values.append(f"CAST(NULL AS {type_name}) AS {column}")
    cols = ", ".join(columns)
    return AuxTable(table, f"SELECT {cols} FROM {table} UNION ALL SELECT {', '.join(values)}")
