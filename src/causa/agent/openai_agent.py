"""The investigator agent on OpenAI's Responses API: the same tools, prompt and audit trail as the
Claude agent, for teams that run on OpenAI.

A plain function-calling loop. Responses are not stored server-side (``store=False``); reasoning
items come back encrypted and are passed on unchanged, so the model keeps its reasoning between tool
calls. The conversation is append-only, which keeps OpenAI's automatic prompt caching effective.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol, cast

from causa.agent.agent import SYSTEM_PROMPT, AgentAnswer, ToolCall
from causa.agent.tools import TOOL_DEFINITIONS, Toolbox
from causa.warehouse import metering

MODEL = "gpt-5-nano"
MAX_OUTPUT_TOKENS = 16000
# Causa's effort levels (shared with the Claude agent and the CLI) on OpenAI's scale.
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

# The agent's strict JSON schemas carry over as they are: every field required, optional ones nullable.
OPENAI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": t["name"],
        "description": t["description"],
        "parameters": t["input_schema"],
        "strict": True,
    }
    for t in TOOL_DEFINITIONS
]


class ResponsesClient(Protocol):
    """The slice of ``openai.OpenAI().responses`` the agent uses (a fake can stand in)."""

    def create(self, **kwargs: Any) -> Any: ...


class OpenAIInvestigatorAgent:
    def __init__(
        self,
        toolbox: Toolbox,
        responses_client: ResponsesClient | None = None,
        model: str = MODEL,
        effort: str = "medium",
        max_turns: int = 15,
        api_key: str | None = None,
    ) -> None:
        if responses_client is None:
            import openai

            responses_client = cast(ResponsesClient, openai.OpenAI(api_key=api_key).responses)
        self._tools = toolbox
        self._responses: ResponsesClient = responses_client
        self._model = model
        self._effort = EFFORT.get(effort, "medium")
        self._max_turns = max_turns
        self.history: list[dict[str, Any]] = []

    def ask(self, question: str) -> AgentAnswer:
        """Answers one question; follow-up questions continue the same conversation."""
        if not self.history:
            question = f"{self._tools.context()}\n\n{question}"
        self.history.append({"role": "user", "content": question})
        answer = AgentAnswer(text="")

        for _ in range(self._max_turns):
            response = self._responses.create(
                model=self._model,
                instructions=SYSTEM_PROMPT,
                input=self.history,
                tools=OPENAI_TOOLS,
                reasoning={"effort": self._effort},
                text={"verbosity": "low"},  # the prompt asks for brief answers; GPT-5 models need the nudge
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
                include=["reasoning.encrypted_content"],
            )
            _add_usage(answer, response)
            answer.model = getattr(response, "model", None)
            answer.stop_reason = getattr(response, "status", None)
            output = list(response.output)
            self.history.extend(_item(o) for o in output)

            if any(c.type == "refusal" for o in output if o.type == "message" for c in o.content):
                answer.stop_reason = "refusal"
                answer.text = "The model declined to answer this request."
                return answer
            calls = [o for o in output if o.type == "function_call"]
            if not calls:
                answer.text = _text(output)
                if answer.stop_reason == "incomplete":
                    answer.text = (answer.text + "\n\n(The answer was cut short.)").strip()
                return answer

            for c in calls:
                call = self._run(c.name, c.arguments)
                answer.tool_calls.append(call)
                self.history.append({"type": "function_call_output", "call_id": c.call_id, "output": call.output})

        answer.text = "Stopped: the investigation needed more steps than allowed."
        return answer

    def _run(self, name: str, arguments: str) -> ToolCall:
        started = time.perf_counter()
        parsed: dict[str, Any] = {}
        with metering() as meter:
            try:
                parsed = json.loads(arguments or "{}")
                output, error = self._tools.call(name, parsed), False
            except Exception as exc:
                output, error = f"{type(exc).__name__}: {exc}", True
        return ToolCall(name, parsed, output, time.perf_counter() - started, error, meter.statements, meter.seconds)


def _item(o: Any) -> dict[str, Any]:
    """An output item as input for the next turn, unchanged."""
    dump = getattr(o, "model_dump", None)
    return dump(exclude_none=True) if dump else dict(vars(o))


def _text(output: list[Any]) -> str:
    return "\n".join(
        c.text for o in output if o.type == "message" for c in o.content if c.type == "output_text"
    ).strip()


def _add_usage(answer: AgentAnswer, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    answer.input_tokens += getattr(usage, "input_tokens", 0) or 0
    answer.output_tokens += getattr(usage, "output_tokens", 0) or 0
    details = getattr(usage, "input_tokens_details", None)
    answer.cache_read_tokens += getattr(details, "cached_tokens", 0) or 0
