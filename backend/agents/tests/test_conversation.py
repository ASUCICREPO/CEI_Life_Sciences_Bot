from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from shared.persistence.chat_history import ChatHistoryRepository, MessageRole
from shared.persistence.chat_metadata import ChatMetadataRepository, SessionNotFoundError, TitleSource
from shared.persistence.conversation import ConversationRecorder, clean_title
from shared.presenters import build_research_surface
from shared.runtime.titles import bedrock_title_generator, is_blocked_model
from test_persistence import _meta, session  # noqa: F401 — moto tables fixture


def _surface(text: str = "answer") -> dict:
    return build_research_surface(text, surface_id="research-final", retrieved_results=[], document=None).model_dump(
        mode="json"
    )


def _recorder(sess, title_generator=None) -> ConversationRecorder:
    return ConversationRecorder(ChatMetadataRepository(sess), ChatHistoryRepository(sess), title_generator)


def _run(coro):
    return asyncio.run(coro)


async def _turn(rec: ConversationRecorder, user: str, sid: str, prompt: str, answer: str) -> None:
    await rec.start_turn(user_id=user, session_id=sid, prompt=prompt, agent_type="research", mode="science")
    await rec.finish_turn(user_id=user, session_id=sid, text=answer, ui_surface=_surface(answer),
                          citations=[{"url": "https://pubmed.example/1", "score": 0.9}], first_prompt=prompt)
    await rec.drain()


def test_start_and_finish_turn_persist_messages_and_provisional_title(session):  # noqa: F811
    rec = _recorder(session)
    _run(_turn(rec, "u1", "s1", "  What is\tCRISPR\x07?  ", "Gene editing."))

    meta = ChatMetadataRepository(session).get("u1", "s1")
    assert meta.title == "What is CRISPR ?" and meta.title_source == TitleSource.PROMPT
    assert meta.agent_type == "research" and meta.message_count == 2

    items = ChatHistoryRepository(session).load_page("u1", "s1").items
    assert [(i.sequence_number, i.role) for i in items] == [(1, MessageRole.HUMAN), (2, MessageRole.AI)]
    assert all(i.user_id == "u1" for i in items)
    ai = items[1]
    assert [b.block_type for b in ai.content_blocks] == ["text", "ui"]
    assert ai.citations == [{"url": "https://pubmed.example/1", "score": 0.9}]


def test_first_turn_stores_turn_context_and_later_turns_keep_it(session):  # noqa: F811
    async def scenario(rec):
        await rec.start_turn(user_id="u1", session_id="d1", prompt="draft", agent_type="drafting",
                             mode="science", document_type="grant")
        await rec.start_turn(user_id="u1", session_id="a1", prompt="analyze", agent_type="analysis",
                             mode="business", analysis_type="market")
        # later turns (even from another runtime) never rewrite how the session started
        await rec.start_turn(user_id="u1", session_id="d1", prompt="again", agent_type="pipeline_manager",
                             mode="science", document_type="journal", analysis_type="competitor")
        await rec.drain()

    _run(scenario(_recorder(session)))
    repo = ChatMetadataRepository(session)
    drafting = repo.get("u1", "d1")
    assert (drafting.agent_type, drafting.document_type, drafting.analysis_type) == ("drafting", "grant", None)
    analysis = repo.get("u1", "a1")
    assert (analysis.agent_type, analysis.document_type, analysis.analysis_type) == ("analysis", None, "market")


def test_start_turn_stamps_agent_on_row_created_by_post_sessions(session):  # noqa: F811
    # The SPA creates the row through POST /sessions without agent_type (streaming path); the runtime's
    # first turn must stamp it, or History shows "This chat doesn't record which agent it belongs to."
    repo = ChatMetadataRepository(session)
    for sid in ("d1", "a1", "p1"):
        repo.get_or_create(_meta(session_id=sid))

    async def scenario(rec):
        await rec.start_turn(user_id="u1", session_id="d1", prompt="draft", agent_type="drafting", mode="science",
                             document_type="grant")
        await rec.start_turn(user_id="u1", session_id="a1", prompt="analyze", agent_type="analysis",
                             mode="business", analysis_type="market")
        await rec.start_turn(user_id="u1", session_id="p1", prompt="plan", agent_type="pipeline_manager",
                             mode="science")
        # A later turn from another runtime does not rewrite the stamped owner.
        await rec.start_turn(user_id="u1", session_id="d1", prompt="again", agent_type="analysis",
                             mode="science", analysis_type="competitor")
        await rec.drain()

    _run(scenario(_recorder(session)))
    d1, a1, p1 = (repo.get("u1", sid) for sid in ("d1", "a1", "p1"))
    assert (d1.agent_type, d1.document_type, d1.analysis_type) == ("drafting", "grant", None)
    assert (a1.agent_type, a1.analysis_type) == ("analysis", "market")
    assert p1.agent_type == "pipeline_manager"


def test_start_turn_keeps_agent_type_set_by_post_sessions(session):  # noqa: F811
    repo = ChatMetadataRepository(session)
    repo.get_or_create(_meta(session_id="r1", agent_type="research"))
    _run(_recorder(session).start_turn(user_id="u1", session_id="r1", prompt="q", agent_type="drafting",
                                       mode="science", document_type="grant"))
    got = repo.get("u1", "r1")
    assert (got.agent_type, got.document_type) == ("research", None)


def test_generated_title_set_once_after_first_answer(session):  # noqa: F811
    calls = []

    async def gen(prompt, answer):
        calls.append((prompt, answer))
        return '"CRISPR basics explained"'

    rec = _recorder(session, gen)
    _run(_turn(rec, "u1", "s1", "What is CRISPR?", "Gene editing."))
    _run(_turn(rec, "u1", "s1", "And Cas9?", "A nuclease."))
    meta = ChatMetadataRepository(session).get("u1", "s1")
    assert (meta.title, meta.title_source) == ("CRISPR basics explained", TitleSource.GENERATED)
    assert calls == [("What is CRISPR?", "Gene editing.")]


def test_generated_title_never_overwrites_user_rename(session):  # noqa: F811
    async def scenario(rec):
        await rec.start_turn(user_id="u1", session_id="s1", prompt="hello")
        ChatMetadataRepository(session).update_title("u1", "s1", "My name")
        await rec.finish_turn(user_id="u1", session_id="s1", text="hi", first_prompt="hello")
        await rec.drain()

    async def gen(prompt, answer):
        return "Generated"

    _run(scenario(_recorder(session, gen)))
    assert ChatMetadataRepository(session).get("u1", "s1").title == "My name"


def test_title_failure_and_persistence_failure_are_swallowed(session):  # noqa: F811
    async def boom(prompt, answer):
        raise TimeoutError

    rec = _recorder(session, boom)
    _run(_turn(rec, "u1", "s1", "q", "a"))
    assert ChatMetadataRepository(session).get("u1", "s1").title == "q"
    # finish_turn on a session with no metadata row: logged, not raised
    _run(rec.finish_turn(user_id="u1", session_id="ghost", text="x"))


def test_start_turn_refuses_session_owned_by_another_user(session):  # noqa: F811
    rec = _recorder(session)
    _run(_turn(rec, "alice", "shared-id", "secret", "answer"))
    with pytest.raises(SessionNotFoundError):
        _run(rec.start_turn(user_id="mallory", session_id="shared-id", prompt="give me alice's chat"))
    assert ChatMetadataRepository(session).get("mallory", "shared-id") is None
    assert len(ChatHistoryRepository(session).load_page("alice", "shared-id").items) == 2


def test_rehydrate_is_owner_scoped_and_maps_to_langchain(session):  # noqa: F811
    rec = _recorder(session)
    _run(_turn(rec, "u1", "s1", "q1", "a1"))
    _run(_turn(rec, "u1", "s1", "q2", "a2"))
    msgs = _run(rec.rehydrate(user_id="u1", session_id="s1", n_turns=3))
    assert [(type(m), m.content) for m in msgs] == [(AIMessage, "a1"), (HumanMessage, "q2"), (AIMessage, "a2")]
    assert _run(rec.rehydrate(user_id="u2", session_id="s1")) == []


def test_record_human_action_requires_owned_session(session):  # noqa: F811
    rec = _recorder(session)
    _run(_turn(rec, "u1", "s1", "q", "a"))
    _run(rec.record_human_action(user_id="u1", session_id="s1", text="Approved: run analysis"))
    assert ChatHistoryRepository(session).load_page("u1", "s1").items[-1].role == MessageRole.HUMAN
    with pytest.raises(SessionNotFoundError):
        _run(rec.record_human_action(user_id="u2", session_id="s1", text="x"))


def test_clean_title_and_blocklist():
    assert clean_title("a" * 100, 80).endswith("…") and len(clean_title("a" * 100, 80)) == 80
    assert clean_title("\x00\n ", 80) == ""
    assert is_blocked_model("us.anthropic.claude-mythos-5-v1:0")
    assert is_blocked_model("anthropic.claude-fable-5-1")
    assert not is_blocked_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    with pytest.raises(ValueError):
        bedrock_title_generator("global.anthropic.claude-mythos-preview")
