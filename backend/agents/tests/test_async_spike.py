import pytest
from langgraph.checkpoint.memory import InMemorySaver

from spikes.async_spike.graph import create_graph


@pytest.mark.asyncio
async def test_cancel_between_checkpointed_steps():
    checks = 0

    async def cancelled():
        nonlocal checks
        checks += 1
        return checks == 3

    saver = InMemorySaver()
    graph = create_graph(saver, cancelled)
    config = {"configurable": {"thread_id": "spike", "actor_id": "user"}}
    result = await graph.ainvoke(
        {"completed_steps": 0, "total_steps": 5, "delay_seconds": 0, "cancelled": False}, config
    )
    assert result["completed_steps"] == 2
    assert result["cancelled"] is True
    assert (await graph.aget_state(config)).values["completed_steps"] == 2


@pytest.mark.asyncio
async def test_resume_after_interrupted_iteration():
    checks = 0

    async def fail_on_third_check():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("simulated process failure")
        return False

    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "spike", "actor_id": "user"}}
    with pytest.raises(RuntimeError):
        await create_graph(saver, fail_on_third_check).ainvoke(
            {"completed_steps": 0, "total_steps": 4, "delay_seconds": 0, "cancelled": False}, config
        )

    async def never_cancel():
        return False

    fresh_graph = create_graph(saver, never_cancel)
    assert (await fresh_graph.aget_state(config)).values["completed_steps"] == 2
    result = await fresh_graph.ainvoke(None, config)
    assert result["completed_steps"] == 4
