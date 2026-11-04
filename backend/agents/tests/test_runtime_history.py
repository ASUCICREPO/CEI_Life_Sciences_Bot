from __future__ import annotations

import asyncio
import base64
import json

import pytest

import pipeline_main
import shared.runtime.history as history_hooks
from shared.config import config
from shared.persistence.chat_history import ChatHistoryRepository, MessageRole
from shared.persistence.chat_metadata import ChatMetadataRepository, SessionNotFoundError
from shared.persistence.conversation import ConversationRecorder
from shared.runtime.history import begin_turn, describe_action
from shared.runtime.identity import UnauthenticatedError, resolve_user_id
from test_pipeline_main import _approve_action, _FakeGraph, _plan  # noqa: F401
from test_persistence import session  # noqa: F401 — moto tables fixture


def _unsigned_jwt(claims: dict) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


class _Ctx:
    def __init__(self, session_id: str, sub: str | None = None) -> None:
        self.session_id = session_id
        self.request_headers = {"authorization": f"Bearer test-token:{sub}"} if sub else None


# -- identity ---------------------------------------------------------------------------------------------------


def test_verified_sub_wins_over_payload_user_id():
    assert resolve_user_id({"user_id": "spoofed"}, _Ctx("s", sub="cognito-sub")) == "cognito-sub"


def test_no_unauthenticated_path():
    assert not hasattr(config, "direct_mode")
    for payload in ({"user_id": "local-demo"}, {}):
        with pytest.raises(UnauthenticatedError):
            resolve_user_id(payload, _Ctx("s"))


def test_unsigned_jwt_is_rejected():
    ctx = _Ctx("s")
    ctx.request_headers = {"Authorization": f"Bearer {_unsigned_jwt({'sub': 'forged'})}"}
    with pytest.raises(UnauthenticatedError):
        resolve_user_id({}, ctx)


# -- begin_turn / rehydration -----------------------------------------------------------------------------------


def _recorder(sess) -> ConversationRecorder:
    return ConversationRecorder(ChatMetadataRepository(sess), ChatHistoryRepository(sess))


def test_begin_turn_rehydrates_cold_thread_without_the_new_prompt(session):  # noqa: F811
    rec = _recorder(session)

    async def scenario():
        await begin_turn(user_id="u1", session_id="s1", prompt="q1", agent_type="research", mode="science",
                         warm=False, recorder=rec)
        await rec.finish_turn(user_id="u1", session_id="s1", text="a1")
        warm = await begin_turn(user_id="u1", session_id="s1", prompt="q2", agent_type="research",
                                mode="science", warm=True, recorder=rec)
        await rec.finish_turn(user_id="u1", session_id="s1", text="a2")
        cold = await begin_turn(user_id="u1", session_id="s1", prompt="q3", agent_type="research",
                                mode="science", warm=False, recorder=rec)
        return warm, cold

    warm, cold = asyncio.run(scenario())
    assert warm == []
    assert [m.content for m in cold] == ["q1", "a1", "q2", "a2"]
    assert len(ChatHistoryRepository(session).load_page("u1", "s1").items) == 5


def test_begin_turn_refuses_foreign_session(session):  # noqa: F811
    rec = _recorder(session)

    async def scenario():
        await begin_turn(user_id="alice", session_id="s1", prompt="q", agent_type="research", mode=None,
                         warm=False, recorder=rec)
        await begin_turn(user_id="mallory", session_id="s1", prompt="q", agent_type="research", mode=None,
                         warm=False, recorder=rec)

    with pytest.raises(SessionNotFoundError):
        asyncio.run(scenario())


def test_begin_turn_survives_persistence_outage():
    class Broken(ConversationRecorder):
        def __init__(self):
            pass

        async def start_turn(self, **_):
            raise ConnectionError("dynamodb down")

    out = asyncio.run(begin_turn(user_id="u", session_id="s", prompt="q", agent_type="x", mode=None,
                                 warm=False, recorder=Broken()))
    assert out == []


# -- entrypoint wiring (pipeline: run -> HITL resume -> async job) ----------------------------------------------


@pytest.fixture
def pipeline_env(monkeypatch, session):  # noqa: F811
    monkeypatch.setenv("CEI_DEMO_REPLAY", "off")
    for store in (pipeline_main._pending._pending, pipeline_main._thread_prompts, pipeline_main._thread_ids,
                  pipeline_main._background_tasks):
        store.clear()
    rec = _recorder(session)
    monkeypatch.setattr(history_hooks, "get_recorder", lambda: rec)
    graph = _FakeGraph(_plan())
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: graph)
    return session


def test_pipeline_records_prompt_hitl_answer_and_job_result_under_jwt_identity(pipeline_env):
    sid = "sess-history-1"

    async def scenario():
        run = await pipeline_main.invoke({"prompt": "Research TP53", "user_id": "spoofed"}, _Ctx(sid, sub="sub-1"))
        action = _approve_action(run["ui"], sid)
        resumed = await pipeline_main.invoke({"op": "resume", "action": action}, _Ctx(sid, sub="sub-1"))
        # the job may already have finished and removed itself (same guard as test_pipeline_main)
        task = pipeline_main._background_tasks.get(resumed["job_id"])
        if task is not None:
            await task

    asyncio.run(scenario())
    items = ChatHistoryRepository(pipeline_env).load_page("sub-1", sid).items
    assert [i.role for i in items] == [MessageRole.HUMAN, MessageRole.AI, MessageRole.HUMAN, MessageRole.AI,
                                       MessageRole.AI]
    assert {i.user_id for i in items} == {"sub-1"}
    assert items[0].content_blocks[0].body == "Research TP53"
    assert items[2].content_blocks[0].body == "Selected: approve-plan"
    assert items[-1].content_blocks[0].body == "Final synthesized answer"
    meta = ChatMetadataRepository(pipeline_env).get("sub-1", sid)
    assert meta.agent_type == "pipeline_manager" and meta.message_count == 5
    assert ChatMetadataRepository(pipeline_env).get("spoofed", sid) is None


def test_describe_action_renders_value():
    from types import SimpleNamespace

    assert describe_action(SimpleNamespace(action_id="ok", value=None)) == "Selected: ok"
    assert describe_action(SimpleNamespace(action_id="pick", value={"a": 1})) == 'Selected: pick — {"a": 1}'
