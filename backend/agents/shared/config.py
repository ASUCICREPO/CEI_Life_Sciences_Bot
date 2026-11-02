from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings


class AgentConfig(BaseSettings):
    model_config = {"env_prefix": "CEI_"}

    # ADR: CEI_ENVIRONMENT, default "local" | Rationale: B19 — deployed compute (agent runtimes, Gateway
    # Lambdas) sets CEI_ENVIRONMENT=deployed, which makes direct_mode an error instead of a silent shortcut
    environment: Literal["local", "deployed"] = "local"
    aws_region: str = "us-east-1"
    aws_profile: str = "cei-project"
    direct_mode: bool = (
        True  # ADR: default True | CLI dev phase requires no Lambda infra (D9); local-only (B19)
    )
    chat_history_table: str = "ChatHistoryStore"
    chat_metadata_table: str = "ChatMetadataStore"
    job_id_table: str = "JobIdStore"
    user_metadata_table: str = "UserMetadataTable"
    attachment_metadata_table: str = "AttachmentMetadataStore"
    attachments_bucket: str = "attachments"
    api_key_secret_prefix: str = "cei/user-keys"
    default_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_read_timeout: int = (
        900  # ADR: 900s | long drafts exceed botocore 60s default; matches 15-min stream limit
    )
    bedrock_max_attempts: int = 3
    code_interpreter_id: str = "aws.codeinterpreter.v1"
    packages_s3_bucket: str = ""
    packages_s3_key: str = "life-sciences-packages.tar.gz"

    @model_validator(mode="after")
    def _direct_mode_is_local_only(self) -> AgentConfig:
        if self.environment == "deployed" and self.direct_mode:
            raise ValueError(
                "CEI_DIRECT_MODE is for local CLI runs only (B19); deployed code must set CEI_DIRECT_MODE=false "
                "and reach data sources through the AgentCore Gateway."
            )
        return self


config = AgentConfig()
