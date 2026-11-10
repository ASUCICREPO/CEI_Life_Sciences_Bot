"""Generate a reviewed report for a trusted, authenticated export operation.

The caller must authorize the user and session, load the requested completed turn
from storage, and validate the review acknowledgment before calling this service.
This module must not be exposed as a model tool or invoked from untrusted graph state.
"""
from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from shared.persistence.exports import ExportRepository
from shared.tools.code_interpreter import CodeInterpreterTool
from shared.tools.export import ExportTool, SUPPORTED_FORMATS

logger = logging.getLogger(__name__)


def generate_reviewed_export(
    *, repository: ExportRepository, user_id: str, session_id: str, turn_id: str,
    reviewed: bool, format: str, title: str, sections: list[dict[str, Any]],
    charts: dict[str, bytes] | None = None, chart_sections: dict[str, str] | None = None,
    doc_type: str = "latex", code_interpreter: CodeInterpreterTool | None = None,
    export_tool_factory: Callable[..., Any] = ExportTool,
) -> dict[str, Any]:
    """Render and persist an export after the authenticated caller confirms review.

    Temporary files are isolated per operation and removed on every exit. PDF and
    DOCX require a Code Interpreter. LaTeX with charts is rejected until its image
    assets can be bundled with the .tex file as a single downloadable artifact.
    """
    if reviewed is not True:
        raise ValueError("review acknowledgment is required")
    if not user_id or not session_id or not turn_id:
        raise ValueError("owner, session and turn are required")
    if format not in SUPPORTED_FORMATS:
        raise ValueError("unsupported export format")
    if format in {"pdf", "docx"} and code_interpreter is None:
        raise ValueError("Code Interpreter is required for this format")
    if format == "latex" and charts:
        raise ValueError("LaTeX exports with charts require asset bundling")

    with TemporaryDirectory(prefix="cei-export-") as directory:
        output_dir = Path(directory)
        tool = export_tool_factory(code_interpreter=code_interpreter, doc_type=doc_type)
        artifacts = tool.generate(title=title, sections=sections, charts=charts or {}, formats=[format],
                                  output_dir=output_dir, chart_sections=chart_sections)
        if len(artifacts) != 1 or artifacts[0].format != format:
            raise RuntimeError("report generation failed")
        artifact = artifacts[0]
        path = Path(artifact.local_path).resolve()
        if path.parent != output_dir.resolve() or not path.is_file():
            raise RuntimeError("report generation returned an invalid file")
        result = repository.save(user_id=user_id, session_id=session_id, turn_id=turn_id,
                                 filename=artifact.filename, format=format, data=path.read_bytes(), reviewed=True)
    logger.info("reviewed_export_created", extra={"user_id": user_id, "turn_id": turn_id,
                                                  "export_id": result["export_id"]})
    return result
