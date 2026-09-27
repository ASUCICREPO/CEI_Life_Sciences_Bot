from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from shared.tools.chart import place_charts
from shared.tools.code_interpreter import CodeInterpreterTool, Language
from shared.tools.export_pdf import pdf_code
from shared.tools.export_docx import docx_code
from shared.tools.export_latex import latex_export

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = {"pdf", "docx", "md", "csv", "txt", "latex"}


class ExportArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)
    format: str
    filename: str
    local_path: str
    byte_size: int


class ExportTool:
    def __init__(self, code_interpreter: CodeInterpreterTool | None, doc_type: str = "latex") -> None:
        self._ci = code_interpreter
        self._doc_type = doc_type

    def generate(
        self,
        title: str,
        sections: list[dict[str, Any]],
        charts: dict[str, bytes],
        formats: list[str],
        output_dir: Path,
        chart_sections: dict[str, str] | None = None,
    ) -> list[ExportArtifact]:
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:60] or "document"
        artifacts: list[ExportArtifact] = []

        for fmt in formats:
            fmt = fmt.lower()
            if fmt not in SUPPORTED_FORMATS:
                logger.warning({"event": "export_unsupported_format", "format": fmt})
                continue
            try:
                out_path = output_dir / f"{safe_title}.{fmt}"
                if fmt == "md":
                    data = self.render_markdown(title, sections, charts, chart_sections)
                    out_path.write_text(data, encoding="utf-8")
                elif fmt == "txt":
                    data = self._generate_txt(title, sections)
                    out_path.write_text(data, encoding="utf-8")
                elif fmt == "csv":
                    data = self._generate_csv(sections)
                    out_path.write_text(data, encoding="utf-8")
                elif fmt == "latex":
                    tex_source, image_files = latex_export(
                        title=title,
                        sections=sections,
                        charts=charts,
                        chart_sections=chart_sections,
                        doc_type=self._doc_type,
                        output_dir=output_dir,
                    )
                    out_path = output_dir / f"{safe_title}.tex"
                    out_path.write_text(tex_source, encoding="utf-8")
                    # Write chart images alongside the .tex file
                    for img_name, img_bytes in image_files.items():
                        (output_dir / img_name).write_bytes(img_bytes)
                elif fmt == "pdf":
                    data = self._generate_via_ci(title, sections, charts, "pdf", chart_sections)
                    if data:
                        out_path.write_bytes(data)
                    else:
                        logger.error({"event": "export_pdf_failed"})
                        continue
                elif fmt == "docx":
                    data = self._generate_via_ci(title, sections, charts, "docx", chart_sections)
                    if data:
                        out_path.write_bytes(data)
                    else:
                        logger.error({"event": "export_docx_failed"})
                        continue

                byte_size = out_path.stat().st_size
                artifacts.append(ExportArtifact(
                    format=fmt,
                    filename=out_path.name,
                    local_path=str(out_path),
                    byte_size=byte_size,
                ))
                logger.info({"event": "export_complete", "format": fmt, "path": str(out_path), "bytes": byte_size})
            except Exception:
                logger.exception({"event": "export_error", "format": fmt})

        return artifacts

    # ── Local generators (no sandbox needed) ─────────────────────────────────

    @staticmethod
    def render_markdown(
        title: str,
        sections: list[dict[str, Any]],
        charts: dict[str, bytes] | None = None,
        chart_sections: dict[str, str] | None = None,
    ) -> str:
        """Final markdown document with charts embedded inline as PNG data URIs (self-contained)."""
        charts = charts or {}
        placed, unmatched = place_charts(sections, list(charts), chart_sections)
        lines = [f"# {title}", ""]
        for sec in sorted(sections, key=lambda s: s.get("order", 0)):
            if sec.get("heading"):
                lines.append(f"## {sec['heading']}")
                lines.append("")
            lines.append(sec.get("content", ""))
            lines.append("")
            for chart_title in placed.get(sec.get("order", 0), []):
                lines += ExportTool._md_figure(chart_title, charts[chart_title])
        if unmatched:
            lines += ["## Figures", ""]
            for chart_title in unmatched:
                lines += ExportTool._md_figure(chart_title, charts[chart_title])
        return "\n".join(lines)

    @staticmethod
    def _md_figure(chart_title: str, png_bytes: bytes) -> list[str]:
        alt = chart_title.replace("[", "").replace("]", "")
        data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
        return [f"![{alt}]({data_uri})", "", f"*Figure: {chart_title}*", ""]

    def _generate_txt(self, title: str, sections: list[dict[str, Any]]) -> str:
        lines = [title.upper(), "=" * len(title), ""]
        for sec in sorted(sections, key=lambda s: s.get("order", 0)):
            if sec.get("heading"):
                lines.append(sec["heading"].upper())
                lines.append("-" * len(sec["heading"]))
            # Strip markdown formatting
            content = re.sub(r"[#*_`\[\]()]", "", sec.get("content", ""))
            lines.append(content)
            lines.append("")
        return "\n".join(lines)

    def _generate_csv(self, sections: list[dict[str, Any]]) -> str:
        """Extract pipe-delimited or markdown tables from sections and convert to CSV."""
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Section", "Row"])

        for sec in sorted(sections, key=lambda s: s.get("order", 0)):
            content = sec.get("content", "")
            heading = sec.get("heading", "")
            for line in content.splitlines():
                if "|" in line and not line.strip().startswith("|---"):
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    writer.writerow([heading] + cells)

        return buf.getvalue()

    # ── Sandbox generators ────────────────────────────────────────────────────

    def _generate_via_ci(
        self,
        title: str,
        sections: list[dict[str, Any]],
        charts: dict[str, bytes],
        fmt: str,
        chart_sections: dict[str, str] | None = None,
    ) -> bytes | None:
        import json

        if self._ci is None:
            logger.error({"event": "ci_export_unavailable", "format": fmt})
            return None

        # Attach each chart to its section; unplaced charts go in a trailing Figures section
        placed, unmatched = place_charts(sections, list(charts), chart_sections)
        sections = [{**sec, "charts": placed.get(sec.get("order", 0), [])} for sec in sections]
        if unmatched:
            last_order = max((sec.get("order", 0) for sec in sections), default=0)
            sections.append({"heading": "Figures", "content": "", "order": last_order + 1, "charts": unmatched})

        sections_json = json.dumps(sections)
        charts_b64 = {k: base64.b64encode(v).decode() for k, v in charts.items()}
        charts_json = json.dumps(charts_b64)

        if fmt == "pdf":
            code = pdf_code(title, sections_json, charts_json)
        else:
            code = docx_code(title, sections_json, charts_json)

        result = self._ci._run(code, Language.PYTHON)
        if result.get("is_error"):
            logger.error({"event": "ci_export_error", "format": fmt, "stderr": result.get("stderr", "")})
            return None

        stdout = result.get("stdout", "")
        marker = f"{fmt.upper()}_B64:"
        for line in stdout.splitlines():
            if line.startswith(marker):
                return base64.b64decode(line[len(marker):])

        return None

