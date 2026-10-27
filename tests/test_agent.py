from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from causa.agent import TOOL_DEFINITIONS, InvestigatorAgent, Toolbox, tools
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT content FROM read_text('/proc/self/environ')",  # file reader (table function)
        "SELECT * FROM '/etc/hosts'",  # file as a table name
        "SELECT * INTO TABLE MART.COPY FROM MART.FCT_ORDER_LINES",  # a write dressed as a SELECT
        "SELECT * FROM SYS.EXA_DBA_USERS",
        "SELECT * FROM EXA_DBA_USERS",  # Exasol resolves system views without a schema
        "SELECT HOST FROM EXA_STATISTICS.EXA_DBA_AUDIT_SESSIONS",
        "SELECT * FROM MART.FCT_ORDER_LINES WHERE 1 IN (SELECT 1 FROM EXA_STATISTICS.EXA_DBA_AUDIT_SQL)",
        "SELECT * FROM OPS.QUERY_LOG",
        "SELECT * FROM CAUSA.NOTIFICATIONS",
        "SELECT * FROM warehouse.MART.FCT_ORDER_LINES",
    ],
)
def test_sql_tool_refuses_anything_but_company_tables(ws, sql: str) -> None:
    assert "error" in json.loads(Toolbox(ws).call("run_sql", {"sql": sql}))


def test_sql_tool_allows_company_tables_and_ctes(ws) -> None:
    sql = (
        "WITH t AS (SELECT REGION, SUM(NET_REVENUE_EUR) AS V FROM MART.FCT_ORDER_LINES GROUP BY REGION) SELECT * FROM t"
    )
    out = json.loads(Toolbox(ws).call("run_sql", {"sql": sql}))
    assert "error" not in out and out["rows"]


def test_the_local_copy_cannot_read_files(tmp_path) -> None:
    wh = DuckDBWarehouse(tmp_path / "w.duckdb")
    with pytest.raises(duckdb.Error):
        wh.query("SELECT content FROM read_text('/etc/hosts')")
    wh.execute("CREATE SCHEMA S")  # the attached file itself stays writable
    wh.execute("CREATE TABLE S.T AS SELECT 1 AS X")
    assert wh.scalar("SELECT X FROM S.T") == 1
    wh.close()


def test_a_slow_sql_tool_query_is_stopped(ws, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "SQL_SECONDS", 0.2)
    sql = (
        "SELECT COUNT(*) FROM MART.FCT_ORDER_LINES A CROSS JOIN MART.FCT_ORDER_LINES B "
        "CROSS JOIN MART.FCT_ORDER_LINES C"
    )
    out = json.loads(Toolbox(ws).call("run_sql", {"sql": sql}))
    assert "stopped after" in out["error"]
    assert Toolbox(ws).call("list_metrics", {})  # the connection is still usable


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
    # The summary says in words which period the incident distorted and in which direction.
    assert any(
        "distorted the period being explained" in line and "worse than it really was" in line for line in out["summary"]
    )
    assert out["summary"][-1].startswith("Real business change once incidents are removed")


def test_periods_after_the_data_are_refused_with_the_right_dates(ws) -> None:
    after = f"{ws.today}..{ws.today + timedelta(days=6)}"
    with pytest.raises(ValueError, match=f"latest complete day is {ws.today - timedelta(days=1)}"):
        Toolbox(ws).call("judge_change", {"metric": "net_revenue", "current": after})


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
    assert first["messages"][0]["content"].startswith(f"(Today is {ws.today}. Metrics: net_revenue, ")
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
    assert page.status_code == 200 and "/static/app.js?v=" in page.text and "/static/app.css?v=" in page.text
    assert page.headers["cache-control"] == "no-cache"
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


def test_investigations_reuse_replays_and_verdicts(ws, monkeypatch: pytest.MonkeyPatch) -> None:
    from causa.metrics.attribution import Period
    from causa.verdict import Investigator

    calls: list[str] = []
    for name in ("execute", "query"):
        original = getattr(ws.wh, name)
        monkeypatch.setattr(ws.wh, name, lambda sql, _o=original: calls.append(sql) or _o(sql))

    def replayed() -> int:
        return sum("CAUSA_REPLAY" in sql for sql in calls)

    ws._replays.clear()
    tb = Toolbox(ws)
    first = tb.judge_change("net_revenue", f"{day(76)}..{day(82)}")
    assert replayed() > 0
    # another week touching the same incident re-uses its replay
    calls.clear()
    second = tb.judge_change("net_revenue", f"{day(77)}..{day(83)}")
    assert replayed() == 0 and len(ws._replays) == 1
    # the same question again, from any tool, runs nothing at all
    calls.clear()
    tb.recall_consumers("net_revenue", f"{day(77)}..{day(83)}")
    assert tb.investigate("net_revenue", f"{day(77)}..{day(83)}")["verdict"] == second
    assert calls == []

    # cached replays give exactly what a fresh, uncached run gives
    fresh = Investigator(ws.wh, ws.catalog, ws.graph, ws.query_log, ws._health, replay=True).verdict(
        ws.config.metric("net_revenue"), Period(day(77), day(83))
    )
    assert second["data_incident_impact"] == round(fresh.data_impact, 2) < 0
    assert first["findings"][0]["method"].startswith("counterfactual replay")


# -- the OpenAI agent --------------------------------------------------------------------------


def fn_call(call_id: str, name: str, **arguments: Any) -> SimpleNamespace:
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments))


def message(*parts: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(type="message", role="assistant", content=list(parts))


def response(*output: SimpleNamespace, status: str = "completed") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, input_tokens_details=SimpleNamespace(cached_tokens=64))
    return SimpleNamespace(output=list(output), status=status, model="gpt-5-nano", usage=usage)


class ScriptedResponses:
    def __init__(self, *responses: SimpleNamespace) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "input": list(kwargs["input"])})
        return self.responses.pop(0)


def output_text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="output_text", text=t)


def test_openai_agent_runs_the_same_tools(ws) -> None:
    from causa.agent import OpenAIInvestigatorAgent
    from causa.agent.agent import SYSTEM_PROMPT
    from causa.agent.openai_agent import OPENAI_TOOLS

    fake = ScriptedResponses(
        response(
            SimpleNamespace(type="reasoning", id="rs_1", encrypted_content="opaque"),
            fn_call("c1", "list_metrics"),
            fn_call("c2", "lineage", table="RAW.FX_RATES", column=None),
        ),
        response(message(output_text("Net revenue is the metric to look at."))),
    )
    agent = OpenAIInvestigatorAgent(Toolbox(ws), responses_client=fake)
    answer = agent.ask("what can you explain?")

    assert answer.text == "Net revenue is the metric to look at." and answer.model == "gpt-5-nano"
    assert [c.name for c in answer.tool_calls] == ["list_metrics", "lineage"] and not answer.tool_calls[1].error
    first, second = fake.requests
    assert first["model"] == "gpt-5-nano" and first["instructions"] == SYSTEM_PROMPT and first["store"] is False
    assert first["text"] == {"verbosity": "low"}
    assert first["input"][0]["content"].startswith(f"(Today is {ws.today}. Metrics: net_revenue, ")
    # one function per engine tool, with the agent's strict schema unchanged
    assert [t["name"] for t in first["tools"]] == [t["name"] for t in TOOL_DEFINITIONS]
    assert first["tools"] is OPENAI_TOOLS and all(t["strict"] for t in OPENAI_TOOLS)
    # the reasoning item goes back unchanged, then each result matched by call id
    sent = second["input"]
    assert sent[1] == {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
    assert [i["call_id"] for i in sent if i.get("type") == "function_call_output"] == ["c1", "c2"]
    assert answer.input_tokens == 200 and answer.cache_read_tokens == 128


def test_openai_agent_reports_tool_errors_and_refusals(ws) -> None:
    from causa.agent import OpenAIInvestigatorAgent

    fake = ScriptedResponses(
        response(fn_call("c1", "explain_metric", metric="nope", current="2026-01-01", baseline=None, where=None)),
        response(message(SimpleNamespace(type="refusal", refusal="no"))),
    )
    answer = OpenAIInvestigatorAgent(Toolbox(ws), responses_client=fake).ask("explain nope")
    (call,) = answer.tool_calls
    assert call.error and "unknown metric" in call.output
    assert answer.stop_reason == "refusal" and "declined" in answer.text


def test_provider_follows_the_configured_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from causa.agent import agent_provider
    from causa.config import AgentProvider, Settings

    assert agent_provider(Settings()) is None  # no keys (see conftest)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert agent_provider(Settings()) is AgentProvider.OPENAI
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert agent_provider(Settings()) is AgentProvider.ANTHROPIC  # auto prefers Claude
    monkeypatch.setenv("CAUSA_AGENT_PROVIDER", "openai")
    assert agent_provider(Settings()) is AgentProvider.OPENAI


def test_pages_load_while_the_agent_thinks(ws) -> None:
    import threading

    from causa.agent import AgentAnswer

    thinking, done = threading.Event(), threading.Event()

    class SlowAgent:
        def __init__(self, toolbox: Toolbox) -> None:
            self.toolbox = toolbox

        def ask(self, question: str) -> AgentAnswer:
            self.toolbox.call("list_metrics", {})  # tools still go through the warehouse lock
            thinking.set()
            assert done.wait(10)  # the model is "thinking"
            return AgentAnswer(text="ok")

    client = TestClient(create_app(ws, agent_factory=SlowAgent))
    asking = threading.Thread(target=lambda: client.post("/ask", json={"question": "slow?"}))
    asking.start()
    assert thinking.wait(10)
    page = client.get(f"/metrics/net_revenue/series?since={day(70)}&until={day(76)}")
    assert page.status_code == 200 and len(page.json()["points"]) == 7
    done.set()
    asking.join(10)


def test_new_activity_in_the_log_refreshes_the_workspace() -> None:
    wh = DuckDBWarehouse(":memory:")
    run_simulation(small_scenario(days=10), wh)
    fresh = Workspace(wh)
    tb = Toolbox(fresh)
    graph, before = fresh.graph, fresh.today
    tb.judge_change("net_revenue", str(START + timedelta(days=8)))
    assert tb.keep_fresh(every=0) is False and fresh.graph is graph  # nothing new: nothing reloaded
    assert len(tb._verdicts) == 1

    # a new statement lands in the query log (as a nightly load would)
    wh.execute(
        "INSERT INTO OPS.QUERY_LOG SELECT SESSION_ID, STMT_ID + 1000000, CLIENT, USER_NAME, COMMAND_NAME, "
        "SQL_TEXT, START_TIME + INTERVAL '1' DAY, STOP_TIME + INTERVAL '1' DAY, DURATION, SUCCESS, ROW_COUNT, "
        "ERROR_TEXT FROM OPS.QUERY_LOG WHERE START_TIME = (SELECT MAX(START_TIME) FROM OPS.QUERY_LOG)"
    )
    assert tb.keep_fresh(every=0) is True
    assert fresh.graph is not graph and fresh.today == before + timedelta(days=1)
    assert not tb._verdicts  # answers computed on the old data are gone
    wh.close()


def test_health_ready_and_warm_up(ws) -> None:
    client = TestClient(create_app(ws))
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready").json()
    assert ready["checks"]["warehouse"]["backend"] == "duckdb" and ready["checks"]["warm_up"] == "off"

    tb = Toolbox(ws)
    assert tb.warm_up() > 0
    calls: list[str] = []
    original = ws.wh.query
    ws.wh.query = lambda sql: calls.append(sql) or original(sql)  # type: ignore[method-assign]
    try:
        tb.judge_change("net_revenue", f"{day(77)}..{day(83)}")  # the incident's week: prepared already
    finally:
        ws.wh.query = original  # type: ignore[method-assign]
    assert not any("CAUSA_REPLAY" in sql for sql in calls)


def test_answers_report_what_they_cost(ws) -> None:
    from causa.agent.pricing import usd

    tb = Toolbox(ws)
    ws._replays.clear()
    first = tb.investigate("net_revenue", f"{day(70)}..{day(76)}")["cost"]
    assert first["statements"] > 0 and first["backends"] == ["duckdb"] and not first["cached"]
    again = tb.investigate("net_revenue", f"{day(70)}..{day(76)}")["cost"]
    assert again == {"statements": 0, "warehouse_seconds": 0.0, "backends": [], "cached": True}

    # 12,824 input tokens (6,656 cached) and 8,264 output on gpt-5-nano: the live Exasol run
    assert round(usd("gpt-5-nano-2025-08-07", 12_824, 6_656, 8_264), 5) == 0.00365
    assert usd("claude-opus-5-5", 1, 0, 1) is None  # unknown price: tokens only


def test_notifications_endpoint(ws) -> None:
    client = TestClient(create_app(ws))
    r = client.get("/notifications").json()
    assert r["watching_every_minutes"] == 0 and r["slack"] is False and isinstance(r["sent"], list)
