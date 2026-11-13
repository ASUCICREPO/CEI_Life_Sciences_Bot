"""CEI UI 1.2 event framing and incremental rendering for Research.

Graph updates are emitted as they happen. Structured synthesis is validated and
grounded as a unit by the existing graph, so its components become available only
after synthesis; this module does not manufacture token deltas from a finished answer.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from shared.streaming.trace_redaction import trace_events_from_entries


class ResearchEventStream:
    def __init__(self) -> None:
        self.sequence = 0
        self.trace_events: list[dict[str, Any]] = []

    def emit(self, kind: str, **body: Any) -> bytes:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        frame = f"id: {self.sequence}\nevent: {kind}\ndata: {payload}\n\n".encode("utf-8")
        self.sequence += 1
        return frame

    def begin(self, surface_id: str) -> bytes:
        return self.emit("surface_begin", surface_id=surface_id, revision=1,
                         content_type="application/vnd.cei.ui+json", protocol="cei-ui",
                         version="1.2", root_id="root")

    def trace(self, entry: Any) -> bytes:
        # trace_events_from_entries redacts keys and values before the live event is built.
        event = trace_events_from_entries([entry])[0]
        event.pop("seq", None)
        event.pop("type", None)
        self.trace_events.append({"seq": self.sequence, "type": "trace_event", **event})
        return self.emit("trace_event", **event)

    def components(self, surface: Mapping[str, Any], *, parent_id: str | None = None, index: int = 0) -> list[bytes]:
        """Emit a presenter's graph parent-first so the frontend can reconstruct it.

        Container child lists are rebuilt through component_add. Tabs retain their
        descriptors and are followed by the referenced child components.
        """
        components = {c["id"]: c for c in surface["components"]}
        records: list[dict[str, Any]] = []
        seen: set[str] = set()

        def visit(component_id: str, parent_id: str | None, index: int) -> None:
            if component_id in seen:
                raise ValueError("stream surfaces must not share child components")
            seen.add(component_id)
            component = dict(components[component_id])
            children = component.get("children")
            if component.get("type") == "Tabs":
                children = [tab["child_id"] for tab in component.get("tabs", [])]
            elif isinstance(children, list):
                component["children"] = []
            else:
                children = []
            args = {"parent_id": parent_id, "component": component}
            if parent_id is not None:
                args["index"] = index
            records.append(args)
            for position, child_id in enumerate(children):
                visit(child_id, component_id, position)

        visit(surface["root_id"], parent_id, index)
        if seen != set(components):
            raise ValueError("stream surface contains unreachable components")
        return [self.emit("component_add", **record) for record in records]


def text_chunks(text: str, limit: int = 4096) -> list[str]:
    """Split on Unicode characters while respecting the SSE text byte limit."""
    chunks: list[str] = []
    current = ""
    size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if size + char_size > limit:
            chunks.append(current)
            current, size = "", 0
        current += char
        size += char_size
    if current:
        chunks.append(current)
    return chunks


async def with_heartbeats(updates: AsyncIterator[Any], *, interval: float = 17.0) -> AsyncIterator[tuple[bool, Any]]:
    """Yield graph values or heartbeats without cancelling a slow node."""
    pending: asyncio.Task | None = None
    try:
        pending = asyncio.create_task(anext(updates))
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield False, None
                continue
            try:
                value = pending.result()
            except StopAsyncIteration:
                break
            yield True, value
            pending = asyncio.create_task(anext(updates))
    finally:
        if pending and not pending.done():
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
        await updates.aclose()
