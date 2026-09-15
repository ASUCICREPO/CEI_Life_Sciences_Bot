from pydantic_settings import BaseSettings
import os

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
ATTACHMENTS_BUCKET = os.getenv("AGENTCORE_ATTACHMENTS_BUCKET", "attachments")
SESSION_CHAT_HISTORY_STORE = os.getenv("AGENTCORE_SESSION_CHAT_HISTORY_STORE", "ChatHistoryStore")
SESSION_METADATA_STORE = os.getenv("AGENTCORE_SESSION_METADATA_STORE", "SessionMetadataStore")

USER_METADATA_TABLE = os.getenv("USER_METADATA_TABLE", "UserMetadataTable")
JOB_ID_TABLE = os.getenv("AGENTCORE_JOB_ID_TABLE", "JobIdStore")
ATTACHMENT_METADATA_TABLE = os.getenv("AGENTCORE_ATTACHMENT_METADATA_TABLE", "AttachmentMetadataStore")
API_KEY_SECRET_PREFIX = os.getenv("API_KEY_SECRET_PREFIX", "cei/user-keys")

class AgentConfig(BaseSettings):
    model_config = {"env_prefix": "CEI_"}

    aws_region: str = AWS_REGION
    chat_history_table: str = SESSION_CHAT_HISTORY_STORE
    user_metadata_table: str = USER_METADATA_TABLE
    job_id_table: str = JOB_ID_TABLE
    session_metadata_table: str = SESSION_METADATA_STORE
    attachment_metadata_table: str = ATTACHMENT_METADATA_TABLE
    attachments_bucket: str = ATTACHMENTS_BUCKET
    api_key_secret_prefix: str = API_KEY_SECRET_PREFIX
    default_model_id: str = "anthropic.claude-sonnet-5-v1"


config = AgentConfig()
