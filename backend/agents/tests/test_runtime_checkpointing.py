"""Runtime wiring of short-term-memory checkpoints: begin_turn/end_turn hooks and cold HITL resume."""
from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

import shared.runtime.checkpointing as checkpointing
from shared.persistence.checkpoints import CheckpointRepository
from shared.runtime.history import begin_turn, end_turn
from test_checkpoints import SID, USER, _cfg, _graph, _interrupted_saver, table  # noqa: F401 — moto fixture


class _Recorder:
    def __init__(self, sequence: int | None = 3) -> None:
        self.sequence = sequence
        self.rehydrated = 0

    async def start_turn(self, **_: object) -> None:
        return None

    async def finish_turn(self, **_: object) -> int | None:
        return self.sequence

    async def rehydrate(self, **_: object) -> list:
        self.rehydrated += 1
        return [HumanMessage(content="old text turn")]


class _Pending:
    def __init__(self) -> None:
        self.registered: dict = {}

    def register(self, thread_id: str, surface) -> None:  # noqa: ANN001
        self.registered[thread_id] = dict(surface)


def _use(monkeypatch, repo) -> None:  # noqa: ANN001
    monkeypatch.setattr(checkpointing, "get_checkpoint_repository", lambda: repo)


def test_end_turn_snapshots_and_cold_begin_turn_restores_instead_of_text(table, monkeypatch):  # noqa: F811
    _use(monkeypatch, CheckpointRepository(table=table))
    rec = _Recorder(sequence=3)

    async def scenario():
        await end_turn(user_id=USER, session_id=SID, envelope={"text": "a"}, recorder=rec,
                       saver=_interrupted_saver())
        await checkpointing.drain_checkpoints()
        fresh = InMemorySaver()
        history = await begin_turn(user_id=USER, session_id=SID, prompt="q2", agent_type="research", mode=None,
                                   warm=False, recorder=rec, saver=fresh)
        return history, fresh

    history, fresh = asyncio.run(scenario())
    assert history == [] and rec.rehydrated == 0
    state = _graph(fresh).get_state(_cfg())
    assert [m.content for m in state.values["messages"]] == ["hi"] and state.interrupts


def test_cold_begin_turn_falls_back_to_text_when_no_snapshot(table, monkeypatch):  # noqa: F811
    _use(monkeypatch, CheckpointRepository(table=table))
    rec = _Recorder()
    history = asyncio.run(begin_turn(user_id=USER, session_id=SID, prompt="q", agent_type="research", mode=None,
                                     warm=False, recorder=rec, saver=InMemorySaver()))
    assert [m.content for m in history] == ["old text turn"] and rec.rehydrated == 1


def test_other_users_snapshot_is_never_restored(table, monkeypatch):  # noqa: F811
    repo = CheckpointRepository(table=table)
    assert repo.save_from_saver(USER, SID, _interrupted_saver(), version=3)
    _use(monkeypatch, repo)
    rec = _Recorder()
    fresh = InMemorySaver()
    history = asyncio.run(begin_turn(user_id="mallory", session_id=SID, prompt="q", agent_type="research",
                                     mode=None, warm=False, recorder=rec, saver=fresh))
    assert rec.rehydrated == 1 and history  # fell back; nothing of USER's state loaded
    assert fresh.get_tuple(_cfg()) is None


def test_no_snapshot_when_answer_was_not_recorded(monkeypatch):
    calls: list = []

    class _Repo:
        def save(self, *args: object) -> bool:
            calls.append(args)
            return True

    _use(monkeypatch, _Repo())

    async def scenario():
        await end_turn(user_id=USER, session_id=SID, envelope={"text": "a"}, recorder=_Recorder(sequence=None),
                       saver=_interrupted_saver())
        await checkpointing.drain_checkpoints()

    asyncio.run(scenario())
    assert calls == []


def test_background_save_failure_never_raises(monkeypatch):
    class _Broken:
        def save(self, *_: object) -> bool:
            raise RuntimeError("ddb down")

    _use(monkeypatch, _Broken())

    async def scenario():
        task = checkpointing.schedule_checkpoint_save(user_id=USER, session_id=SID, saver=_interrupted_saver(),
                                                      version=1)
        await checkpointing.drain_checkpoints()
        return task

    assert asyncio.run(scenario()).exception() is None


def test_cold_resume_restores_graph_and_re_registers_pending_surface(table, monkeypatch):  # noqa: F811
    repo = CheckpointRepository(table=table)
    assert repo.save_from_saver(USER, SID, _interrupted_saver(), version=3)
    fresh, pending = InMemorySaver(), _Pending()
    graph = _graph(fresh)
    restored = asyncio.run(checkpointing.restore_for_resume(user_id=USER, session_id=SID, saver=fresh,
                                                            graph=graph, pending=pending, repo=repo))
    assert restored and pending.registered[SID] == {"question": "pick one"}
