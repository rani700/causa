"""HTTP API: the engines and the investigator agent behind one small FastAPI service."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from causa.agent import InvestigatorAgent, Toolbox
from causa.brief import morning_brief, to_slack, to_text
from causa.workspace import Workspace


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


def create_app(workspace: Workspace, agent_factory: Callable[[Toolbox], InvestigatorAgent] | None = None) -> FastAPI:
    app = FastAPI(title="Causa", summary="Root-cause engine for business metrics")
    toolbox = Toolbox(workspace)
    lock = threading.Lock()  # one warehouse connection: serialise engine work
    conversations: dict[str, InvestigatorAgent] = {}
    make_agent = agent_factory or (lambda tb: InvestigatorAgent(tb))

    def run(name: str, **arguments: Any) -> Any:
        try:
            with lock:
                return getattr(toolbox, name)(**arguments)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/metrics")
    def metrics() -> Any:
        return run("list_metrics")

    @app.get("/metrics/{metric}/explain")
    def explain(metric: str, current: str, baseline: str | None = None) -> Any:
        return run("explain_metric", metric=metric, current=current, baseline=baseline)

    @app.get("/metrics/{metric}/verdict")
    def verdict(metric: str, current: str, baseline: str | None = None) -> Any:
        return run("judge_change", metric=metric, current=current, baseline=baseline)

    @app.get("/metrics/{metric}/recall")
    def recall(metric: str, current: str, baseline: str | None = None) -> Any:
        return run("recall_consumers", metric=metric, current=current, baseline=baseline)

    @app.get("/health/cases")
    def health(since: str, until: str, min_severity: str = "medium") -> Any:
        return run("health_cases", since=since, until=until, min_severity=min_severity)

    @app.get("/lineage/{table}")
    def lineage(table: str, column: str | None = None) -> Any:
        return run("lineage", table=table, column=column)

    @app.get("/metrics/{metric}/brief")
    def brief(metric: str, day: str | None = None, format: str = "text") -> Any:
        with lock:
            b = morning_brief(workspace, metric, date.fromisoformat(day) if day else None)
        return to_slack(b) if format == "slack" else {"text": to_text(b)}

    @app.post("/ask")
    def ask(request: AskRequest) -> AskResponse:
        conversation_id = request.conversation_id or uuid.uuid4().hex
        agent = conversations.get(conversation_id)
        if agent is None:
            agent = conversations[conversation_id] = make_agent(toolbox)
        with lock:
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
