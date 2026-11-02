from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

import boto3
from pydantic import BaseModel

from shared.config import config
from shared.schemas.citation import Citation, SourceType

if TYPE_CHECKING:
    from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


class DataSourceResult(BaseModel):
    source_type: SourceType
    results: list[dict[str, Any]]
    citations: list[Citation]
    query_metadata: dict[str, Any] = {}


class DataSourceTool(ABC):
    """One upstream data source.

    `fetch()` is the real upstream HTTP call path. It runs inside the F4 Gateway Lambda for this
    source, and in local CLI runs when `direct_mode` is on (B19: never in deployed agent code).
    """

    function_name: str
    source_type: SourceType
    description: str
    InputSchema: type[BaseModel]
    # B20: True when fetch() accepts an optional `api_key` (a user's own key for higher rate limits).
    supports_api_key: ClassVar[bool] = False

    def __init__(self, session: boto3.Session | None = None, direct_mode: bool = False) -> None:
        self._direct_mode = direct_mode
        self._session = session
        # ADR: lambda client created on first non-direct invoke | Rationale: the F4 source Lambdas build a
        # tool only to call fetch(); an eager boto3 Session would need the local `aws_profile` to exist
        self._lambda_client: Any = None

    def invoke(self, **params: Any) -> DataSourceResult:
        if self._direct_mode:
            return self.fetch(**params)
        if self._lambda_client is None:
            session = self._session or boto3.Session(
                region_name=config.aws_region,
                profile_name=config.aws_profile,
            )
            self._lambda_client = session.client("lambda")
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
            # The F4 Lambdas return DataSourceResult.model_dump(), i.e. `query_metadata`
            query_metadata=raw.get("query_metadata", raw.get("metadata", {})),
        )

    @abstractmethod
    def _build_payload(self, **params: Any) -> dict[str, Any]: ...

    @abstractmethod
    def fetch(self, **params: Any) -> DataSourceResult:
        """Call the public upstream API (with retry, see `shared.tools.retrying_http`)."""
        ...

    def as_langchain_tool(self) -> StructuredTool:
        # ADR: lazy langchain import | Rationale: the F4 source Lambdas import this module but ship
        # without langchain; only agent code builds LangChain tools
        from langchain_core.tools import StructuredTool

        tool_instance = self

        def _run(**kwargs: Any) -> dict[str, Any]:
            return tool_instance.invoke(**kwargs).model_dump(mode="json")

        return StructuredTool.from_function(
            func=_run,
            name=self.function_name,
            description=self.description,
            args_schema=self.InputSchema,
        )
