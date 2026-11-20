from __future__ import annotations

import pytest


def flatten_content(content) -> str:
    """Join a Converse content-block list (text + guard_content blocks) back into one string.

    Lets existing substring assertions on a model-call's user message keep working
    after shared.runtime.guarded_input.user_turn()/content_blocks() split it into blocks.
    """
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


class _NullRecorder:
    """Stands in for the DynamoDB-backed ConversationRecorder so entrypoint tests never reach AWS."""

    async def start_turn(self, **_: object) -> None:
        return None

    async def assert_owner(self, **_: object) -> None:
        return None

    async def session_metadata(self, **_: object) -> None:
        return None

    async def finish_turn(self, **_: object) -> None:
        return None

    async def record_human_action(self, **_: object) -> None:
        return None

    async def rehydrate(self, **_: object) -> list:
        return []

    async def drain(self) -> None:
        return None


TEST_TOKEN_PREFIX = "test-token:"
# B112: a second fake-token prefix that verifies fine but carries no age-verified claim, for tests of that
# gate (shared.runtime.identity.AgeNotVerifiedError) without touching every other test's plain auth_headers.
NO_AGE_VERIFIED_TOKEN_PREFIX = "test-token-noage:"


class FakeVerifier:
    """Injected in place of the Cognito JWKS verifier: accepts `test-token:<sub>` (age-verified) and
    `test-token-noage:<sub>` (verified, but missing the B112 `cei_age_verified` claim) bearer tokens."""

    def verify(self, token: str) -> dict:
        from shared.runtime.identity import UnauthenticatedError

        if token.startswith(NO_AGE_VERIFIED_TOKEN_PREFIX) and token[len(NO_AGE_VERIFIED_TOKEN_PREFIX):]:
            return {"sub": token[len(NO_AGE_VERIFIED_TOKEN_PREFIX):], "token_use": "access"}
        if not token.startswith(TEST_TOKEN_PREFIX) or not token[len(TEST_TOKEN_PREFIX):]:
            raise UnauthenticatedError("invalid test token")
        return {"sub": token[len(TEST_TOKEN_PREFIX):], "token_use": "access", "cei_age_verified": "true"}


def auth_headers(sub: str = "test-user") -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN_PREFIX}{sub}"}


def unverified_age_headers(sub: str = "test-user") -> dict[str, str]:
    """B112: a verified bearer token for `sub` that lacks the age-verified claim."""
    return {"Authorization": f"Bearer {NO_AGE_VERIFIED_TOKEN_PREFIX}{sub}"}


@pytest.fixture(autouse=True)
def _fake_token_verifier():
    from shared.runtime import identity

    identity.set_verifier(FakeVerifier())
    yield
    identity.set_verifier(None)


class _NullCheckpoints:
    """Stands in for the DynamoDB-backed CheckpointRepository in entrypoint tests."""

    def restore(self, *_: object) -> bool:
        return False

    def save(self, *_: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _no_real_chat_history(monkeypatch):
    import shared.runtime.checkpointing as checkpointing
    import shared.runtime.history as history

    monkeypatch.setattr(checkpointing, "get_checkpoint_repository", lambda: _NullCheckpoints())
    monkeypatch.setattr(history, "get_recorder", lambda: _NullRecorder())
    yield
