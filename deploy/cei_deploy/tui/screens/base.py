"""Shared base class for every wizard screen: keyboard navigation, key hints, Back/Next footer."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Static

if TYPE_CHECKING:
    from ..app import DeployApp
    from ..state import WizardState

DEFAULT_HINT = "Tab/Down next · Shift+Tab/Up back · Enter press/open · Esc previous step"


class WizardScreen(Screen):
    """Base screen: provides `self.wizard`, keyboard navigation, and a footer with key hints.

    ADR: arrow keys move focus between fields and buttons | Rationale: users in AWS CloudShell
    expected Up/Down and Left/Right to move between options; Textual binds only Tab/Shift+Tab by
    default. Screen bindings are not priority bindings, so a focused widget that uses an arrow key
    (Input cursor, Select, the model list) still gets it first | Alternative: priority bindings
    (rejected: would break cursor movement in inputs and lists)
    """

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=False),
        Binding("down", "focus_next", "Next field", show=False),
        Binding("up", "focus_previous", "Previous field", show=False),
        Binding("right", "focus_next_button", show=False),
        Binding("left", "focus_previous_button", show=False),
    ]

    #: The key-hint text shown in the footer; screens may override it.
    HINT = DEFAULT_HINT

    #: First control to focus when a screen opens (never a scroll container).
    AUTO_FOCUS = "Input, Select, SelectionList, Button"

    def on_mount(self) -> None:
        # ADR: compact (borderless, one-row) inputs, selects and buttons on every screen | Rationale:
        # the default "tall" borders use half-block glyphs that browser terminals (AWS CloudShell)
        # draw with gaps, and each bordered widget costs 3 rows in a small CloudShell pane |
        # Alternative: per-widget compact=True at each call site (rejected: easy to miss one)
        for widget in self.query("Input, Select, Button"):
            widget.compact = True  # type: ignore[attr-defined]
        # ADR: scroll containers never take focus | Rationale: a focusable VerticalScroll made Tab and
        # Down stop on an invisible container, so keys seemed to do nothing; focusing a child still
        # scrolls it into view
        for container in self.query(VerticalScroll):
            container.can_focus = False

    def action_go_back(self) -> None:
        if len(self.app.screen_stack) > 2:  # the stack's first entry is Textual's default screen
            self.app.pop_screen()

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def action_focus_next_button(self) -> None:
        # Left/Right move only between buttons, so they never steal a text cursor.
        if isinstance(self.focused, Button):
            self.focus_next(Button)

    def action_focus_previous_button(self) -> None:
        if isinstance(self.focused, Button):
            self.focus_previous(Button)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in a text field moves to the next field (then to the footer buttons).
        event.stop()
        self.focus_next()

    @property
    def wizard(self) -> "WizardState":
        return self.app.wizard  # type: ignore[attr-defined]

    @property
    def wizard_app(self) -> "DeployApp":
        return self.app  # type: ignore[return-value]

    def footer_buttons(
        self, next_label: str = "Next", show_back: bool = True, extra: Optional[Iterable[Button]] = None,
    ) -> Horizontal:
        children = [Static(self.HINT, id="key-hints")]
        if show_back:
            children.append(Button("Back", id="nav-back", variant="default"))
        children.extend(extra or [])
        children.append(Button(next_label, id="nav-next", variant="primary"))
        return Horizontal(*children, id="nav-footer")

    def set_error(self, message: str) -> None:
        try:
            error_widget = self.query_one("#error-message", Static)
        except Exception:
            return
        error_widget.update(message)
