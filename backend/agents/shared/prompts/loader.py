from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

SHARED_TEMPLATES_DIR = Path(__file__).parent / "templates"


class PromptLoader:

    def __init__(self, agent_templates_dir: Path) -> None:
        search_paths = [str(agent_templates_dir), str(SHARED_TEMPLATES_DIR)]
        self._env = Environment(
            loader=FileSystemLoader(search_paths),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_name: str, **kwargs: object) -> str:
        template = self._env.get_template(template_name)
        return template.render(**kwargs)

    def system_prompt(self, mode: str, **kwargs: object) -> str:
        return self.render(f"system_{mode}.j2", mode=mode, **kwargs)
