"""Minimal interrupt graph for testing checkpoint recovery across processes."""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from shared.memory.agentcore import build_invocation_config
from shared.runtime.ops import PendingInterrupts, check_resume, parse_action, resume_value
from shared.tools.hitl.inline_functions import InlineFunctionRegistry


class State(TypedDict, total=False):
    question: str
    options: list[str]
    answer: Any
    completed: bool


def _ask(state: State) -> dict:
    surface = InlineFunctionRegistry()._ask_user_question(state["question"], state["options"])
    return {"answer": interrupt(surface)}


def _finish(state: State) -> dict:
    return {"completed": True}


def create_graph(checkpointer):
    builder = StateGraph(State)
    builder.add_node("ask", _ask)
    builder.add_node("finish", _finish)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


def start(graph, actor_id: str, thread_id: str, question: str, options: list[str]) -> dict:
    config = build_invocation_config(thread_id, actor_id)
    result = graph.invoke({"question": question, "options": options}, config)
    return {"status": "awaiting_input", "ui": result["__interrupt__"][0].value}


def resume(graph, actor_id: str, thread_id: str, raw_action: dict) -> dict:
    """Recover the pending surface from the checkpoint, not process memory."""
    config = build_invocation_config(thread_id, actor_id)
    snapshot = graph.get_state(config)
    if not snapshot.interrupts:
        return {"status": "stale"}
    pending = PendingInterrupts()
    pending.register(thread_id, snapshot.interrupts[0].value)
    action = parse_action({"action": raw_action})
    error = check_resume(pending, thread_id, action)
    if error is not None:
        return error
    if action.value not in snapshot.values["options"]:
        return {"status": "rejected", "message": "Answer is not an offered option."}
    result = graph.invoke(Command(resume=resume_value(action)), config)
    return {"status": "completed", "answer": result["answer"], "completed": result["completed"]}
