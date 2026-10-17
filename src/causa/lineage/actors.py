"""Identifies *who* issued a statement from the session's client string and user.

Tools announce themselves through the driver's client name, e.g.
``Tableau Server 2025.1; workbook=Executive Revenue`` or ``Airflow 2.10; dag=sales_mart``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class ActorKind(StrEnum):
    SOURCE = "source"  # ingestion connector
    PIPELINE = "pipeline"  # orchestrated transformation / ML job
    DEPLOYMENT = "deployment"
    DASHBOARD = "dashboard"
    SCHEDULED_REPORT = "scheduled_report"
    REVERSE_ETL = "reverse_etl"  # pushes warehouse data into operational systems
    SPREADSHEET = "spreadsheet"
    AD_HOC = "ad_hoc"


# Attribute keys that name the asset, in priority order, and what kind of actor they imply.
_PRIMARY_KEYS: tuple[tuple[str, ActorKind], ...] = (
    ("connector", ActorKind.SOURCE),
    ("dag", ActorKind.PIPELINE),
    ("job", ActorKind.DEPLOYMENT),
    ("report", ActorKind.SCHEDULED_REPORT),
    ("sync", ActorKind.REVERSE_ETL),
    ("workbook", ActorKind.DASHBOARD),
    ("dashboard", ActorKind.DASHBOARD),
)
_SPREADSHEET_TOOLS = ("excel", "google sheets")
_VERSION = re.compile(r"\s+v?\d[\w.\-]*$")


@dataclass(frozen=True)
class Actor:
    key: str
    kind: ActorKind
    name: str
    tool: str
    attributes: dict[str, str] = field(default_factory=dict, compare=False, hash=False)

    @property
    def node_id(self) -> str:
        return f"actor:{self.key}"


def parse_client(client: str | None, user: str | None) -> Actor:
    parts = [p.strip() for p in (client or "").split(";") if p.strip()]
    tool = _VERSION.sub("", parts[0]) if parts else "unknown"
    attrs = dict(p.split("=", 1) for p in parts[1:] if "=" in p)

    for key, kind in _PRIMARY_KEYS:
        if key in attrs:
            if kind is ActorKind.DASHBOARD and any(t in tool.lower() for t in _SPREADSHEET_TOOLS):
                kind = ActorKind.SPREADSHEET
            return Actor(f"{tool}/{key}={attrs[key]}", kind, attrs[key], tool, attrs)

    who = (user or "unknown").upper()
    return Actor(f"{tool}/user={who}", ActorKind.AD_HOC, f"{who} via {tool}", tool, attrs)
