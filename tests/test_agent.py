from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from causa.agent import TOOL_DEFINITIONS, InvestigatorAgent, Toolbox
from causa.agent.agent import FALLBACK_BETA, MODEL
from causa.api import create_app
from causa.brief import morning_brief, to_slack, to_text
from causa.simulator.runner import run_simulation
from causa.simulator.scenario import CustomerChurn, SchemaDriftStall
from causa.warehouse.duckdb_backend import DuckDBWarehouse
from causa.workspace import Workspace
from tests.conftest import START, small_scenario


def day(offset: int) -> date:
    return START + timedelta(days=offset)


@pytest.fixture(scope="module")
def ws():
    wh = DuckDBWarehouse(":memory:")
    scenario = small_scenario(
        [
            CustomerChurn(event_id="B", start=day(70), customer_name="Acme GmbH"),
            SchemaDriftStall(event_id="D", start=day(77), end=day(80), feed="emea_orders"),
        ],
        days=84,
    )
    scenario.base_orders_per_day = 250
    run_simulation(scenario, wh)
    yield Workspace(wh)
    wh.close()


# -- a scripted stand-in for client.beta.messages ------------------------------------------------


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_use(id_: str, name: str, **inputs: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inputs)


def reply(stop: str, *content: SimpleNamespace) -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80)
    return SimpleNamespace(stop_reason=stop, content=list(content), model=MODEL, usage=usage)


class ScriptedMessages:
    def __init__(self, *replies: SimpleNamespace) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


# -- tools -------------------------------------------------------------------------------------


def test_tool_definitions_are_strict_and_cover_every_handler(ws) -> None:
    toolbox = Toolbox(ws)
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == set(toolbox._handlers)
    for t in TOOL_DEFINITIONS:
        schema = t["input_schema"]
        assert t["strict"] and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_sql_tool_is_read_only_and_keeps_ground_truth_out(ws) -> None:
    toolbox = Toolbox(ws)
    assert "error" in json.loads(toolbox.call("run_sql", {"sql": "DROP TABLE MART.FCT_ORDER_LINES"}))
    assert "error" in json.loads(toolbox.call("run_sql", {"sql": "SELECT 1; DELETE FROM RAW.FX_RATES"}))
    assert "off limits" in json.loads(toolbox.call("run_sql", {"sql": "SELECT * FROM SIM.EVENTS"}))["error"]
    out = json.loads(toolbox.call("run_sql", {"sql": "SELECT ORDER_ID FROM MART.FCT_ORDER_LINES"}))
    assert len(out["rows"]) == 50 and out["truncated"]


def test_judge_change_tool_returns_replayed_data_finding(ws) -> None:
    out = json.loads(
        Toolbox(ws).call(
            "judge_change", {"metric": "net_revenue", "current": f"{day(77)}..{day(83)}", "baseline": None}
        )
    )
    (finding,) = [f for f in out["findings"] if f["counted_as_data_incident"]]
    assert finding["method"].startswith("counterfactual replay")
    assert "schema change" in finding["cause"]
    assert out["data_incident_impact"] == finding["impact_on_change"] < 0


# -- the agent loop ----------------------------------------------------------------------------


def test_agent_runs_tools_and_answers(ws) -> None:
    fake = ScriptedMessages(
        reply(
            "tool_use",
            text("Checking."),
            tool_use("t1", "list_metrics"),
            tool_use("t2", "lineage", table="RAW.FX_RATES", column=None),
        ),
        reply("end_turn", text("Net revenue is the metric to look at.")),
    )
    agent = InvestigatorAgent(Toolbox(ws), messages_client=fake)
    answer = agent.ask("what can you explain?")

    assert answer.text == "Net revenue is the metric to look at."
    assert [c.name for c in answer.tool_calls] == ["list_metrics", "lineage"]
    first, second = fake.requests
    assert first["model"] == MODEL and first["betas"] == [FALLBACK_BETA] and first["fallbacks"] == "default"
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"] is TOOL_DEFINITIONS
    assert first["messages"][0]["content"].startswith(f"(Today is {ws.today}.)")
    # Both results go back together, in one user turn, matched by id.
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"] and not any(r["is_error"] for r in results)
    assert answer.input_tokens == 200 and answer.cache_read_tokens == 160


def test_tool_errors_are_returned_to_the_model(ws) -> None:
    fake = ScriptedMessages(
        reply(
            "tool_use", tool_use("t1", "explain_metric", metric="nope", current="2026-01-01", baseline=None, where=None)
        ),
        reply("end_turn", text("That metric does not exist.")),
    )
    answer = InvestigatorAgent(Toolbox(ws), messages_client=fake).ask("explain nope")
    (call,) = answer.tool_calls
    assert call.error and "unknown metric" in call.output
    assert fake.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_is_reported_not_read_as_content(ws) -> None:
    fake = ScriptedMessages(reply("refusal"))
    answer = InvestigatorAgent(Toolbox(ws), messages_client=fake).ask("...")
    assert answer.stop_reason == "refusal" and "declined" in answer.text


def test_follow_up_questions_keep_the_conversation(ws) -> None:
    fake = ScriptedMessages(reply("end_turn", text("one")), reply("end_turn", text("two")))
    agent = InvestigatorAgent(Toolbox(ws), messages_client=fake)
    agent.ask("first")
    agent.ask("second")
    roles = [m["role"] for m in fake.requests[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


# -- brief and API -----------------------------------------------------------------------------


def test_morning_brief_splits_data_from_business(ws) -> None:
    b = morning_brief(ws, "net_revenue", day(84))
    text_ = to_text(b)
    assert "comes from data incidents" in text_ and "schema change" in text_
    payload = to_slack(b)
    assert payload["blocks"][0]["type"] == "header"
    assert any("Data incidents" in blk.get("text", {}).get("text", "") for blk in payload["blocks"])


def test_api_serves_engines_and_agent(ws) -> None:
    fake = ScriptedMessages(reply("end_turn", text("All good.")))
    client = TestClient(create_app(ws, agent_factory=lambda tb: InvestigatorAgent(tb, messages_client=fake)))
    assert client.get("/metrics").json()["metrics"][0]["name"] == "net_revenue"
    assert client.get("/metrics/nope/explain", params={"current": f"{day(77)}..{day(83)}"}).status_code == 404
    verdict = client.get("/metrics/net_revenue/verdict", params={"current": f"{day(77)}..{day(83)}"}).json()
    assert verdict["data_incident_impact"] < 0
    asked = client.post("/ask", json={"question": "status?"}).json()
    assert asked["answer"] == "All good." and asked["conversation_id"]


def test_web_ui_and_its_endpoints(ws, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(create_app(ws))
    page = client.get("/")
    assert page.status_code == 200 and "/static/app.js" in page.text
    assert client.get("/static/app.js").status_code == 200

    status = client.get("/status").json()
    assert status["backend"] == "duckdb" and status["agent_available"] is False
    assert status["history_start"] <= str(day(0)) and status["metrics"][0]["name"] == "net_revenue"
    # without a key the agent says why instead of failing
    assert client.post("/ask", json={"question": "hi"}).status_code == 503

    points = client.get("/metrics/net_revenue/series", params={"since": str(day(0)), "until": str(day(83))}).json()
    assert len(points["points"]) == 84 and all(v > 0 for _, v in points["points"])

    # one engine run gives both the verdict and the recall for the week of the schema drift
    found = client.get("/metrics/net_revenue/investigation", params={"current": f"{day(77)}..{day(83)}"}).json()
    assert found["verdict"]["data_incident_impact"] < 0
    assert found["recalls"] and found["recalls"][0]["origin_table"] == "RAW.SHOP_ORDERS"

    lineage = client.get("/lineage-map").json()
    ids = {n["id"] for n in lineage["nodes"]}
    assert {"table:RAW.SHOP_ORDERS", "table:MART.FCT_ORDER_LINES"} <= ids
    assert all(e["source"] in ids and e["target"] in ids for e in lineage["edges"])
    assert {e["kind"] for e in lineage["edges"]} == {"feeds", "loads", "reads"}
    assert any(c["table"] == "RAW.SHOP_ORDERS" for c in lineage["schema_changes"])

    blast = client.get("/lineage/RAW.SHOP_ORDERS/impact").json()
    assert "MART.FCT_ORDER_LINES" in blast["tables"]
    assert {a["node"] for a in blast["actors"] if a["reads"]} <= ids  # every reader is drawn on the map


def test_mcp_server_offers_the_agents_tools(ws) -> None:
    import anyio
    from mcp import Client

    from causa.mcp_server import create_server

    built: list[Toolbox] = []

    def factory() -> Toolbox:
        built.append(Toolbox(ws))
        return built[-1]

    async def session() -> None:
        async with Client(create_server(factory)) as client:
            assert "list_metrics first" in (client.instructions or "")
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {t["name"] for t in TOOL_DEFINITIONS}
            assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())
            # optional arguments are optional, not "required but nullable"
            assert tools["judge_change"].input_schema["required"] == ["metric", "current"]
            assert not built  # the warehouse is not touched until a tool runs

            week = {"metric": "net_revenue", "current": f"{day(77)}..{day(83)}"}
            verdict = await client.call_tool("judge_change", week)
            assert not verdict.is_error
            out = json.loads(verdict.content[0].text)
            assert out["data_incident_impact"] < 0 and len(built) == 1

            missing = await client.call_tool("judge_change", {"metric": "no_such_metric", "current": str(day(80))})
            assert missing.is_error and "no_such_metric" in missing.content[0].text
            refused = await client.call_tool("run_sql", {"sql": "DELETE FROM MART.FCT_ORDER_LINES"})
            assert "only a single SELECT" in refused.content[0].text
            assert len(built) == 1  # one workspace for the whole session

    anyio.run(session)
