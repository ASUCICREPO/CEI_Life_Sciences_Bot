from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.cei_ui import CEIUISurface, JsonValue

MAX_SSE_EVENTS = 500
MAX_SSE_PUBLIC_TEXT_LENGTH = 20_000
PUBLIC_PROCESSING_STATUS = "processing"
PUBLIC_TOOL_STATUS = "tool_running"
PUBLIC_CODE_EXECUTION_STATUS = "code_running"
PUBLIC_ERROR_MESSAGE = "The request could not be completed."
PUBLIC_ERROR_CODES = frozenset(
    {"invalid_request", "rate_limited", "service_unavailable", "internal_error"}
)


class EventType(StrEnum):
    THINKING_DELTA = "thinking_delta"
    TEXT_DELTA = "text_delta"
    UI_SURFACE = "ui_surface"
    HITL_REQUEST = "hitl_request"
    TOOL_USE = "tool_use"
    CODE_EXECUTION = "code_execution"
    DONE = "done"
    ERROR = "error"


class SSEEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: EventType
    data: CEIUISurface | dict[str, JsonValue]
    id: str | None = None
    retry: int | None = None

    def encode(self) -> str:
        lines: list[str] = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        lines.append(f"event: {self.event}")
        data = (
            self.data.model_dump(mode="json")
            if isinstance(self.data, CEIUISurface)
            else self.data
        )
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
        return "\n".join(lines) + "\n"


class SSEStream(BaseModel):
    events: list[SSEEvent] = Field(default_factory=list, max_length=MAX_SSE_EVENTS)

    def _emit(self, event: SSEEvent) -> None:
        if len(self.events) >= MAX_SSE_EVENTS:
            raise ValueError(f"SSE event count exceeds {MAX_SSE_EVENTS}")
        self.events.append(event)

    def emit_thinking(self, text: str) -> None:
        self._emit(
            SSEEvent(
                event=EventType.THINKING_DELTA,
                data={"status": PUBLIC_PROCESSING_STATUS},
            )
        )

    def emit_text(self, text: str) -> None:
        self._emit(
            SSEEvent(
                event=EventType.TEXT_DELTA,
                data={"text": text[:MAX_SSE_PUBLIC_TEXT_LENGTH]},
            )
        )

    def emit_ui_surface(self, surface: CEIUISurface) -> None:
        """Emit one complete CEI UI surface."""
        self._emit(SSEEvent(event=EventType.UI_SURFACE, data=surface))

    def emit_hitl_request(self, surface: CEIUISurface) -> None:
        """Emit an interactive request as a complete CEI UI surface; pauses, not completes, the stream (B56)."""
        self._emit(SSEEvent(event=EventType.HITL_REQUEST, data=surface))

    def emit_tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        self._emit(
            SSEEvent(
                event=EventType.TOOL_USE,
                data={"status": PUBLIC_TOOL_STATUS},
            )
        )

    def emit_error(self, message: str, *, code: str | None = None) -> None:
        public_code = code if code in PUBLIC_ERROR_CODES else "internal_error"
        self._emit(
            SSEEvent(
                event=EventType.ERROR,
                data={"message": PUBLIC_ERROR_MESSAGE, "code": public_code},
            )
        )

    def emit_code_execution(self, code: str, result: dict[str, Any]) -> None:
        self._emit(
            SSEEvent(
                event=EventType.CODE_EXECUTION,
                data={"status": PUBLIC_CODE_EXECUTION_STATUS},
            )
        )

    def emit_done(self) -> None:
        self._emit(SSEEvent(event=EventType.DONE, data={}))

    def serialize(self) -> str:
        return "".join(e.encode() for e in self.events)
