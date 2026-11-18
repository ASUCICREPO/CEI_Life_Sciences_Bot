"""Shared base class for every wizard screen: Back/Next footer, esc-to-go-back."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Static

if TYPE_CHECKING:
    from ..app import DeployApp
    from ..state import WizardState


class WizardScreen(Screen):
    """Base screen: provides `self.wizard`, an esc=back binding, and a Back/Next footer."""

    BINDINGS = [Binding("escape", "go_back", "Back", show=True)]

    def action_go_back(self) -> None:
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()

    @property
    def wizard(self) -> "WizardState":
        return self.app.wizard  # type: ignore[attr-defined]

    @property
    def wizard_app(self) -> "DeployApp":
        return self.app  # type: ignore[return-value]

    def footer_buttons(self, next_label: str = "Next", show_back: bool = True) -> Horizontal:
        buttons = []
        if show_back:
            buttons.append(Button("Back", id="nav-back", variant="default"))
        buttons.append(Button(next_label, id="nav-next", variant="primary"))
        return Horizontal(*buttons, id="nav-footer")

    def set_error(self, message: str) -> None:
        try:
            error_widget = self.query_one("#error-message", Static)
        except Exception:
            return
        error_widget.update(message)
