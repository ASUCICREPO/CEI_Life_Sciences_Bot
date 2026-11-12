from langchain_core.messages import AIMessage, AIMessageChunk

from drafting.nodes import route_after_evidence, should_research
from shared.response_text import content_text, final_ai_text


def test_content_text_joins_bedrock_text_blocks():
    blocks = [{"type": "text", "text": "## Proposed", "index": 0}, {"type": "text", "text": " Outline", "index": 0}]
    assert content_text(blocks) == "## Proposed Outline"


def test_content_text_drops_non_text_blocks():
    blocks = [{"type": "reasoning_content", "reasoning_content": {"text": "hidden"}}, {"type": "text", "text": "ok"}]
    assert content_text(blocks) == "ok"


def test_content_text_handles_strings_and_none():
    assert content_text("plain") == "plain"
    assert content_text(None) == ""


def test_streamed_chunks_never_leak_python_repr():
    chunks = [AIMessageChunk(content=[{"type": "text", "text": "Hel", "index": 0}]),
              AIMessageChunk(content=[{"type": "text", "text": "lo", "index": 0}])]
    deltas = "".join(content_text(c.content) for c in chunks)
    assert deltas == "Hello"
    assert "'type'" not in deltas


def test_final_ai_text_reads_block_content():
    assert final_ai_text([AIMessage(content=[{"type": "text", "text": "Answer"}])]) == "Answer"


def test_outline_skipped_when_no_approval_needed():
    assert route_after_evidence({"needs_outline_approval": False}) == "draft"
    assert should_research({"research_questions": [], "needs_outline_approval": False}) == "draft"


def test_outline_written_when_approval_needed():
    assert route_after_evidence({"needs_outline_approval": True}) == "outline"
    assert should_research({"research_questions": [], "needs_outline_approval": True}) == "outline"
    assert should_research({"research_questions": ["q"], "needs_outline_approval": False}) == "research"
