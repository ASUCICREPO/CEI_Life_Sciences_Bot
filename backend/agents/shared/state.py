from __future__ import annotations

from enum import StrEnum
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from shared.schemas.citation import Citation


class AgentMode(StrEnum):
    SCIENCE = "science"
    BUSINESS = "business"


class BaseAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    mode: AgentMode
    user_id: str
    session_id: str
    citations: list[Citation]
