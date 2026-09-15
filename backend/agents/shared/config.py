from __future__ import annotations

from pydantic_settings import BaseSettings


class AgentConfig(BaseSettings):
    model_config = {"env_prefix": "CEI_"}

    aws_region: str = "us-east-1"
    chat_history_table: str = "ChatHistoryStore"
    chat_metadata_table: str = "ChatMetadataStore"
    job_id_table: str = "JobIdStore"
    user_metadata_table: str = "UserMetadataTable"
    attachment_metadata_table: str = "AttachmentMetadataStore"
    attachments_bucket: str = "attachments"
    api_key_secret_prefix: str = "cei/user-keys"
    default_model_id: str = "anthropic.claude-sonnet-5-v1"


config = AgentConfig()
