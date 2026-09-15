from __future__ import annotations

from enum import StrEnum
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel


class Language(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"


class CodeInterpreterInput(BaseModel):
    code: str
    language: Language = Language.PYTHON


class CodeInterpreterTool:
    name = "run_code"
    description = "Execute code in a sandboxed environment. Supports Python, JavaScript, and TypeScript."

    def _run(self, code: str, language: Language = Language.PYTHON) -> dict[str, Any]:
        raise NotImplementedError("Delegated to AgentCore built-in code interpreter sandbox")

    def as_langchain_tool(self) -> StructuredTool:
        return StructuredTool.from_function(
            func=self._run,
            name=self.name,
            description=self.description,
            args_schema=CodeInterpreterInput,
        )
