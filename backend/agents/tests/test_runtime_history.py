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
from shared.runtime.identity import AgeNotVerifiedError, UnauthenticatedError, resolve_user_id
from tests.conftest import NO_AGE_VERIFIED_TOKEN_PREFIX
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


def test_resolve_user_id_raises_when_the_age_verified_claim_is_missing():
    ctx = _Ctx("s")
    ctx.request_headers = {"Authorization": f"Bearer {NO_AGE_VERIFIED_TOKEN_PREFIX}cognito-sub"}
    with pytest.raises(AgeNotVerifiedError):
        resolve_user_id({}, ctx)


def test_resolve_user_id_succeeds_when_the_age_verified_claim_is_present():
    assert resolve_user_id({}, _Ctx("s", sub="cognito-sub")) == "cognito-sub"


# -- begin_turn / rehydration -----------------------------------------------------------------------------------


def _recorder(sess) -> ConversationRecorder:
    return ConversationRecorder(ChatMetadataRepository(sess), ChatHistoryRepository(sess))


def test_completed_marker_is_persisted_only_when_explicit(session):  # noqa: F811
    rec = _recorder(session)

    async def scenario():
        await begin_turn(user_id="u1", session_id="s1", prompt="q", agent_type="drafting", mode="science",
                         warm=False, recorder=rec)
        await history_hooks.end_turn(user_id="u1", session_id="s1", envelope={"text": "Ready", "status": "completed"}, recorder=rec)
        await history_hooks.end_turn(user_id="u1", session_id="s1", envelope={"text": "Unknown"}, recorder=rec)

    asyncio.run(scenario())
    turns = ChatHistoryRepository(session).load_page("u1", "s1").items
    assert [turn.completion_status for turn in turns] == [None, "completed", None]


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


def test_begin_turn_survives_persistence_outage(caplog):
    class Broken(ConversationRecorder):
        def __init__(self):
            pass

        async def start_turn(self, **_):
            raise ConnectionError("dynamodb down")

    out = asyncio.run(begin_turn(user_id="u", session_id="s", prompt="q", agent_type="x", mode=None,
                                 warm=False, recorder=Broken()))
    assert out == []
    assert "ConnectionError" in caplog.text
    assert "dynamodb down" not in caplog.text


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
    sid = "0b0c4f7e-5555-4a55-8a55-000000000001"

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


# -- session guard: ownership first, fail closed, UUID-only session ids -----------------------------------------


def _status_and_body(response):
    if isinstance(response, dict):
        return 200, response
    return response.status_code, json.loads(response.body)


async def _run_to_job(sid: str, sub: str) -> str:
    run = await pipeline_main.invoke({"prompt": "Research TP53"}, _Ctx(sid, sub=sub))
    resumed = await pipeline_main.invoke({"op": "resume", "action": _approve_action(run["ui"], sid)}, _Ctx(sid, sub=sub))
    task = pipeline_main._background_tasks.get(resumed["job_id"])
    if task is not None:
        await task
    return resumed["job_id"]


def test_job_status_answers_only_the_job_owner(pipeline_env):
    sid = "0b0c4f7e-5555-4a55-8a55-000000000002"

    async def scenario():
        job_id = await _run_to_job(sid, "alice")
        own = await pipeline_main.invoke({"op": "job_status", "job_id": job_id}, _Ctx(sid, sub="alice"))
        stolen = await pipeline_main.invoke({"op": "job_status", "job_id": job_id}, _Ctx(sid, sub="mallory"))
        return own, stolen

    own, stolen = asyncio.run(scenario())
    assert own["status"] == "completed"
    assert _status_and_body(stolen) == (404, {"status": "error", "message": "Session not found."})


def test_job_status_requires_authentication(pipeline_env):
    sid = "0b0c4f7e-5555-4a55-8a55-000000000003"
    response = asyncio.run(pipeline_main.invoke({"op": "job_status", "job_id": "job-x"}, _Ctx(sid)))
    assert _status_and_body(response)[0] == 401


def test_job_registry_refuses_jobs_without_matching_owner():
    from shared.runtime.jobs import JobRegistry

    registry = JobRegistry()
    owned = registry.create(session_id="s", agent_type="pipeline_manager", user_id="alice")
    orphan = registry.create(session_id="s", agent_type="pipeline_manager")
    assert registry.get_owned(owned.job_id, user_id="alice", session_id="s") is owned
    assert registry.get_owned(owned.job_id, user_id="mallory", session_id="s") is None
    assert registry.get_owned(owned.job_id, user_id="alice", session_id="other") is None
    assert registry.get_owned(orphan.job_id, user_id="alice", session_id="s") is None


@pytest.mark.parametrize("bad_sid", [None, "", "default", "sess-1", "0b0c4f7e55554a558a55000000000004"])
def test_runtime_refuses_missing_or_non_uuid_session_id(pipeline_env, monkeypatch, bad_sid):
    graph_calls = []
    monkeypatch.setattr(pipeline_main, "_compile_graph", lambda: graph_calls.append(1))
    for payload in ({"prompt": "hi"}, {"op": "job_status", "job_id": "job-x"}):
        response = asyncio.run(pipeline_main.invoke(payload, _Ctx(bad_sid, sub="alice")))
        assert _status_and_body(response) == (400, {"status": "error", "message": "A valid session id is required."})
    assert graph_calls == [] and not pipeline_main._thread_prompts


def test_foreign_session_is_refused_before_any_session_work(pipeline_env):
    sid = "0b0c4f7e-5555-4a55-8a55-000000000005"

    async def scenario():
        await _run_to_job(sid, "alice")
        pipeline_main._thread_prompts.clear()
        return await pipeline_main.invoke({"prompt": "give me alice's thread"}, _Ctx(sid, sub="mallory"))

    response = asyncio.run(scenario())
    assert _status_and_body(response)[0] == 404
    assert sid not in pipeline_main._thread_prompts  # nothing about the session was touched
    assert ChatMetadataRepository(pipeline_env).get("mallory", sid) is None


def test_owner_lookup_failure_returns_generic_500_and_logs_cause_type(pipeline_env, monkeypatch, caplog):
    from shared.persistence.ownership import GENERIC_INTERNAL_ERROR

    def broken_owner_of(self, session_id):
        raise ConnectionError("ProvisionedThroughputExceededException: table ChatHistoryStore")

    monkeypatch.setattr(ChatHistoryRepository, "owner_of", broken_owner_of)
    sid = "0b0c4f7e-5555-4a55-8a55-000000000006"
    with caplog.at_level("ERROR"):
        responses = [
            asyncio.run(pipeline_main.invoke(payload, _Ctx(sid, sub="alice")))
            for payload in ({"prompt": "hi"}, {"op": "job_status", "job_id": "job-x"})
        ]
    for response in responses:
        assert _status_and_body(response) == (500, {"status": "error", "message": GENERIC_INTERNAL_ERROR})
    assert sid not in pipeline_main._thread_prompts
    assert "ownership check failed" in caplog.text
    assert "ConnectionError" in caplog.text
    assert "ProvisionedThroughputExceededException" not in caplog.text


def test_begin_turn_never_fails_open_on_owner_lookup_errors(session):  # noqa: F811
    from shared.persistence.ownership import OwnershipCheckError

    rec = _recorder(session)

    def broken_owner_of(session_id):
        raise ConnectionError("dynamodb down")

    rec._history.owner_of = broken_owner_of
    with pytest.raises(OwnershipCheckError):
        asyncio.run(begin_turn(user_id="u1", session_id="s1", prompt="q", agent_type="research", mode=None,
                               warm=False, recorder=rec))
