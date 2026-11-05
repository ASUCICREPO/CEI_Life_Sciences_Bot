from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class AgentConfig(BaseSettings):
    model_config = {"env_prefix": "CEI_"}

    aws_region: str = "us-east-1"
    # Named local profile (e.g. CEI_AWS_PROFILE=cei-project for dev). None => default credential chain, which is
    # what deployed runtimes must use (execution role). Never default this to a developer profile: inside AgentCore
    # the profile does not exist and every model call fails with ProfileNotFound (surfaced as HTTP 424).
    aws_profile: str | None = None
    # ADR: identity has no bypass flag | the old CEI_DIRECT_MODE also let the payload `user_id` stand in for the
    # Cognito `sub`; it is deleted (plan 3b4d4f64 decision 9). Data-source transport is a separate, identity-free
    # concern: True calls the public APIs in-process, False routes through the data-source Lambdas.
    data_sources_direct_invoke: bool = True
    # Cognito access-token verification (shared/runtime/identity.py). Empty pool id => every request is rejected.
    cognito_user_pool_id: str = ""
    cognito_region: str = ""  # defaults to aws_region
    cognito_app_client_ids: list[str] = []
    cognito_jwks_ttl_seconds: int = 3600
    # Short-term-memory checkpoints in ChatHistoryStore (shared/persistence/checkpoints.py)
    checkpoint_chunk_bytes: int = 300_000
    checkpoint_max_chunks: int = 32
    # Per-turn reasoning/tool traces in ChatHistoryStore (shared/persistence/traces.py, F6 B9/B78)
    trace_gzip_threshold_bytes: int = 400_000
    trace_chunk_bytes: int = 350_000
    chat_history_table: str = "ChatHistoryStore"
    chat_metadata_table: str = "ChatMetadataStore"
    job_id_table: str = "JobIdStore"
    user_metadata_table: str = "UserMetadataTable"
    attachment_metadata_table: str = "AttachmentMetadataStore"
    attachments_bucket: str = "attachments"
    api_key_secret_prefix: str = "cei/user-keys"
    default_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    # Chat-title model: small + cheap; must never match the D3 blocklist (enforced in shared/runtime/titles.py)
    title_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    title_timeout_seconds: float = 8.0
    history_rehydrate_turns: int = 20
    bedrock_read_timeout: int = 900  # ADR: 900s | long drafts exceed botocore 60s default; matches 15-min stream limit
    bedrock_max_attempts: int = 3
    code_interpreter_id: str = "aws.codeinterpreter.v1"
    packages_s3_bucket: str = ""
    packages_s3_key: str = "life-sciences-packages.tar.gz"
    # Browser CORS allowlist for the runtime container (shared/runtime/app_factory.py). Comma-separated explicit
    # origins (CEI_ALLOWED_ORIGINS), never "*". Empty => no CORS middleware (local, Vite-proxied runs).
    allowed_origins: str = ""

    @field_validator("aws_profile", mode="before")
    @classmethod
    def _blank_profile_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def allowed_origins_list(self) -> list[str]:
        return parse_origins(self.allowed_origins)


def parse_origins(raw: str | None) -> list[str]:
    """Split a comma-separated origin list into trimmed, non-empty, trailing-slash-free entries."""
    if not raw:
        return []
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip() and item.strip() != "*"]


config = AgentConfig()
