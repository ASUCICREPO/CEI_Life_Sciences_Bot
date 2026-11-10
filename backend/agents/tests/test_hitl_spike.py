from langgraph.checkpoint.memory import InMemorySaver

from spikes.hitl_spike.graph import create_graph, resume, start


def test_hitl_resume_in_new_graph_rejects_stale_and_duplicate_actions():
    saver = InMemorySaver()
    first = create_graph(saver)
    response = start(first, "user", "session", "Choose", ["A", "B"])
    surface = response["ui"]
    action = {
        "request_id": "r1", "session_id": "session", "surface_id": surface["surface_id"],
        "revision": surface["revision"], "interaction_id": "ask-user-question",
        "component_id": "answer", "action_id": "submit-answer", "value": "A",
    }
    second = create_graph(saver)
    assert resume(second, "user", "session", {**action, "revision": action["revision"] + 1})["status"] == "stale"
    assert resume(second, "user", "session", {**action, "value": "C"})["status"] == "rejected"
    result = resume(second, "user", "session", action)
    assert result["completed"] and result["answer"]["value"] == "A"
    assert resume(second, "user", "session", action)["status"] == "stale"
