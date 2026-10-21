"""Discovers how to monitor each table without configuration.

* load column      a timestamp the loader stamps (``LOADED_AT``, ``_FIVETRAN_SYNCED`` ...)
* segment column   a low-cardinality column naming the source (feed, connector, currency ...)
* event column     another timestamp/date, used to measure lateness
* key columns      the smallest id-like combination unique within (almost) every load day
* null columns     keys plus columns that gate rows downstream (from lineage)
* value columns    numeric columns downstream values are computed from (from lineage)
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime

from causa.health.models import TableMonitor
from causa.lineage import LineageGraph
from causa.sources.catalog import Catalog
from causa.sqlutil import ts
from causa.warehouse import Warehouse

log = logging.getLogger(__name__)

LOAD_COLUMNS = (
    "LOADED_AT",
    "_LOADED_AT",
    "INGESTED_AT",
    "_FIVETRAN_SYNCED",
    "_SDC_BATCHED_AT",
    "_AIRBYTE_EXTRACTED_AT",
    "INSERTED_AT",
)
SEGMENT_HINTS = ("SOURCE_FEED", "SOURCE", "FEED", "CONNECTOR", "SOURCE_SYSTEM", "CURRENCY", "REGION", "COUNTRY")
EXCLUDED_SCHEMAS = frozenset({"CAUSA", "SIM", "OPS", "SYS", "EXA_STATISTICS"})
MAX_SEGMENTS = 50
KEY_BAD_DAY_SHARE = 0.1  # a key may repeat on a few days: those are the incidents we look for
_TEMPORAL = ("TIMESTAMP", "DATE")
_NUMERIC = ("DECIMAL", "DOUBLE", "INTEGER", "BIGINT", "FLOAT", "NUMERIC", "REAL", "SMALLINT", "HUGEINT")


def _is(type_name: str, families: tuple[str, ...]) -> bool:
    return type_name.upper().startswith(families)


class MonitorDiscovery:
    def __init__(self, wh: Warehouse, catalog: Catalog, graph: LineageGraph) -> None:
        self._wh = wh
        self._catalog = catalog
        self._graph = graph

    def discover(self, since: datetime, until: datetime) -> list[TableMonitor]:
        monitors = []
        for table in self._catalog.tables():
            if table.split(".")[0] in EXCLUDED_SCHEMAS:
                continue
            monitor = self.discover_table(table, since, until)
            if monitor:
                monitors.append(monitor)
        return monitors

    def discover_table(self, table: str, since: datetime, until: datetime) -> TableMonitor | None:
        schema, name = table.split(".")
        columns = self._catalog.mapping[schema][name]
        load = next((c for c in LOAD_COLUMNS if c in columns and _is(columns[c], ("TIMESTAMP",))), None)
        if load is None:
            return None
        window = f"{load} >= {ts(since)} AND {load} < {ts(until)}"
        if not self._wh.scalar(f"SELECT COUNT(*) FROM {table} WHERE {window}"):
            return None

        segment = self._segment_column(table, columns, window)
        event = next((c for c in columns if c != load and _is(columns[c], _TEMPORAL)), None)
        keys = self._key_columns(table, columns, load, window, exclude={segment})
        if not keys and event and segment:  # e.g. one rate per (date, currency)
            keys = self._key_columns(table, columns, load, window, exclude=set(), candidates=[event, segment])
        gating, derived = self._graph.influential_columns(table)
        nulls = [c for c in columns if (c in gating or c in keys) and c != load]
        values = [
            c
            for c in columns
            if c in derived and _is(columns[c], _NUMERIC) and c not in keys and c not in gating and c != segment
        ]
        return TableMonitor(table, load, segment, event, keys, nulls, values)

    def _segment_column(self, table: str, columns: dict[str, str], window: str) -> str | None:
        for hint in SEGMENT_HINTS:
            if hint in columns:
                distinct = self._wh.scalar(f"SELECT COUNT(DISTINCT {hint}) FROM {table} WHERE {window}")
                if distinct and 1 < int(distinct) <= MAX_SEGMENTS:  # type: ignore[call-overload]
                    return hint
        return None

    def _key_columns(
        self,
        table: str,
        columns: dict[str, str],
        load: str,
        window: str,
        exclude: set[str | None],
        candidates: list[str] | None = None,
    ) -> list[str]:
        """Smallest id-like combination that is unique within (almost) every load day.

        Judged per day rather than over the whole window, so that a duplicate load inside the window
        cannot hide the very key needed to detect it."""
        if candidates is None:
            candidates = [
                c for c in columns if c not in exclude and (c == "ID" or c.endswith(("_ID", "_NO", "_KEY", "_NUMBER")))
            ][:8]
        day = f"CAST({load} AS DATE)"
        days = int(self._wh.scalar(f"SELECT COUNT(DISTINCT {day}) FROM {table} WHERE {window}") or 0)  # type: ignore[call-overload]
        for size in (1, 2, 3):
            for combo in itertools.combinations(candidates, size):
                cols = ", ".join(combo)
                bad_days = self._wh.scalar(
                    f"""SELECT COUNT(DISTINCT D) FROM (
                        SELECT {day} AS D FROM {table} WHERE {window}
                        GROUP BY {day}, {cols} HAVING COUNT(*) > 1) k"""
                )
                if days and int(bad_days or 0) <= KEY_BAD_DAY_SHARE * days:  # type: ignore[call-overload]
                    return list(combo)
        log.info("no key found for %s", table)
        return []
