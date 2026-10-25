"""The investigator agent: a language model plans, Causa's engines measure.

Two providers share one toolbox, one prompt and one audit trail: Claude (``agent.py``) and OpenAI
(``openai_agent.py``). ``make_agent`` picks one from the settings.
"""

from __future__ import annotations

from causa.agent.agent import AgentAnswer, InvestigatorAgent
from causa.agent.openai_agent import OpenAIInvestigatorAgent
from causa.agent.tools import TOOL_DEFINITIONS, Toolbox
from causa.config import AgentProvider, Settings, get_settings

Agent = InvestigatorAgent | OpenAIInvestigatorAgent


def agent_provider(settings: Settings | None = None) -> AgentProvider | None:
    """The provider the agent will use, or None when no key for it is configured."""
    s = settings or get_settings()
    has = {
        AgentProvider.ANTHROPIC: bool(s.anthropic_api_key and s.anthropic_api_key.get_secret_value()),
        AgentProvider.OPENAI: bool(s.openai_api_key and s.openai_api_key.get_secret_value()),
    }
    if s.agent_provider is AgentProvider.AUTO:
        return next((p for p, ok in has.items() if ok), None)
    return s.agent_provider if has[s.agent_provider] else None


def make_agent(toolbox: Toolbox, settings: Settings | None = None, effort: str | None = None) -> Agent:
    s = settings or get_settings()
    provider = agent_provider(s)
    if provider is AgentProvider.OPENAI:
        assert s.openai_api_key is not None
        return OpenAIInvestigatorAgent(
            toolbox, model=s.openai_model, effort=effort or "medium", api_key=s.openai_api_key.get_secret_value()
        )
    if provider is AgentProvider.ANTHROPIC:
        import anthropic

        assert s.anthropic_api_key is not None
        client = anthropic.Anthropic(api_key=s.anthropic_api_key.get_secret_value())
        return InvestigatorAgent(toolbox, messages_client=client.beta.messages, effort=effort or "high")
    raise RuntimeError(NO_KEY)


NO_KEY = "The investigator agent needs OPENAI_API_KEY or ANTHROPIC_API_KEY (in .env or the environment)."

__all__ = [
    "NO_KEY",
    "TOOL_DEFINITIONS",
    "Agent",
    "AgentAnswer",
    "InvestigatorAgent",
    "OpenAIInvestigatorAgent",
    "Toolbox",
    "agent_provider",
    "make_agent",
]
