from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict


class AgentMode(StrEnum):
    SCIENCE = "science"
    BUSINESS = "business"


class TraceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    stage: str
    detail: str
    input_tokens: int = 0
    output_tokens: int = 0


class BaseAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    mode: AgentMode
    model_id: str
    user_id: str
    session_id: str
    trace: Annotated[list[TraceEntry], operator.add]
