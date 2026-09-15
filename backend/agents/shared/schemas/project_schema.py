from pydantic import BaseModel
from enum import StrEnum
from typing import Optional

class AllowedSourceTypes(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MARKDOWN = "md"
    JSON = "json"
    JPEG = "jpeg"
    PNG = "png"

class ProjectSource(BaseModel):
    id: str
    source_name: str
    source_url: str
    source_owner_id: str
    source_type: AllowedSourceTypes
    created_at: str
    is_indexed: bool

    extracted_context: Optional[str]

class ChatProject(BaseModel):
    
    id: str
    project_name: str
    project_instructions: str
    project_owner_id: str
    created_at: str
    updated_at: str

    sources: list[ProjectSource] = []