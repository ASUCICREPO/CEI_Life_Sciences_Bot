"""The Textual application shell: shared state, prefetching, and screen navigation."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from textual.app import App
from textual.binding import Binding

from .. import config as config_mod
from ..prefetch import Prefetcher
from .screens.start import StartScreen
from .state import WizardState

_CSS_PATH = str(Path(__file__).with_name("app.tcss"))


class DeployApp(App):
    """CEI Life Sciences Bot guided deployment configurator."""

    CSS_PATH = _CSS_PATH
    TITLE = "CEI Life Sciences Bot — Deploy"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", priority=True)]

    def __init__(
        self, *, repo_root: Path, dry_run: bool = False,
        session_factory: Optional[Callable] = None, prefetcher=None,
    ):
        super().__init__()
        self.repo_root = repo_root
        self.session_factory = session_factory
        config_path = config_mod.default_config_path(repo_root)
        existing = config_mod.load(config_path)
        self._has_saved_config = existing is not None
        cfg = existing if existing is not None else config_mod.DeployConfig()
        self.wizard = WizardState(cfg=cfg, config_path=config_path, repo_root=repo_root, dry_run=dry_run)
        self.prefetcher = prefetcher if prefetcher is not None else Prefetcher(session_factory, repo_root)
        self.exit_code: Optional[int] = None
        self.transcript: List[str] = []

    def on_mount(self) -> None:
        self.prefetcher.start_global()
        self.push_screen(StartScreen())

    def action_quit(self) -> None:
        if self.exit_code is None:
            self.exit_code = 1
        self.exit()
