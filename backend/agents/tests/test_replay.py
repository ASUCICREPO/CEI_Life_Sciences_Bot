from __future__ import annotations

import time

from shared.runtime.replay import ReplayHub, scenario_key


def _hub(monkeypatch, tmp_path, mode: str, speed: str = "100") -> ReplayHub:
    monkeypatch.setenv("CEI_DEMO_REPLAY", mode)
    monkeypatch.setenv("CEI_DEMO_REPLAY_SPEED", speed)
    monkeypatch.setenv("CEI_DEMO_RECORDINGS_DIR", str(tmp_path))
    ReplayHub._instances.clear()
    return ReplayHub.for_agent("pipeline")


def test_scenario_key_normalizes_prompt() -> None:
    assert scenario_key("pipeline", "  Do THE thing  ") == scenario_key("pipeline", "do the thing")
    assert scenario_key("pipeline", "a") != scenario_key("analysis", "a")


def test_record_then_play_round_trip(monkeypatch, tmp_path) -> None:
    recorder = _hub(monkeypatch, tmp_path, "record")
    prompt = "Research TP53 and draft a pitch"

    run_envelope = {"result": "plan", "text": "plan", "ui": {"surface_id": "s1"}, "status": "awaiting_input"}
    recorder.record_run(prompt, run_envelope)

    resume_envelope = {
        "job_id": "job-recorded-1",
        "status": "running",
        "action_response": {
            "request_id": "r1",
            "session_id": "recorded-session",
            "surface_id": "hitl-plan-approval",
            "revision": 1,
            "interaction_id": "plan-approval",
            "component_id": "plan-review",
            "action_id": "approve-plan",
            "value": None,
            "status": "accepted",
            "message": None,
        },
        "text": "running",
        "result": "running",
    }
    recorder.record_resume(prompt, resume_envelope)

    recorder.record_job_status(prompt, {"job_id": "job-recorded-1", "session_id": "recorded-session", "revision": 1, "status": "running"})
    time.sleep(0.01)
    recorder.record_job_status(prompt, {"job_id": "job-recorded-1", "session_id": "recorded-session", "revision": 2, "status": "completed"})

    key = scenario_key("pipeline", prompt)
    recorded_path = tmp_path / f"pipeline-{key}.json"
    assert recorded_path.exists()

    player = _hub(monkeypatch, tmp_path, "play", speed="1000")
    played_run = player.on_run(prompt, session_id="live-session")
    assert played_run == run_envelope

    class _FakeAction:
        def model_dump(self, mode: str = "json") -> dict:
            return {
                "request_id": "live-r1",
                "session_id": "live-session",
                "surface_id": "hitl-plan-approval",
                "revision": 1,
                "interaction_id": "plan-approval",
                "component_id": "plan-review",
                "action_id": "approve-plan",
                "value": None,
            }

    played_resume = player.on_resume(prompt, session_id="live-session", action=_FakeAction(), job_id="job-live-1")
    assert played_resume is not None
    assert played_resume["job_id"] == "job-live-1"
    assert played_resume["action_response"]["session_id"] == "live-session"
    assert played_resume["action_response"]["status"] == "accepted"

    time.sleep(0.02)
    status = player.on_job_status("job-live-1", session_id="live-session")
    assert status is not None
    assert status["job_id"] == "job-live-1"
    assert status["session_id"] == "live-session"
    assert status["status"] == "completed"


def test_play_falls_through_when_no_scenario_matches(monkeypatch, tmp_path) -> None:
    player = _hub(monkeypatch, tmp_path, "play")
    assert player.on_run("never recorded", session_id="s1") is None
    assert player.on_resume("never recorded", session_id="s1", action=None, job_id=None) is None
    assert player.on_job_status("job-x", session_id="s1") is None


def test_off_mode_never_records_or_plays(monkeypatch, tmp_path) -> None:
    hub = _hub(monkeypatch, tmp_path, "off")
    hub.record_run("x", {"status": "completed"})
    assert list(tmp_path.glob("*.json")) == []
    assert hub.on_run("x", session_id="s1") is None


def test_play_without_live_job_id_maps_new_job_and_repeats(monkeypatch, tmp_path) -> None:
    recorder = _hub(monkeypatch, tmp_path, "record")
    prompt = "Run the demo pipeline"
    recorder.record_run(prompt, {"text": "plan", "status": "awaiting_input"})
    recorder.record_resume(prompt, {"job_id": "job-recorded", "status": "running"})
    recorder.record_job_status(prompt, {"job_id": "job-recorded", "revision": 2, "status": "running"})
    recorder.record_job_status(prompt, {"job_id": "job-recorded", "revision": 5, "status": "completed"})

    player = _hub(monkeypatch, tmp_path, "play")
    for _ in range(2):  # the same scenario must play again for a second demo
        assert player.on_run(prompt, session_id="live")["text"] == "plan"
        resumed = player.on_resume(prompt, session_id="live", action=None, job_id=None)
        assert resumed is not None
        assert resumed["job_id"].startswith("job-replay-")
        status = player.on_job_status(resumed["job_id"], session_id="live")
        assert status is not None and status["job_id"] == resumed["job_id"]


def test_record_run_replaces_old_recording(monkeypatch, tmp_path) -> None:
    recorder = _hub(monkeypatch, tmp_path, "record")
    recorder.record_run("p", {"text": "first"})
    recorder.record_resume("p", {"text": "first-resume"})
    recorder.record_run("p", {"text": "second"})

    player = _hub(monkeypatch, tmp_path, "play")
    assert player.on_run("p", session_id="s")["text"] == "second"
    assert player.on_resume("p", session_id="s", action=None, job_id=None) is None
