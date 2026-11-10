from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drafting.export_service import generate_reviewed_export

SESSION = "6f1c2d3e-0000-4000-8000-000000000001"


def invoke(repository, **overrides):
    args = {"repository": repository, "user_id": "alice", "session_id": SESSION, "turn_id": "turn-1",
            "reviewed": True, "format": "md", "title": "Report", "sections": [{"heading": "Results", "content": "Text"}]}
    return generate_reviewed_export(**(args | overrides))


def test_review_required_before_any_generation():
    repo = Mock()
    factory = Mock()
    with pytest.raises(ValueError, match="review"):
        invoke(repo, reviewed=False, export_tool_factory=factory)
    factory.assert_not_called()
    repo.save.assert_not_called()


def test_renders_and_persists_reviewed_markdown(tmp_path):
    repo = Mock()
    repo.save.return_value = {"export_id": "exp-1"}
    captured = {}
    def factory(**kwargs):
        def generate(**values):
            output = values["output_dir"] / "report.md"
            captured["output"] = output
            output.write_bytes(b"report")
            return [SimpleNamespace(format="md", filename="report.md", local_path=str(output))]
        return SimpleNamespace(generate=generate)
    assert invoke(repo, export_tool_factory=factory) == {"export_id": "exp-1"}
    assert repo.save.call_args.kwargs["data"] == b"report"
    assert repo.save.call_args.kwargs["reviewed"] is True
    assert not captured["output"].exists()


def test_failure_never_persists_unrendered_content():
    repo = Mock()
    factory = Mock(return_value=SimpleNamespace(generate=lambda **kwargs: []))
    with pytest.raises(RuntimeError, match="generation failed"):
        invoke(repo, export_tool_factory=factory)
    repo.save.assert_not_called()


def test_rejects_missing_interpreter_and_unbundled_latex():
    repo = Mock()
    with pytest.raises(ValueError, match="Code Interpreter"):
        invoke(repo, format="pdf")
    with pytest.raises(ValueError, match="asset bundling"):
        invoke(repo, format="latex", charts={"chart": b"png"})
    repo.save.assert_not_called()


def test_rejects_artifact_outside_temporary_directory(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    factory = Mock(return_value=SimpleNamespace(generate=lambda **kwargs: [
        SimpleNamespace(format="md", filename="report.md", local_path=str(outside))
    ]))
    repo = Mock()
    with pytest.raises(RuntimeError, match="invalid file"):
        invoke(repo, export_tool_factory=factory)
    repo.save.assert_not_called()


@pytest.mark.parametrize("format", ["md", "txt", "csv", "latex"])
def test_real_text_generators_produce_persistable_bytes(format):
    repo = Mock()
    repo.save.return_value = {"export_id": "exp-1"}
    invoke(repo, format=format)
    saved = repo.save.call_args.kwargs
    assert saved["format"] == format
    assert isinstance(saved["data"], bytes) and saved["data"]
