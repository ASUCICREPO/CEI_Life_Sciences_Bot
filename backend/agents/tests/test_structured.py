from __future__ import annotations

import json
from unittest.mock import Mock

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from shared.structured import invoke_structured


class _Section(BaseModel):
    heading: str


class _Doc(BaseModel):
    title: str
    sections: list[_Section]


def _model_returning(result: dict) -> Mock:
    model = Mock()
    model.with_structured_output.return_value.invoke.return_value = result
    return model


def _raw(args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "_Doc", "args": args, "id": "t1"}])


def test_repairs_list_field_sent_as_json_string() -> None:
    raw = _raw({"title": "T", "sections": json.dumps([{"heading": "A"}])})
    parsed, got_raw, error = invoke_structured(
        _model_returning({"raw": raw, "parsed": None, "parsing_error": ValueError("bad")}), _Doc, []
    )
    assert error is None and got_raw is raw
    assert parsed == _Doc(title="T", sections=[_Section(heading="A")])


def test_unrepairable_output_still_returns_error() -> None:
    raw = _raw({"title": "T"})  # sections missing: nothing to decode
    parsed, _, error = invoke_structured(
        _model_returning({"raw": raw, "parsed": None, "parsing_error": ValueError("missing")}), _Doc, []
    )
    assert parsed is None and error is not None


def test_bad_json_string_is_not_repaired() -> None:
    raw = _raw({"title": "T", "sections": "[not json"})
    parsed, _, error = invoke_structured(
        _model_returning({"raw": raw, "parsed": None, "parsing_error": ValueError("bad")}), _Doc, []
    )
    assert parsed is None and error is not None


def test_streamed_prose_is_emitted_incrementally_without_other_block_data() -> None:
    from langchain_core.messages import AIMessageChunk

    model = Mock()
    bound = model.with_structured_output.return_value
    fragments = ['{"sections":[{"blocks":[{"kind":"prose","markdown":"Hello',
                 ' world"},{"kind":"callout","title":"private"}]}]}']
    bound.stream.return_value = iter([
        {"raw": AIMessageChunk(content="", tool_call_chunks=[{"name": "ResponseDocument", "args": fragment, "id": "call", "index": 0}])}
        for fragment in fragments
    ] + [{"parsed": {"done": True}}])
    deltas = []
    parsed, _, error = invoke_structured(model, _Doc, [], on_prose_delta=deltas.append)
    assert parsed == {"done": True}
    assert error is None
    assert len(deltas) >= 2
    assert "".join(deltas) == "Hello world"
    assert "private" not in "".join(deltas)
