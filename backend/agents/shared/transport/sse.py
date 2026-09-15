from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.a2ui import A2UIComponent


class EventType(StrEnum):
    TEXT_DELTA = "text_delta"
    A2UI = "a2ui"
    HITL_REQUEST = "hitl_request"
    TOOL_USE = "tool_use"
    DONE = "done"
    ERROR = "error"


class SSEEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: EventType
    data: dict[str, Any]
    id: str | None = None
    retry: int | None = None

    def encode(self) -> str:
        lines: list[str] = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        lines.append(f"event: {self.event.value}")
        lines.append(f"data: {json.dumps(self.data)}")
        lines.append("")
        return "\n".join(lines) + "\n"


class SSEStream(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    events: list[SSEEvent] = Field(default_factory=list)

    def emit_text(self, text: str) -> None:
        self.events.append(
            SSEEvent(event=EventType.TEXT_DELTA, data={"text": text})
        )

    def emit_a2ui(self, component: A2UIComponent) -> None:
        self.events.append(
            SSEEvent(event=EventType.A2UI, data=component.to_dict())
        )

    def emit_hitl_request(self, request: A2UIComponent) -> None:
        self.events.append(
            SSEEvent(event=EventType.HITL_REQUEST, data=request.to_dict())
        )

    def emit_tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        self.events.append(
            SSEEvent(
                event=EventType.TOOL_USE,
                data={"name": tool_name, "input": tool_input},
            )
        )

    def emit_error(self, message: str, *, code: str | None = None) -> None:
        data: dict[str, Any] = {"message": message}
        if code is not None:
            data["code"] = code
        self.events.append(SSEEvent(event=EventType.ERROR, data=data))

    def emit_done(self) -> None:
        self.events.append(SSEEvent(event=EventType.DONE, data={}))

    def serialize(self) -> str:
        return "".join(e.encode() for e in self.events)
