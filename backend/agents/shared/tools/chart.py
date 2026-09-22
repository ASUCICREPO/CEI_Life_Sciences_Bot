from __future__ import annotations

import base64
import textwrap
from typing import Any

from pydantic import BaseModel, ConfigDict

from shared.tools.code_interpreter import CodeInterpreterTool, Language


class ChartSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    title: str
    chart_type: str  # "line", "bar", "scatter", "pie"
    x_label: str = ""
    y_label: str = ""
    data: dict[str, Any]  # e.g. {"x": [...], "series_name": [...]}


class ChartTool:
    def __init__(self, code_interpreter: CodeInterpreterTool) -> None:
        self._ci = code_interpreter

    def render(self, spec: ChartSpec) -> bytes | None:
        """Run a matplotlib script in the sandbox, return PNG bytes or None on failure."""
        import json
        data_json = json.dumps(spec.data)

        code = textwrap.dedent(f"""\
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import json, base64, io

            data = json.loads({data_json!r})
            fig, ax = plt.subplots(figsize=(8, 5))

            chart_type = {spec.chart_type!r}
            title = {spec.title!r}
            x_label = {spec.x_label!r}
            y_label = {spec.y_label!r}

            x = data.get("x", list(range(len(next(iter(data.values()))))))
            series = {{k: v for k, v in data.items() if k != "x"}}

            if chart_type == "pie":
                labels = list(series.keys())
                values = [sum(v) if isinstance(v, list) else v for v in series.values()]
                ax.pie(values, labels=labels, autopct="%1.1f%%")
            elif chart_type == "scatter":
                for name, y in series.items():
                    ax.scatter(x, y, label=name)
                ax.legend()
            elif chart_type == "bar":
                import numpy as np
                width = 0.8 / max(len(series), 1)
                for i, (name, y) in enumerate(series.items()):
                    offsets = [xi + i * width for xi in range(len(x))]
                    ax.bar(offsets, y, width=width, label=name)
                ax.set_xticks([i + (len(series) - 1) * width / 2 for i in range(len(x))])
                ax.set_xticklabels(x, rotation=45, ha="right")
                ax.legend()
            else:  # line
                for name, y in series.items():
                    ax.plot(x, y, label=name, marker="o")
                ax.legend()

            ax.set_title(title)
            if x_label:
                ax.set_xlabel(x_label)
            if y_label:
                ax.set_ylabel(y_label)
            plt.tight_layout()

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=150)
            buf.seek(0)
            print("PNG_B64:" + base64.b64encode(buf.read()).decode())
            plt.close(fig)
        """)

        result = self._ci._run(code, Language.PYTHON)
        if result.get("is_error"):
            return None

        stdout = result.get("stdout", "")
        for line in stdout.splitlines():
            if line.startswith("PNG_B64:"):
                return base64.b64decode(line[8:])

        # Fall back to content items (AgentCore may return image bytes directly)
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "image":
                raw = item.get("source", {}).get("data", "")
                if raw:
                    return base64.b64decode(raw)

        return None
