"""HTTP API: the engines and the investigator agent behind one small FastAPI service."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from causa.agent import NO_KEY, Agent, Toolbox, agent_provider, make_agent
from causa.brief import morning_brief, to_slack, to_text
from causa.lineage import LineageGraph
from causa.lineage.graph import EdgeKind
from causa.sqlutil import dt
from causa.workspace import Workspace

WEB = Path(__file__).parent / "web"
MAX_CONVERSATIONS = 200  # most recent first; older ones are forgotten


class AskRequest(BaseModel):
    question: str
    conversation_id: str | None = None


class AskResponse(BaseModel):
    conversation_id: str
    answer: str
    tool_calls: list[dict[str, Any]]
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


def create_app(workspace: Workspace, agent_factory: Callable[[Toolbox], Agent] | None = None) -> FastAPI:
    app = FastAPI(title="Causa", summary="Root-cause engine for business metrics")
    lock = threading.RLock()  # one warehouse connection: serialise engine work, never model calls
    toolbox = Toolbox(workspace, lock)
    conversations: OrderedDict[str, tuple[Agent, threading.Lock]] = OrderedDict()
    registry = threading.Lock()
    new_agent = agent_factory or make_agent
    provider = agent_provider()
    agent_available = agent_factory is not None or provider is not None

    @contextmanager
    def engine() -> Iterator[None]:
        """Engine work: one at a time, on data no older than the toolbox's refresh interval."""
        with lock:
            toolbox.keep_fresh()
            yield

    def run(name: str, **arguments: Any) -> Any:
        try:
            with engine():
                return getattr(toolbox, name)(**arguments)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/status")
    def status() -> Any:
        with engine():
            metrics = toolbox.list_metrics()
            first = cast("datetime | None", workspace.query_log["start_time"].min())
        return {
            "backend": workspace.wh.name,
            "query_log": "Exasol audit log" if workspace.query_log_table == "audit" else workspace.query_log_table,
            "today": metrics["today"],
            "history_start": str(first.date()) if first else None,
            "metrics": metrics["metrics"],
            "agent_available": agent_available,
            "agent_provider": str(provider) if provider else None,
        }

    @app.get("/metrics")
    def metrics() -> Any:
        return run("list_metrics")

    @app.get("/metrics/{metric}/series")
    def series(metric: str, since: str, until: str) -> Any:
        try:
            m = workspace.config.metric(metric)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        where = f"{m.time_column} BETWEEN {dt(date.fromisoformat(since))} AND {dt(date.fromisoformat(until))}"
        if m.filter:
            where += f" AND ({m.filter})"
        sql = (
            f"SELECT {m.time_column} AS D, SUM({m.measure_column}) AS V FROM {m.table} WHERE {where} "
            f"GROUP BY {m.time_column} ORDER BY {m.time_column}"
        )
        with engine():
            frame = workspace.wh.query(sql)
        return {"metric": m.name, "unit": m.unit, "points": [[str(d), float(v or 0)] for d, v in frame.iter_rows()]}

    @app.get("/metrics/{metric}/explain")
    def explain(metric: str, current: str, baseline: str | None = None) -> Any:
        return run("explain_metric", metric=metric, current=current, baseline=baseline)

    @app.get("/metrics/{metric}/verdict")
    def verdict(metric: str, current: str, baseline: str | None = None) -> Any:
        return run("judge_change", metric=metric, current=current, baseline=baseline)

    @app.get("/metrics/{metric}/investigation")
    def investigation(metric: str, current: str, baseline: str | None = None) -> Any:
        return run("investigate", metric=metric, current=current, baseline=baseline)

    @app.get("/metrics/{metric}/recall")
    def recall(metric: str, current: str, baseline: str | None = None) -> Any:
        return run("recall_consumers", metric=metric, current=current, baseline=baseline)

    @app.get("/health/cases")
    def health(since: str, until: str, min_severity: str = "medium") -> Any:
        return run("health_cases", since=since, until=until, min_severity=min_severity)

    @app.get("/lineage-map")
    def lineage_map() -> Any:
        with engine():
            return lineage_map_dict(workspace.graph)

    @app.get("/lineage/{table}/impact")
    def impact(table: str, since: str | None = None, until: str | None = None) -> Any:
        with engine():
            result = workspace.graph.impact(
                table.upper(),
                date.fromisoformat(since) if since else None,
                date.fromisoformat(until) if until else None,
            )
        return {
            "origin": result.origin,
            "tables": result.tables,
            "actors": [
                {
                    "node": f"actor:{a.actor}",
                    "kind": str(a.kind),
                    "name": a.name,
                    "via": list(a.via),
                    "reads": a.reads,
                    "writes": list(a.writes),
                    "active_days": a.active_days,
                    "last_read": str(a.last_read) if a.last_read else None,
                }
                for a in result.actors
            ],
        }

    @app.get("/lineage/{table}")
    def lineage(table: str, column: str | None = None) -> Any:
        return run("lineage", table=table, column=column)

    @app.get("/metrics/{metric}/brief")
    def brief(metric: str, day: str | None = None, format: str = "text") -> Any:
        with engine():
            b = morning_brief(workspace, metric, date.fromisoformat(day) if day else None)
        return to_slack(b) if format == "slack" else {"text": to_text(b)}

    @app.post("/ask")
    def ask(request: AskRequest) -> AskResponse:
        if not agent_available:
            raise HTTPException(503, NO_KEY)
        conversation_id = request.conversation_id or uuid.uuid4().hex
        with registry:
            if conversation_id not in conversations:
                conversations[conversation_id] = (new_agent(toolbox), threading.Lock())
                if len(conversations) > MAX_CONVERSATIONS:
                    conversations.popitem(last=False)
            conversations.move_to_end(conversation_id)
            agent, turn = conversations[conversation_id]
        # One question at a time per conversation; the toolbox takes the warehouse lock per tool call,
        # so other visitors' pages keep loading while the model thinks.
        with turn:
            answer = agent.ask(request.question)
        return AskResponse(
            conversation_id=conversation_id,
            answer=answer.text,
            tool_calls=[
                {"tool": c.name, "input": c.input, "seconds": round(c.seconds, 2), "error": c.error}
                for c in answer.tool_calls
            ],
            model=answer.model,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            cache_read_tokens=answer.cache_read_tokens,
        )

    return app


def lineage_map_dict(graph: LineageGraph) -> dict[str, Any]:
    """Tables and the tools around them (connectors that load, consumers that read), for drawing."""
    g = graph.g
    edges = []
    used: set[str] = set()
    for u, v, d in g.edges(data=True):
        kind = d["kind"]
        if kind not in (EdgeKind.FEEDS, EdgeKind.LOADS, EdgeKind.READS):
            continue
        activity = d["activity"]
        edges.append(
            {
                "source": u,
                "target": v,
                "kind": str(kind),
                "statements": activity.statements,
                "last_seen": str(activity.last_seen) if activity.last_seen else None,
            }
        )
        used |= {u, v}
    nodes = []
    for n, d in g.nodes(data=True):
        if d["type"] == "table":
            schema, _, name = d["name"].partition(".")
            nodes.append(
                {
                    "id": n,
                    "type": "table",
                    "name": d["name"],
                    "label": name,
                    "group": schema,
                    "object_type": d["object_type"],
                    "built_by": sorted(d["writers"]),
                }
            )
        elif d["type"] == "actor" and n in used:
            nodes.append(
                {
                    "id": n,
                    "type": "actor",
                    "name": d["name"],
                    "label": d["name"],
                    "group": str(d["kind"]),
                    "tool": d["tool"],
                    "attributes": d["attributes"],
                }
            )
    return {
        "nodes": nodes,
        "edges": edges,
        "schema_changes": [
            {"table": c.table, "at": str(c.at), "by": c.actor, "ddl": c.ddl} for c in graph.schema_changes
        ],
    }
