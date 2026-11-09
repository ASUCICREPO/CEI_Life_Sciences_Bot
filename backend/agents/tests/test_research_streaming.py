import asyncio
import json

import pytest

from shared.state import TraceEntry
from shared.streaming.research import ResearchEventStream, with_heartbeats


def decode(frame):
    fields = dict(line.split(": ", 1) for line in frame.decode().strip().splitlines())
    return int(fields["id"]), fields["event"], json.loads(fields["data"])


def test_parent_first_tabs_and_children_reconstruction_order():
    stream = ResearchEventStream()
    surface = {
        "root_id": "root",
        "components": [
            {"id": "body", "type": "Markdown", "content": "Result"},
            {"id": "card", "type": "Card", "children": ["body"]},
            {"id": "root", "type": "Tabs", "tabs": [{"label": "Answer", "child_id": "card"}]},
        ],
    }
    events = [decode(item) for item in stream.components(surface)]
    assert [item[0] for item in events] == [0, 1, 2]
    assert [item[2]["component"]["id"] for item in events] == ["root", "card", "body"]
    assert [item[2]["parent_id"] for item in events] == [None, "root", "card"]
    assert events[1][2]["component"]["children"] == []
    assert "children" not in events[2][2]["component"]


def test_invalid_graph_does_not_consume_sequence_numbers():
    stream = ResearchEventStream()
    with pytest.raises(ValueError):
        stream.components({"root_id": "root", "components": [
            {"id": "root", "type": "Column", "children": []},
            {"id": "orphan", "type": "Markdown", "content": "not reachable"},
        ]})
    assert decode(stream.emit("error", code="internal_error", message="failed"))[0] == 0


def test_trace_redacted_before_transmission_and_recording():
    stream = ResearchEventStream()
    frame = stream.trace(TraceEntry(stage="TOOL", detail="api_key=secret-value"))
    seq, kind, body = decode(frame)
    assert kind == "trace_event"
    assert "secret-value" not in frame.decode()
    assert stream.trace_events == [{"seq": seq, "type": kind, **body}]


@pytest.mark.asyncio
async def test_heartbeats_do_not_cancel_slow_update():
    async def source():
        await asyncio.sleep(0.03)
        yield "done"
    values = [item async for item in with_heartbeats(source(), interval=0.005)]
    assert any(not present for present, _ in values)
    assert values[-1] == (True, "done")
