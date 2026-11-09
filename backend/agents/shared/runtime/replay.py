"""Demo replay mode for the local MVP runtimes (B94).

``CEI_DEMO_REPLAY`` selects the mode:

- ``off`` (default): no recording or playback.
- ``record``: every envelope a runtime returns, and every distinct job
  snapshot, is appended to ``<dir>/<agent>-<scenario>.json`` with a wall-clock
  time offset.
- ``play``: a matching scenario (by normalized prompt + agent) replays its
  recorded envelopes in order, and serves job snapshots by elapsed time times
  ``CEI_DEMO_REPLAY_SPEED`` (default 4). No match falls through to a live run.

Recordings never hold secrets — they are exactly the JSON envelopes and job
snapshots the runtime already returns to callers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")


class ReplayMode(StrEnum):
    OFF = "off"
    RECORD = "record"
    PLAY = "play"


def _mode() -> ReplayMode:
    raw = os.environ.get("CEI_DEMO_REPLAY", "off").strip().lower()
    try:
        return ReplayMode(raw)
    except ValueError:
        return ReplayMode.OFF


def _speed() -> float:
    raw = os.environ.get("CEI_DEMO_REPLAY_SPEED", "4")
    try:
        value = float(raw)
    except ValueError:
        return 4.0
    return value if value > 0 else 4.0


def _recordings_dir() -> Path:
    raw = os.environ.get("CEI_DEMO_RECORDINGS_DIR")
    if raw:
        return Path(raw)
    # backend/agents/demo_recordings — this file lives at backend/agents/shared/runtime/replay.py
    return Path(__file__).resolve().parents[2] / "demo_recordings"


def normalize_prompt(prompt: str) -> str:
    return _WHITESPACE_RE.sub(" ", prompt.strip().lower())


def scenario_key(agent: str, prompt: str) -> str:
    """Stable scenario id from the agent name and the normalized prompt."""
    digest = hashlib.sha256(f"{agent}:{normalize_prompt(prompt)}".encode("utf-8")).hexdigest()
    return digest[:16]


_EMPTY_RECORDING: dict[str, Any] = {"envelopes": {"run": [], "resume": []}, "job_snapshots": []}


class ReplayHub:
    """One hub per agent name (``ReplayHub.for_agent("pipeline")``)."""

    _instances: dict[str, "ReplayHub"] = {}

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.mode = _mode()
        self.speed = _speed()
        self.dir = _recordings_dir()
        self._cursors: dict[str, dict[str, int]] = {}
        self._clock_start: dict[str, float] = {}
        self._job_scenarios: dict[str, str] = {}

    @classmethod
    def for_agent(cls, agent: str) -> "ReplayHub":
        hub = cls._instances.get(agent)
        if hub is None:
            hub = cls(agent)
            cls._instances[agent] = hub
        return hub

    @property
    def enabled(self) -> bool:
        return self.mode is not ReplayMode.OFF

    # ── storage ──────────────────────────────────────────────────────────

    def _path(self, key: str) -> Path:
        return self.dir / f"{self.agent}-{key}.json"

    def _load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning({"event": "replay_load_error", "error_type": type(error).__name__})
            return None

    def _save(self, key: str, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── record ───────────────────────────────────────────────────────────

    def record_run(self, prompt: str, envelope: Mapping[str, Any]) -> None:
        if self.mode is not ReplayMode.RECORD:
            return
        key = scenario_key(self.agent, prompt)
        # A new run starts a new recording, so re-recording a scenario replaces it.
        data = copy.deepcopy(_EMPTY_RECORDING)
        data["envelopes"]["run"] = [dict(envelope)]
        self._save(key, data)
        self._clock_start[key] = time.monotonic()

    def record_resume(self, prompt: str, envelope: Mapping[str, Any]) -> None:
        if self.mode is not ReplayMode.RECORD:
            return
        key = scenario_key(self.agent, prompt)
        data = self._load(key) or copy.deepcopy(_EMPTY_RECORDING)
        data["envelopes"].setdefault("resume", []).append(dict(envelope))
        self._save(key, data)
        self._clock_start.setdefault(key, time.monotonic())

    def record_job_status(self, prompt: str, snapshot: Mapping[str, Any]) -> None:
        if self.mode is not ReplayMode.RECORD:
            return
        key = scenario_key(self.agent, prompt)
        data = self._load(key) or copy.deepcopy(_EMPTY_RECORDING)
        start = self._clock_start.setdefault(key, time.monotonic())
        offset = max(time.monotonic() - start, 0.0)
        snapshots = data.setdefault("job_snapshots", [])
        last_revision = snapshots[-1]["snapshot"].get("revision") if snapshots else None
        if snapshot.get("revision") == last_revision:
            return  # only distinct snapshots are recorded
        snapshots.append({"offset": offset, "snapshot": dict(snapshot)})
        self._save(key, data)

    # ── play ─────────────────────────────────────────────────────────────

    def _rewrite_envelope(
        self, envelope: dict[str, Any], *, job_id: str | None, action: Any | None
    ) -> dict[str, Any]:
        if job_id and "job_id" in envelope:
            envelope["job_id"] = job_id
        if action is not None and isinstance(envelope.get("action_response"), dict):
            recorded = envelope["action_response"]
            rewritten = action.model_dump(mode="json")
            rewritten["status"] = recorded.get("status", "completed")
            rewritten["message"] = recorded.get("message")
            envelope["action_response"] = rewritten
        return envelope

    def on_run(self, prompt: str, *, session_id: str) -> dict[str, Any] | None:
        if self.mode is not ReplayMode.PLAY:
            return None
        key = scenario_key(self.agent, prompt)
        data = self._load(key)
        if not data:
            return None
        run_list = data.get("envelopes", {}).get("run", [])
        if not run_list:
            return None
        # Every run starts the scenario again, so a demo can be played many times.
        self._cursors[key] = {"run": 1, "resume": 0}
        envelope = copy.deepcopy(run_list[0])
        self._clock_start[key] = time.monotonic()
        return self._rewrite_envelope(envelope, job_id=None, action=None)

    def on_resume(
        self, prompt: str, *, session_id: str, action: Any | None, job_id: str | None
    ) -> dict[str, Any] | None:
        if self.mode is not ReplayMode.PLAY:
            return None
        key = scenario_key(self.agent, prompt)
        data = self._load(key)
        if not data:
            return None
        resume_list = data.get("envelopes", {}).get("resume", [])
        cursor = self._cursors.setdefault(key, {"run": 0, "resume": 0})
        if cursor["resume"] >= len(resume_list):
            return None
        envelope = copy.deepcopy(resume_list[cursor["resume"]])
        cursor["resume"] += 1
        if "job_id" in envelope and not job_id:
            job_id = f"job-replay-{uuid.uuid4().hex[:12]}"
        rewritten = self._rewrite_envelope(envelope, job_id=job_id, action=action)
        if job_id and "job_id" in rewritten:
            self._job_scenarios[job_id] = key
            self._clock_start[key] = time.monotonic()
        return rewritten

    def on_job_status(self, job_id: str, *, session_id: str) -> dict[str, Any] | None:
        if self.mode is not ReplayMode.PLAY:
            return None
        key = self._job_scenarios.get(job_id)
        if key is None:
            return None
        data = self._load(key)
        if not data:
            return None
        snapshots = data.get("job_snapshots", [])
        if not snapshots:
            return None
        start = self._clock_start.get(key, time.monotonic())
        elapsed = max(time.monotonic() - start, 0.0) * self.speed
        chosen = snapshots[0]
        for item in snapshots:
            if item["offset"] <= elapsed:
                chosen = item
            else:
                break
        snapshot = copy.deepcopy(chosen["snapshot"])
        snapshot["job_id"] = job_id
        snapshot["session_id"] = session_id
        return snapshot
