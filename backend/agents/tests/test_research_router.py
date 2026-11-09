import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from shared.runtime.identity import UnauthenticatedError
from shared.runtime.research_router import SessionNotOwned, TokenGrantError, route_research

SESSION = "3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab"
SCOPES = ("cei-tools/src.pubmed", "cei-tools/common")


def dependencies():
    verifier = Mock()
    verifier.verify.return_value = {"sub": "alice"}
    leases = Mock()
    issue = AsyncMock(return_value={"access_token": "gateway-secret", "scope": " ".join(SCOPES)})
    invoke = AsyncMock(return_value={"ok": True})
    return dict(bearer_token="user-token", request={"session_id": SESSION, "message": "question",
                "task_type": "live-research", "mode": "science", "user_id": "mallory"},
                verifier=verifier, session_exists=Mock(return_value=True), leases=leases, scopes=SCOPES,
                issue_token=issue, invoke_runtime=invoke)


def test_verified_subject_is_used_and_lease_is_released():
    deps = dependencies()
    assert asyncio.run(route_research(**deps)) == {"ok": True}
    deps["session_exists"].assert_called_once_with("alice", SESSION)
    deps["issue_token"].assert_awaited_once_with("alice", SESSION, SCOPES)
    deps["invoke_runtime"].assert_awaited_once_with("user-token", "gateway-secret", SESSION,
                                                    {"prompt": "question", "mode": "science"})
    deps["leases"].release.assert_called_once_with(deps["leases"].acquire.return_value)


def test_foreign_session_never_receives_tool_token():
    deps = dependencies()
    deps["session_exists"].return_value = False
    with pytest.raises(SessionNotOwned):
        asyncio.run(route_research(**deps))
    deps["issue_token"].assert_not_awaited()
    deps["leases"].acquire.assert_not_called()


def test_invalid_user_token_never_receives_tool_token():
    deps = dependencies()
    deps["verifier"].verify.side_effect = UnauthenticatedError("invalid")
    with pytest.raises(UnauthenticatedError):
        asyncio.run(route_research(**deps))
    deps["issue_token"].assert_not_awaited()


def test_wrong_scopes_do_not_reach_runtime_and_release_lease():
    deps = dependencies()
    deps["issue_token"].return_value = {"access_token": "secret", "scope": "cei-tools/admin"}
    with pytest.raises(TokenGrantError):
        asyncio.run(route_research(**deps))
    deps["invoke_runtime"].assert_not_awaited()
    deps["leases"].release.assert_called_once()
