from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from shared.runtime.conversation import HISTORY_HEADER, prior_conversation_segments, user_turn_with_history
from shared.runtime.guarded_input import Guarded


def _state(*messages):
    return {"messages": list(messages)}


def test_first_turn_has_no_history():
    assert prior_conversation_segments(_state(HumanMessage(content="hi"))) == []
    blocks = user_turn_with_history(_state(HumanMessage(content="hi")), "hi", before="Q: ")
    assert blocks == [{"type": "text", "text": "Q: "}, {"type": "guard_content", "text": "hi"}]


def test_prior_turns_come_first_with_user_text_guarded():
    state = _state(
        HumanMessage(content="Draft a grant on CRISPR"),
        AIMessage(content="Here is the draft about CRISPR."),
        HumanMessage(content="Make section 2 shorter"),
    )
    blocks = user_turn_with_history(state, "Make section 2 shorter", before="User message: ")
    assert blocks[0] == {"type": "text", "text": HISTORY_HEADER}
    assert {"type": "guard_content", "text": "Draft a grant on CRISPR"} in blocks
    assert {"type": "text", "text": "Assistant: Here is the draft about CRISPR.\n"} in blocks
    # The current query is the last guarded block, after the history.
    assert blocks[-1] == {"type": "guard_content", "text": "Make section 2 shorter"}
    # Assistant text is never guarded (B51a).
    assert not any(b["type"] == "guard_content" and "Here is the draft" in b["text"] for b in blocks)


def test_skips_tool_messages_and_keeps_newest_turns():
    msgs = []
    for i in range(15):
        msgs += [HumanMessage(content=f"q{i}"), ToolMessage(content="tool", tool_call_id=str(i)), AIMessage(content=f"a{i}")]
    msgs.append(HumanMessage(content="now"))
    segments = prior_conversation_segments(_state(*msgs), max_turns=3)
    guarded = [s.text for s in segments if isinstance(s, Guarded)]
    assert guarded == ["q12", "q13", "q14"]
    assert not any(isinstance(s, str) and "tool" in s for s in segments)


def test_char_budget_drops_oldest():
    state = _state(
        HumanMessage(content="old " * 50), AIMessage(content="x" * 100),
        HumanMessage(content="new"), AIMessage(content="reply"),
        HumanMessage(content="current"),
    )
    segments = prior_conversation_segments(state, max_chars=50)
    text = "".join(s.text if isinstance(s, Guarded) else s for s in segments)
    assert "new" in text and "reply" in text
    assert "old old" not in text and "x" * 10 not in text


def test_list_content_and_clipping():
    state = _state(
        HumanMessage(content="q"), AIMessage(content=[{"type": "text", "text": "y" * 50}]), HumanMessage(content="c"),
    )
    segments = prior_conversation_segments(state, max_message_chars=10)
    assert any(isinstance(s, str) and s.startswith("Assistant: yyyyyyyyy…") for s in segments)
