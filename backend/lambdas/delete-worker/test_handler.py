import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest

spec = importlib.util.spec_from_file_location("deletion_worker", Path(__file__).with_name("handler.py"))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
JOB = "a" * 32


@pytest.fixture
def backend():
    b = Mock()
    b.get_job.return_value = {"job_id": JOB, "kind": "chat", "user_id": "owner", "session_id": "chat", "status": "deleting"}
    return b


def test_chat_cascade_uses_trusted_job_owner_and_never_user_wide_deletion(backend):
    worker.process(JOB, backend)
    backend.verify_deletion_fence.assert_called_once_with(backend.get_job.return_value)
    backend.quiesce.assert_called_once_with("owner", "chat")
    for name in ("delete_exports", "delete_history_and_replay", "delete_checkpoints", "delete_session_metadata"):
        getattr(backend, name).assert_called_once_with("owner", "chat")
    backend.delete_long_term_memory.assert_not_called()
    backend.delete_api_keys.assert_not_called()
    backend.complete_job.assert_called_once()


@pytest.mark.parametrize("failure", ["verify_deletion_fence", "quiesce", "delete_exports",
                                     "delete_history_and_replay", "delete_checkpoints", "delete_session_metadata"])
def test_failure_never_marks_job_complete(backend, failure):
    getattr(backend, failure).side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        worker.process(JOB, backend)
    backend.complete_job.assert_not_called()


def test_account_cascade_enumerates_sessions_before_user_cleanup(backend):
    backend.get_job.return_value = {"job_id": JOB, "kind": "account", "user_id": "owner", "status": "deleting"}
    backend.sessions.return_value = iter(["chat-1", "chat-2"])
    worker.process(JOB, backend)
    assert backend.delete_exports.call_args_list == [call("owner", "chat-1"), call("owner", "chat-2")]
    backend.delete_long_term_memory.assert_called_once_with("owner")
    backend.delete_cognito_user.assert_called_once_with("owner")
    backend.complete_job.assert_called_once()


def test_account_memory_failure_cannot_complete_or_delete_cognito(backend):
    backend.get_job.return_value = {"job_id": JOB, "kind": "account", "user_id": "owner", "status": "deleting"}
    backend.sessions.return_value = iter([])
    backend.delete_long_term_memory.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        worker.process(JOB, backend)
    backend.complete_job.assert_not_called()
    backend.delete_cognito_user.assert_not_called()


def test_unknown_or_unfenced_job_never_deletes(backend):
    backend.get_job.return_value = None
    with pytest.raises(ValueError):
        worker.process(JOB, backend)
    backend.quiesce.assert_not_called()


def test_sqs_reports_failed_record_and_refuses_supplied_user_id(backend):
    records = [{"messageId": "bad", "body": json.dumps({"job_id": JOB, "user_id": "other"})},
               {"messageId": "good", "body": json.dumps({"job_id": JOB})}]
    assert worker.process_sqs({"Records": records}, backend) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    backend.complete_job.assert_called_once()


def test_complete_job_is_idempotent(backend):
    backend.get_job.return_value["status"] = "complete"
    worker.process(JOB, backend)
    backend.quiesce.assert_not_called()
    backend.complete_job.assert_not_called()
