"""Local runtime operation contract for the MVP demo runtimes (B94, B96).

Every local runtime accepts ``POST /invocations`` with an ``op`` field:

- ``run`` (default when ``op`` is absent): start a new graph run.
- ``resume``: resume a graph paused by ``interrupt()`` with a CEI UI ``ActionRequest``.
- ``job_status``: read a background job snapshot (Pipeline Manager only, B98).

A paused graph returns ``status: "awaiting_input"`` with the HITL surface as ``ui``.
The pending surface is stored per thread so a resume can be checked against it
(surface id, revision, interaction id, component id, action id). A mismatch is
``stale``; the response always echoes the 8 correlation fields of the request.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Mapping

from pydantic import ValidationError

from shared.schemas.cei_ui import ActionRequest, ActionResponse, CEIUISurface

MAX_PENDING_INTERRUPTS = 128

ActionStatus = Literal["accepted", "completed", "stale", "rejected", "error"]


class RuntimeOp(StrEnum):
    RUN = "run"
    RESUME = "resume"
    JOB_STATUS = "job_status"


class EnvelopeStatus(StrEnum):
    COMPLETED = "completed"
    AWAITING_INPUT = "awaiting_input"
    RUNNING = "running"
    REJECTED = "rejected"
    STALE = "stale"
    ERROR = "error"


def parse_op(payload: Mapping[str, Any]) -> RuntimeOp:
    raw = payload.get("op", RuntimeOp.RUN.value)
    try:
        return RuntimeOp(raw)
    except ValueError as error:
        allowed = ", ".join(op.value for op in RuntimeOp)
        raise ValueError(f"op must be one of: {allowed}") from error


def parse_action(payload: Mapping[str, Any]) -> ActionRequest:
    raw = payload.get("action")
    if not isinstance(raw, Mapping):
        raise ValueError("resume requires an 'action' object")
    try:
        return ActionRequest.model_validate(raw)
    except ValidationError as error:
        raise ValueError("action is not a valid CEI UI ActionRequest") from error


def parse_mode(payload: Mapping[str, Any]) -> str:
    mode = payload.get("mode", "science")
    return mode if mode in ("science", "business") else "science"


def parse_prompt(payload: Mapping[str, Any]) -> str:
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    return prompt


def extract_interrupt_surface(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the CEI UI surface of the first pending ``interrupt()``, if any.

    LangGraph puts pending interrupts under ``__interrupt__`` in the ``ainvoke``
    result. Nodes in this repo always interrupt with a surface dict
    (``CEIUISurface.model_dump(mode="json")``).
    """
    if not result:
        return None
    interrupts = result.get("__interrupt__") or []
    for item in interrupts:
        value = getattr(item, "value", None)
        if value is None and isinstance(item, Mapping):
            value = item.get("value")
        if isinstance(value, Mapping):
            return dict(value)
    return None


def resume_value(action: ActionRequest) -> dict[str, Any]:
    """The value a node receives from ``interrupt()`` after a resume."""
    return {
        "action_id": action.action_id,
        "interaction_id": action.interaction_id,
        "component_id": action.component_id,
        "value": action.value,
    }


def action_response(
    action: ActionRequest,
    status: ActionStatus,
    message: str | None = None,
) -> dict[str, Any]:
    return ActionResponse(**action.model_dump(), status=status, message=message).model_dump(mode="json")


@dataclass(frozen=True)
class PendingSurface:
    surface_id: str
    revision: int
    # (interaction_id, component_id) -> allowed action ids
    targets: dict[tuple[str, str], frozenset[str]]


def _surface_targets(surface: CEIUISurface) -> dict[tuple[str, str], frozenset[str]]:
    targets: dict[tuple[str, str], frozenset[str]] = {}
    for component in surface.components:
        interaction_id = getattr(component, "interaction_id", None)
        if interaction_id is None:
            continue
        action_ids: set[str] = set()
        single = getattr(component, "action", None)
        if single is not None:
            action_ids.add(single.action_id)
        for descriptor in getattr(component, "actions", None) or []:
            action_ids.add(descriptor.action_id)
        if action_ids:
            targets[(interaction_id, component.id)] = frozenset(action_ids)
    return targets


class PendingInterrupts:
    """Thread-safe, bounded store of the HITL surface each thread is waiting on."""

    def __init__(self, limit: int = MAX_PENDING_INTERRUPTS) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, PendingSurface] = OrderedDict()

    def register(self, thread_id: str, surface: Mapping[str, Any]) -> PendingSurface:
        parsed = CEIUISurface.model_validate(surface)
        pending = PendingSurface(
            surface_id=parsed.surface_id,
            revision=parsed.revision,
            targets=_surface_targets(parsed),
        )
        with self._lock:
            self._pending[thread_id] = pending
            self._pending.move_to_end(thread_id)
            while len(self._pending) > self._limit:
                self._pending.popitem(last=False)
        return pending

    def get(self, thread_id: str) -> PendingSurface | None:
        with self._lock:
            return self._pending.get(thread_id)

    def matches(self, thread_id: str, action: ActionRequest) -> bool:
        pending = self.get(thread_id)
        if pending is None:
            return False
        if action.surface_id != pending.surface_id or action.revision != pending.revision:
            return False
        allowed = pending.targets.get((action.interaction_id, action.component_id))
        return allowed is not None and action.action_id in allowed

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._pending.pop(thread_id, None)


def check_resume(
    pending: PendingInterrupts,
    thread_id: str,
    action: ActionRequest,
) -> dict[str, Any] | None:
    """Return a terminal envelope when the resume must not run, else ``None``."""
    if action.session_id != thread_id:
        return {
            "action_response": action_response(action, "rejected", "Session does not match."),
            "status": EnvelopeStatus.REJECTED.value,
        }
    if not pending.matches(thread_id, action):
        return {
            "action_response": action_response(
                action, "stale", "This request is no longer waiting for that input."
            ),
            "status": EnvelopeStatus.STALE.value,
        }
    return None


def awaiting_envelope(surface: Mapping[str, Any], text: str) -> dict[str, Any]:
    return {
        "result": text,
        "text": text,
        "ui": dict(surface),
        "status": EnvelopeStatus.AWAITING_INPUT.value,
    }


def completed_envelope(
    text: str,
    ui: Mapping[str, Any] | None,
    *,
    trace_text: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "result": text + trace_text,
        "text": text,
        "status": EnvelopeStatus.COMPLETED.value,
    }
    if ui is not None:
        envelope["ui"] = dict(ui)
    if extra:
        envelope.update(extra)
    return envelope
