"""B7 never-show: model ids, guardrails, system prompts, prompt versions and secrets never reach a trace."""
from __future__ import annotations

import asyncio
import json

import pytest

from shared.runtime import history
from shared.state import TraceEntry
from shared.streaming.trace_redaction import (
    REDACTED,
    redact_entries,
    redact_trace_event,
    redacts_trace_input,
    redacts_trace_output,
    trace_events_from_entries,
)

SENSITIVE = [
    "invoking anthropic.claude-3-5-sonnet-20240620-v1:0",
    "model us.anthropic.claude-sonnet-4-20250514-v1:0",
    "amazon.titan-embed-text-v2:0",
    "meta.llama3-70b-instruct-v1:0",
    "arn:aws:bedrock:us-east-1:123456789012:inference-profile/x",
    "guardrailIdentifier=abc123",
    "Applied guardrail gr-1 v2",
    "system_prompt: You are CEI...",
    "the System Prompt says",
    "prompt_version=7",
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:ncbi",
    "key AKIAABCDEFGHIJKLMNOP",
    "token sk-abcdefghijklmnop",
    "NCBI api_key=deadbeef",
    "Authorization: Bearer eyJhbGciOi",
]

SAFE = ["Searching PubMed for BRCA1", "Retrieved 12 results", "Ranking evidence"]


@pytest.mark.parametrize("value", SENSITIVE)
def test_sensitive_values_are_redacted(value):
    event = {"reasoning_text": value, "tool_output_summary": value, "sources": [value]}
    assert redact_trace_event(event) == {"reasoning_text": REDACTED, "tool_output_summary": REDACTED, "sources": [REDACTED]}


@pytest.mark.parametrize("value", SAFE)
def test_safe_values_pass_through(value):
    assert redact_trace_event({"reasoning_text": value}) == {"reasoning_text": value}


def test_only_allow_listed_fields_survive():
    event = {
        "seq": 3,
        "type": "trace_event",
        "ts": "2026-09-29T00:00:03.210Z",
        "step_type": "tool_call",
        "tool_name": "pubmed___search",
        "tool_input": {"query": "GLP-1"},
        "tool_output_summary": "12 results retrieved",
        "sources": ["PMID:38291045"],
        "reasoning_text": "ok",
        "reasoning_label": "summarized",
        "model_id": "anthropic.claude-3",
        "guardrail_config": {"id": "g"},
        "system_prompt": "secret",
        "prompt_version": "7",
        "usage": {"input_tokens": 3},
    }
    assert set(redact_trace_event(event)) == {
        "seq", "type", "ts", "step_type", "tool_name", "tool_input", "tool_output_summary", "sources",
        "reasoning_text", "reasoning_label",
    }


def test_tool_input_drops_never_show_keys_and_scans_nested_values():
    event = {"tool_input": {
        "query": "BRCA1",
        "api_key": "deadbeef",
        "modelId": "x",
        "options": {"system_prompt": "You are", "auth": "Bearer abc", "limit": 5, "tags": ["ok", "sk-abcdefghijkl"]},
    }}
    assert redact_trace_event(event) == {"tool_input": {
        "query": "BRCA1", "options": {"auth": REDACTED, "limit": 5, "tags": ["ok", REDACTED]},
    }}


def test_formatter_decorator_redacts_user_visible_trace_text():
    @redacts_trace_input
    def fmt(entries):
        return "\n".join(f"[{e.stage}] {e.detail}" for e in entries)

    text = fmt([TraceEntry(stage="LLM", detail=v) for v in SENSITIVE] + [{"stage": "SEARCH", "detail": SAFE[0]}])
    assert "SEARCH] " + SAFE[0] in text
    for value in SENSITIVE:
        assert value not in text


def test_producer_decorator_redacts_returned_entries():
    @redacts_trace_output
    def produce():
        return [TraceEntry(stage="STEP#1>LLM", detail="model anthropic.claude-3-haiku-20240307-v1:0")]

    assert produce()[0].detail == REDACTED


def test_entrypoint_formatters_are_wrapped():
    import analysis_main
    import pipeline_main
    from pipeline_manager.dispatcher import _prefixed_trace

    leak = [TraceEntry(stage="LLM", detail="arn:aws:bedrock:us-east-1:1:guardrail/x", input_tokens=1)]
    for formatter in (analysis_main._format_trace, pipeline_main._format_trace):
        assert "arn:aws:bedrock" not in formatter(leak)
    assert _prefixed_trace("s1", leak)[0].detail == REDACTED


class _Repo:
    def __init__(self):
        self.saved = None

    def save(self, user_id, session_id, turn_seq, message_id, events):
        self.saved = (user_id, session_id, turn_seq, message_id, events)
        return True


class _Recorder:
    def __init__(self):
        self.message_id = None

    async def finish_turn(self, **kwargs):
        self.message_id = kwargs["message_id"]
        return 4


def test_end_turn_persists_redacted_trace_under_the_ai_turn(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(history, "get_trace_repository", lambda: repo)
    rec = _Recorder()
    trace = [TraceEntry(stage="LLM", detail="using us.anthropic.claude-sonnet-4"), TraceEntry(stage="SEARCH", detail="ok")]
    asyncio.run(history.end_turn(user_id="u", session_id="s", envelope={"text": "a"}, recorder=rec, trace=trace))
    user_id, session_id, turn_seq, message_id, events = repo.saved
    assert (user_id, session_id, turn_seq, message_id) == ("u", "s", 4, rec.message_id)
    assert "anthropic" not in json.dumps(events)
    assert events[0]["reasoning_text"] == f"[LLM] {REDACTED}"
    assert events[1] == {
        "seq": 1, "type": "trace_event", "step_type": "reasoning", "reasoning_text": "[SEARCH] ok",
        "reasoning_label": "summarized",
    }


def test_trace_save_failure_never_fails_the_turn(monkeypatch):
    class _Broken:
        def save(self, *args):
            raise RuntimeError("ddb down")

    monkeypatch.setattr(history, "get_trace_repository", lambda: _Broken())
    asyncio.run(
        history.end_turn(user_id="u", session_id="s", envelope={"text": "a"}, recorder=_Recorder(), trace=[TraceEntry(stage="x", detail="y")])
    )


def test_repository_applies_second_redaction_layer():
    from shared.persistence.traces import TraceRepository

    puts = []

    class _Table:
        def put_item(self, **kwargs):
            puts.append(kwargs["Item"])

    TraceRepository(table=_Table()).save("u", "s", 1, "m", [{"reasoning_text": "sk-abcdefghijklmnop", "system_prompt": "x"}])
    chunk = next(item for item in puts if item["record_type"] == "trace_chunk")
    assert json.loads(chunk["data"]) == [{"reasoning_text": REDACTED}]


def test_trace_events_from_entries_and_redact_entries_accept_dicts():
    entries = [{"stage": "A", "detail": "guardrail hit"}]
    assert trace_events_from_entries(entries)[0]["reasoning_text"] == f"[A] {REDACTED}"
    assert redact_entries(entries)[0].detail == REDACTED
