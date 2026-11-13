from __future__ import annotations

import logging
import time
import uuid
from enum import StrEnum
from typing import Any

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_BOOTSTRAP_TEMPLATE = """\
import boto3, tarfile, sys, os
_PKG_DIR = "/tmp/site-packages"
if not os.path.isdir(_PKG_DIR):
    s3 = boto3.client("s3")
    s3.download_file("{bucket}", "{key}", "/tmp/packages.tar.gz")
    os.makedirs(_PKG_DIR, exist_ok=True)
    with tarfile.open("/tmp/packages.tar.gz") as tar:
        tar.extractall(_PKG_DIR)
    os.remove("/tmp/packages.tar.gz")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
print("packages ready")
"""

_LANG_MAP: dict[str, str] = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
}


class Language(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"


class CodeInterpreterInput(BaseModel):
    code: str
    language: Language = Language.PYTHON


class CodeInterpreterResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: int
    is_error: bool
    content: list[dict[str, Any]]


def _extract_result(response: dict[str, Any]) -> dict[str, Any]:
    stdout = ""
    stderr = ""
    exit_code = 0
    execution_time_ms = 0
    content: list[dict[str, Any]] = []

    for event in response.get("stream", []):
        result = event.get("result", {})
        structured = result.get("structuredContent", {})
        if structured:
            stdout += structured.get("stdout", "")
            stderr += structured.get("stderr", "")
            exit_code = structured.get("exitCode", exit_code)
            execution_time_ms = structured.get("executionTime", execution_time_ms)
        for item in result.get("content", []):
            content.append(item)

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "execution_time_ms": execution_time_ms,
        "is_error": exit_code != 0,
        "content": content,
    }


class CodeInterpreterTool:
    name = "run_code"
    description = "Execute code in a sandboxed environment. Supports Python, JavaScript, and TypeScript."

    def __init__(
        self,
        region: str,
        code_interpreter_id: str,
        packages_s3_bucket: str = "",
        packages_s3_key: str = "",
    ) -> None:
        self._region = region
        self._code_interpreter_id = code_interpreter_id
        self._packages_s3_bucket = packages_s3_bucket
        self._packages_s3_key = packages_s3_key

    def _run(self, code: str, language: Language = Language.PYTHON) -> dict[str, Any]:
        client = boto3.client("bedrock-agentcore", region_name=self._region)
        session_id: str | None = None
        start_ms = int(time.monotonic() * 1000)

        try:
            session_resp = client.start_code_interpreter_session(
                codeInterpreterIdentifier=self._code_interpreter_id,
                name=f"cei-{uuid.uuid4().hex[:8]}",
                sessionTimeoutSeconds=900,
            )
            session_id = session_resp["sessionId"]

            lang_str = _LANG_MAP[language.value]

            if language is Language.PYTHON and self._packages_s3_bucket:
                bootstrap = _BOOTSTRAP_TEMPLATE.format(
                    bucket=self._packages_s3_bucket,
                    key=self._packages_s3_key,
                )
                client.invoke_code_interpreter(
                    codeInterpreterIdentifier=self._code_interpreter_id,
                    sessionId=session_id,
                    name="executeCode",
                    arguments={"language": "python", "code": bootstrap},
                )

            response = client.invoke_code_interpreter(
                codeInterpreterIdentifier=self._code_interpreter_id,
                sessionId=session_id,
                name="executeCode",
                arguments={"language": lang_str, "code": code},
            )

            result = _extract_result(response)
            result["execution_time_ms"] = int(time.monotonic() * 1000) - start_ms
            logger.info(
                {"event": "code_interpreter_run", "language": lang_str,
                 "exit_code": result["exit_code"], "execution_time_ms": result["execution_time_ms"]}
            )
            return result

        except Exception as exc:
            logger.error({"event": "code_interpreter_error", "error_type": type(exc).__name__})
            return CodeInterpreterResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                execution_time_ms=int(time.monotonic() * 1000) - start_ms,
                is_error=True,
                content=[],
            ).model_dump()

        finally:
            if session_id is not None:
                try:
                    client.stop_code_interpreter_session(
                        codeInterpreterIdentifier=self._code_interpreter_id,
                        sessionId=session_id,
                    )
                except Exception as stop_exc:
                    logger.warning({"event": "code_interpreter_stop_failed", "error_type": type(stop_exc).__name__})

    def as_langchain_tool(self) -> StructuredTool:
        return StructuredTool.from_function(
            func=self._run,
            name=self.name,
            description=self.description,
            args_schema=CodeInterpreterInput,
        )
