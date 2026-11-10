"""Small checkpoint-per-step graph; cancellation is checked before each step."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    completed_steps: int
    total_steps: int
    delay_seconds: float
    cancelled: bool


def create_graph(checkpointer, is_cancelled: Callable[[], Awaitable[bool]]):
    async def step(state: State) -> dict:
        if await is_cancelled():
            return {"cancelled": True}
        await asyncio.sleep(state["delay_seconds"])
        return {"completed_steps": state["completed_steps"] + 1}

    def next_step(state: State) -> str:
        return END if state["cancelled"] or state["completed_steps"] >= state["total_steps"] else "step"

    builder = StateGraph(State)
    builder.add_node("step", step)
    builder.add_edge(START, "step")
    builder.add_conditional_edges("step", next_step)
    return builder.compile(checkpointer=checkpointer)
