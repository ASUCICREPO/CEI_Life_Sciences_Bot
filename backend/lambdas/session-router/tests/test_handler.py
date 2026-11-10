import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location("session_router_handler", Path(__file__).parents[1] / "handler.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)
SESSION = "3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab"
RUN = "d7d02773-3f65-4e65-a2ac-0123456789ab"
SCOPES = ("cei-tools/src.pubmed", "cei-tools/common")


@pytest.fixture
def deps(monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", ",".join(SCOPES))
    monkeypatch.setenv("RESEARCH_RUNTIME_URL", "https://example.invalid")
    verifier = Mock()
    verifier.verify.return_value = {"sub": "alice"}
    monkeypatch.setattr(router, "verifier", lambda: verifier)
    metadata = Mock()
    metadata.get_item.return_value = {"Item": {"session_id": SESSION}}
    leases = Mock()
    leases.find.return_value = None
    monkeypatch.setattr(router, "tables", lambda: (metadata, leases))
    issue = Mock(return_value="gateway-token")
    monkeypatch.setattr(router, "issue_token", issue)
    return verifier, metadata, leases, issue


def event():
    return {"op": "start", "bearer_token": "jwt", "request": {"session_id": SESSION, "run_id": RUN,
        "message": "question", "task_type": "live-research", "mode": "science", "user_id": "mallory"}}


def test_verified_subject_and_server_scopes_are_used(deps):
    _, metadata, leases, issue = deps
    reply = router.handler(event(), None)
    assert reply["status"] == 200 and reply["user_id"] == "alice"
    metadata.get_item.assert_called_once_with(Key={"user_id": "alice", "session_id": SESSION}, ConsistentRead=True)
    leases.acquire.assert_called_once_with("alice", SESSION, RUN, SCOPES)
    issue.assert_called_once_with("alice", SESSION, SCOPES)


def test_foreign_session_never_gets_token(deps):
    _, metadata, leases, issue = deps
    metadata.get_item.return_value = {}
    assert router.handler(event(), None)["status"] == 404
    leases.acquire.assert_not_called()
    issue.assert_not_called()


def test_failed_token_issue_releases_lease(deps, caplog):
    _, _, leases, issue = deps
    issue.side_effect = ValueError("secret must not escape")
    reply = router.handler(event(), None)
    assert reply["status"] == 503 and "secret" not in str(reply)
    leases.release.assert_called_once_with(leases.acquire.return_value)
    assert '"stage": "issue_token"' in caplog.text
    assert '"error_type": "ValueError"' in caplog.text
    assert "secret must not escape" not in caplog.text


def test_finish_cannot_release_a_newer_run(deps):
    _, _, leases, _ = deps
    leases.find.return_value = Mock(run_id="another-run")
    reply = router.handler({"op": "finish", "bearer_token": "jwt", "session_id": SESSION, "run_id": RUN}, None)
    assert reply["status"] == 200
    leases.release.assert_not_called()


def test_aws_error_code_logged_without_service_message(deps, caplog):
    _, _, _, issue = deps
    issue.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "secret details"}}, "GetSecretValue")
    assert router.handler(event(), None)["status"] == 503
    assert '"aws_error_code": "AccessDeniedException"' in caplog.text
    assert "secret details" not in caplog.text


def test_issue_token_accepts_cognito_response_without_top_level_scope(monkeypatch):
    import io
    import json
    monkeypatch.setenv("ROUTER_SECRET_ARN", "secret")
    monkeypatch.setenv("ROUTER_CLIENT_ID", "client")
    monkeypatch.setenv("TOKEN_ENDPOINT", "https://example.invalid/token")
    monkeypatch.setattr(router.boto3, "client", lambda _: Mock(get_secret_value=Mock(return_value={"SecretString": "secret"})))
    monkeypatch.setattr(router.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps({"access_token": "jwt", "expires_in": 3600}).encode()))
    verifier = Mock()
    verifier.verify.return_value = {"scope": " ".join(SCOPES), "cei_user_sub": "alice", "cei_session_id": SESSION}
    monkeypatch.setattr(router, "gateway_verifier", lambda: verifier)
    assert router.issue_token("alice", SESSION, SCOPES) == "jwt"
    verifier.verify.assert_called_once_with("jwt")
