from __future__ import annotations

import pytest


class _NullRecorder:
    """Stands in for the DynamoDB-backed ConversationRecorder so entrypoint tests never reach AWS."""

    async def start_turn(self, **_: object) -> None:
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


class FakeVerifier:
    """Injected in place of the Cognito JWKS verifier: accepts only `test-token:<sub>` bearer tokens."""

    def verify(self, token: str) -> dict:
        from shared.runtime.identity import UnauthenticatedError

        if not token.startswith(TEST_TOKEN_PREFIX) or not token[len(TEST_TOKEN_PREFIX):]:
            raise UnauthenticatedError("invalid test token")
        return {"sub": token[len(TEST_TOKEN_PREFIX):], "token_use": "access"}


def auth_headers(sub: str = "test-user") -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN_PREFIX}{sub}"}


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
