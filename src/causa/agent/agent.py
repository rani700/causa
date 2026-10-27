"""The investigator agent: Claude plans the investigation, Causa's engines produce every number.

A plain tool-use loop over the Messages API. The system prompt and tool definitions are frozen and
cached; the conversation is append-only (assistant turns are appended as returned, thinking blocks
included). Each tool call is recorded, so every answer comes with an audit trail of what was asked of
which engine and what it returned.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import anthropic

from causa.agent.pricing import usd
from causa.agent.tools import TOOL_DEFINITIONS, Toolbox
from causa.warehouse import metering

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Shared with the MCP server, where Claude Desktop or Claude Code plays the investigator.
GUIDANCE = """\
How you work:
- Every figure in your answer must come from a tool result; round it, never derive new figures by \
arithmetic of your own. If a figure is not available from the tools, say so.
- For "why did X change?" or "is this real?", start with judge_change. Use explain_metric to drill \
further into one segment, health_cases for incidents not tied to a metric, lineage to show where data \
comes from, and recall_consumers when someone asks who saw wrong numbers or when a data incident \
affected a widely used metric.
- When the user gives no period, use the last 7 complete days ending yesterday (today's date is in \
list_metrics) and the default baseline (the same weekdays one week earlier).
- judge_change returns a "summary": it already says which period each incident distorted and in \
which direction. Follow it; never describe an incident as inflating or hiding a period differently.
- Keep data incidents and real business change apart. For each data incident give its cause in plain \
words, its impact, how the impact was established (replayed, estimated, healed) and the confidence. \
Mention restatements: numbers people saw at the time that were corrected later.
- If a change is within normal variation, say so plainly instead of over-explaining noise.

How you answer:
- Your readers are business and data people, not SQL readers. Lead with a one-sentence verdict, then \
the data incidents, then the real business change, then what to do next. Be brief; use short bullet \
lists rather than long prose, and no tables unless asked.
"""

SYSTEM_PROMPT = (
    """\
You are Causa's investigator. People ask you why a business metric moved, whether a number can be \
trusted, and who was affected when it could not. You answer with tools that run Causa's engines on \
the company's warehouse: lineage from the query log, data health detection, metric attribution, a \
verdict that separates data incidents from real business change (re-running the pipeline on \
repaired data where possible), and recall of everyone who consumed wrong numbers.

"""
    + GUIDANCE
)


class MessagesClient(Protocol):
    """The slice of ``anthropic.Anthropic().beta.messages`` the agent uses (a fake can stand in)."""

    def create(self, **kwargs: Any) -> Any: ...


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    output: str
    seconds: float
    error: bool = False
    statements: int = 0  # warehouse statements the tool ran
    warehouse_seconds: float = 0.0


@dataclass
class AgentAnswer:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def cost_usd(self) -> float | None:
        return usd(self.model, self.input_tokens, self.cache_read_tokens, self.output_tokens)

    @property
    def warehouse(self) -> dict[str, Any]:
        return {
            "statements": sum(c.statements for c in self.tool_calls),
            "warehouse_seconds": round(sum(c.warehouse_seconds for c in self.tool_calls), 2),
        }


class InvestigatorAgent:
    def __init__(
        self,
        toolbox: Toolbox,
        messages_client: MessagesClient | None = None,
        model: str = MODEL,
        effort: str = "high",
        max_turns: int = 15,
    ) -> None:
        self._tools = toolbox
        self._messages: MessagesClient = messages_client or cast(MessagesClient, anthropic.Anthropic().beta.messages)
        self._model = model
        self._effort = effort
        self._max_turns = max_turns
        self.history: list[dict[str, Any]] = []

    def ask(self, question: str) -> AgentAnswer:
        """Answers one question; follow-up questions continue the same conversation."""
        if not self.history:
            question = f"{self._tools.context()}\n\n{question}"
        self.history.append({"role": "user", "content": question})
        answer = AgentAnswer(text="")

        for _ in range(self._max_turns):
            response = self._messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=TOOL_DEFINITIONS,
                messages=self.history,
                cache_control={"type": "ephemeral"},
                output_config={"effort": self._effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            _add_usage(answer, response)
            answer.stop_reason = response.stop_reason
            answer.model = getattr(response, "model", None)
            self.history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                answer.text = "The model declined to answer this request."
                return answer
            if response.stop_reason == "pause_turn":
                continue
            uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not uses:
                answer.text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                return answer

            results = []
            for block in uses:
                call = self._run(block.name, block.input)
                answer.tool_calls.append(call)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": call.output, "is_error": call.error}
                )
            self.history.append({"role": "user", "content": results})

        answer.text = "Stopped: the investigation needed more steps than allowed."
        return answer

    def _run(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        started = time.perf_counter()
        with metering() as meter:
            try:
                output, error = self._tools.call(name, arguments), False
            except Exception as exc:
                output, error = f"{type(exc).__name__}: {exc}", True
        return ToolCall(name, arguments, output, time.perf_counter() - started, error, meter.statements, meter.seconds)


def _add_usage(answer: AgentAnswer, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    answer.input_tokens += getattr(usage, "input_tokens", 0) or 0
    answer.output_tokens += getattr(usage, "output_tokens", 0) or 0
    answer.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
