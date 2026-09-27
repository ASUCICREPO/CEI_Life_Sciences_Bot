from __future__ import annotations

from pydantic_settings import BaseSettings


class AgentConfig(BaseSettings):
    model_config = {"env_prefix": "CEI_"}

    aws_region: str = "us-east-1"
    aws_profile: str = "cei-project"
    direct_mode: bool = True  # ADR: default True | CLI dev phase requires no Lambda infra (D9)
    chat_history_table: str = "ChatHistoryStore"
    chat_metadata_table: str = "ChatMetadataStore"
    job_id_table: str = "JobIdStore"
    user_metadata_table: str = "UserMetadataTable"
    attachment_metadata_table: str = "AttachmentMetadataStore"
    attachments_bucket: str = "attachments"
    api_key_secret_prefix: str = "cei/user-keys"
    default_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_read_timeout: int = 900  # ADR: 900s | long drafts exceed botocore 60s default; matches 15-min stream limit
    bedrock_max_attempts: int = 3
    code_interpreter_id: str = "aws.codeinterpreter.v1"
    packages_s3_bucket: str = ""
    packages_s3_key: str = "life-sciences-packages.tar.gz"


config = AgentConfig()
