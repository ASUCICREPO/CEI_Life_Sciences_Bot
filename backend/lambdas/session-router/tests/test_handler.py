import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location("session_router_handler", Path(__file__).parents[1] / "handler.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)
# handler.py's own sys.path insertion (for its sibling classifier.py import) has already run by this point.
from classifier import AGENTS, Classification, ClassificationError, GuardrailIntervened  # noqa: E402
from fixture_prompts import FIXTURE_PROMPTS  # noqa: E402
SESSION = "3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab"
RUN = "d7d02773-3f65-4e65-a2ac-0123456789ab"
SCOPES = ("cei-tools/src.pubmed", "cei-tools/common")


@pytest.fixture
def deps(monkeypatch):
    monkeypatch.setenv("TOOL_SCOPES", ",".join(SCOPES))
    monkeypatch.setenv("RESEARCH_RUNTIME_URL", "https://example.invalid/research")
    monkeypatch.setenv("DRAFTING_RUNTIME_URL", "https://example.invalid/drafting")
    monkeypatch.setenv("ANALYSIS_RUNTIME_URL", "https://example.invalid/analysis")
    monkeypatch.setenv("PIPELINE_RUNTIME_URL", "https://example.invalid/pipeline")
    monkeypatch.setenv("CEI_DEFAULT_MODEL_ID", "default-model")
    monkeypatch.setenv("CEI_ROUTER_MODEL_ID", "router-model")
    monkeypatch.setenv("CEI_GUARDRAIL_ID", "guardrail")
    monkeypatch.setenv("CEI_GUARDRAIL_VERSION", "1")
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
    # route_choice tests fetch the same Mock back via router.runs_table() (it is not part of this tuple,
    # so every pre-existing 4-way `deps` destructuring below keeps working unchanged).
    runs = Mock()
    monkeypatch.setattr(router, "runs_table", lambda: runs)
    # classify() is always replaced per-test below; this only avoids a real boto3 client construction
    # (handler.py evaluates bedrock_client() to build classify()'s `client=` kwarg either way).
    monkeypatch.setattr(router, "bedrock_client", lambda: Mock())
    return verifier, metadata, leases, issue


def event(**overrides):
    request = {"session_id": SESSION, "run_id": RUN, "message": "question", "task_type": "live-research",
               "mode": "science", "user_id": "mallory"}
    request.update(overrides)
    return {"op": "start", "bearer_token": "jwt", "request": request}


def free_text_event(**overrides):
    request = {"session_id": SESSION, "run_id": RUN, "message": "a free-text question", "mode": "science"}
    request.update(overrides)
    return {"op": "start", "bearer_token": "jwt", "request": request}


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


def test_blocklisted_model_rejected_before_lease_or_token(deps):
    # B18: the global blocklist (D3 defaults + CEI_BLOCKED_MODELS), not "any model other than the default".
    _, metadata, leases, issue = deps
    metadata.get_item.return_value = {"Item": {"session_id": SESSION, "model_id": "anthropic.claude-fable-5-v1"}}
    result = router.handler(event(), None)
    assert result["status"] == 400 and result["code"] == "model_unavailable"
    assert "anthropic.claude-fable-5-v1" not in str(result)
    leases.acquire.assert_not_called()
    issue.assert_not_called()


def test_non_default_non_blocklisted_model_is_accepted(deps):
    _, metadata, leases, issue = deps
    metadata.get_item.return_value = {"Item": {"session_id": SESSION, "model_id": "other-private-model"}}
    result = router.handler(event(), None)
    assert result["status"] == 200
    leases.acquire.assert_called_once()
    issue.assert_called_once()


def test_env_blocklist_pattern_is_merged_with_d3(deps, monkeypatch):
    _, metadata, leases, issue = deps
    monkeypatch.setenv("CEI_BLOCKED_MODELS", "private.*")
    metadata.get_item.return_value = {"Item": {"session_id": SESSION, "model_id": "private.custom-model"}}
    result = router.handler(event(), None)
    assert result["status"] == 400 and result["code"] == "model_unavailable"
    leases.acquire.assert_not_called()
    issue.assert_not_called()


def test_missing_model_configuration_fails_closed(deps, monkeypatch):
    _, _, leases, issue = deps
    monkeypatch.delenv("CEI_DEFAULT_MODEL_ID")
    assert router.handler(event(), None)["status"] == 503
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


# F9: free-text routing (task_type absent) -- the classifier path, B80's low-confidence ChoicePicker,
# and the route_agent hint only UiActions is trusted to send.

def test_free_text_high_confidence_routes_to_classified_agent(deps, monkeypatch):
    _, metadata, leases, issue = deps
    classify = Mock(return_value=Classification(agent="drafting", confidence=0.95))
    monkeypatch.setattr(router, "classify", classify)
    reply = router.handler(free_text_event(message="write a grant application"), None)
    assert reply["status"] == 200
    assert reply["agent"] == "drafting"
    assert reply["runtime_url"] == "https://example.invalid/drafting"
    classify.assert_called_once()
    assert classify.call_args.args[0] == "write a grant application"
    assert classify.call_args.kwargs["model_id"] == "router-model"
    assert classify.call_args.kwargs["guardrail_id"] == "guardrail"
    leases.acquire.assert_called_once()
    issue.assert_called_once()


def test_free_text_low_confidence_stores_pending_choice_without_a_lease_or_token(deps, monkeypatch):
    _, _, leases, issue = deps
    monkeypatch.setattr(router, "classify", Mock(return_value=Classification(agent="research", confidence=0.4)))
    reply = router.handler(free_text_event(), None)
    assert reply["status"] == 200
    assert reply["choice_required"] is True
    assert reply["surface_id"] == f"router-choice-{RUN}"
    assert "agent" not in reply and "gateway_token" not in reply
    leases.acquire.assert_not_called()
    issue.assert_not_called()
    stored = router.runs_table().update_item.call_args.kwargs
    assert stored["Key"] == {"user_id": "alice", "record": f"route_choice#{SESSION}"}
    assert stored["ExpressionAttributeValues"][":message"] == "a free-text question"
    assert stored["ExpressionAttributeValues"][":surface_id"] == f"router-choice-{RUN}"


def test_classifier_failure_fails_closed_without_routing(deps, monkeypatch):
    _, _, leases, issue = deps
    monkeypatch.setattr(router, "classify", Mock(side_effect=ClassificationError("model unavailable")))
    reply = router.handler(free_text_event(), None)
    assert reply["status"] == 503
    leases.acquire.assert_not_called()
    issue.assert_not_called()


def test_guardrail_intervention_is_a_safe_invalid_request(deps, monkeypatch):
    _, _, leases, issue = deps
    monkeypatch.setattr(router, "classify", Mock(side_effect=GuardrailIntervened("blocked")))
    reply = router.handler(free_text_event(), None)
    assert reply["status"] == 400 and reply["code"] == "invalid_request"
    assert "guardrail" not in reply["message"].lower()
    leases.acquire.assert_not_called()
    issue.assert_not_called()


def test_blocklisted_model_rejects_free_text_before_classifying(deps, monkeypatch):
    _, metadata, leases, issue = deps
    metadata.get_item.return_value = {"Item": {"session_id": SESSION, "model_id": "anthropic.claude-fable-5-v1"}}
    classify = Mock()
    monkeypatch.setattr(router, "classify", classify)
    reply = router.handler(free_text_event(), None)
    assert reply["status"] == 400 and reply["code"] == "model_unavailable"
    classify.assert_not_called()
    leases.acquire.assert_not_called()
    issue.assert_not_called()


def test_route_agent_hint_skips_reclassification(deps, monkeypatch):
    _, _, leases, issue = deps
    classify = Mock()
    monkeypatch.setattr(router, "classify", classify)
    reply = router.handler(free_text_event(route_agent="analysis"), None)
    assert reply["status"] == 200
    assert reply["agent"] == "analysis"
    assert reply["runtime_url"] == "https://example.invalid/analysis"
    classify.assert_not_called()
    leases.acquire.assert_called_once()
    issue.assert_called_once()


def test_route_agent_hint_rejects_an_unknown_agent(deps):
    reply = router.handler(free_text_event(route_agent="not-a-real-agent"), None)
    assert reply["status"] == 400 and reply["code"] == "invalid_request"


def test_live_drafting_routes_deterministically_without_classifying(deps, monkeypatch):
    # live-drafting mirrors live-research (F5): a deterministic dashboard task, no classifier call.
    classify = Mock()
    monkeypatch.setattr(router, "classify", classify)
    _, metadata, leases, issue = deps
    reply = router.handler(event(task_type="live-drafting"), None)
    assert reply["status"] == 200 and reply["agent"] == "drafting"
    assert reply["runtime_url"] == "https://example.invalid/drafting"
    classify.assert_not_called()
    leases.acquire.assert_called_once_with("alice", SESSION, RUN, SCOPES)
    issue.assert_called_once_with("alice", SESSION, SCOPES)


def test_unknown_task_type_is_rejected_without_classifying(deps, monkeypatch):
    classify = Mock()
    monkeypatch.setattr(router, "classify", classify)
    reply = router.handler(free_text_event(task_type="dashboard-task-xyz"), None)
    assert reply["status"] == 400 and reply["code"] == "invalid_request"
    classify.assert_not_called()


@pytest.mark.parametrize("prompt, expected_agent", FIXTURE_PROMPTS)
def test_fixture_prompts_route_to_their_expected_agent(deps, monkeypatch, prompt, expected_agent):
    # F9 task 5: this tests the Router's handling of the classifier's label, not the model's real
    # accuracy -- the classifier call itself is mocked to return each fixture's expected label.
    monkeypatch.setattr(router, "classify", Mock(return_value=Classification(agent=expected_agent, confidence=0.9)))
    reply = router.handler(free_text_event(message=prompt), None)
    assert reply["status"] == 200
    assert reply["agent"] == expected_agent
