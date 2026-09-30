from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from shared.state import AgentMode, BaseAgentState

__all__ = ["AgentMode", "BaseAgentState"]


def __getattr__(name: str) -> object:
	if name in __all__:
		from shared.state import AgentMode, BaseAgentState

		return {"AgentMode": AgentMode, "BaseAgentState": BaseAgentState}[name]
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
