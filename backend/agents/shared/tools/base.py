from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from shared.config import config
from shared.schemas.citation import Citation, SourceType

logger = logging.getLogger(__name__)


class DataSourceResult(BaseModel):
    source_type: SourceType
    results: list[dict[str, Any]]
    citations: list[Citation]
    query_metadata: dict[str, Any] = {}


class DataSourceTool(ABC):
    function_name: str
    source_type: SourceType
    description: str
    InputSchema: type[BaseModel]

    def __init__(self, session: boto3.Session | None = None, direct_mode: bool = False) -> None:
        self._direct_mode = direct_mode
        if not direct_mode:
            self._session = session or boto3.Session(
                region_name=config.aws_region,
                profile_name=config.aws_profile,
            )
            self._lambda_client = self._session.client("lambda")

    def invoke(self, **params: Any) -> DataSourceResult:
        if self._direct_mode:
            return self._direct_invoke(**params)
        payload = self._build_payload(**params)
        response = self._lambda_client.invoke(
            FunctionName=self.function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        raw = json.loads(response["Payload"].read())
        return DataSourceResult(
            source_type=self.source_type,
            results=raw.get("results", []),
            citations=[Citation(**c) for c in raw.get("citations", [])],
            query_metadata=raw.get("metadata", {}),
        )

    @abstractmethod
    def _build_payload(self, **params: Any) -> dict[str, Any]: ...

    @abstractmethod
    def _direct_invoke(self, **params: Any) -> DataSourceResult:
        """Call the public API directly. Used for CLI/local testing."""
        ...

    def as_langchain_tool(self) -> StructuredTool:
        tool_instance = self

        def _run(**kwargs: Any) -> dict[str, Any]:
            return tool_instance.invoke(**kwargs).model_dump(mode="json")

        return StructuredTool.from_function(
            func=_run,
            name=self.function_name,
            description=self.description,
            args_schema=self.InputSchema,
        )
