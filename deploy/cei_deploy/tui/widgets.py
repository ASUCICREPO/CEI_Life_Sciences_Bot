"""Widgets that render cleanly in browser terminals (AWS CloudShell, xterm.js)."""
from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.strip import Strip
from textual.widgets import OptionList, Select, SelectionList
from textual.widgets.option_list import OptionDoesNotExist


class CheckList(SelectionList):
    """A SelectionList that draws "[✓]" (available) / "[ ]" (blocked) marks.

    ADR: text marks, not Textual's half-block toggle | Rationale: SelectionList draws its
    check box with the half-block glyphs U+2590/U+258C, which many browser-terminal fonts (AWS
    CloudShell) render with gaps or at the wrong width, and it shows the checked state by colour
    only | Alternative: keep the stock widget (rejected: hard to read in CloudShell and in 256 colours)
    """

    def action_cursor_down(self) -> None:
        # ADR: Down on the last row (Up on the first) leaves the list | Rationale: with arrow keys
        # only, the list was a dead end -- OptionList keeps Up/Down and stops at its ends
        if self.highlighted is None or self.highlighted >= self.option_count - 1:
            self.screen.focus_next()
        else:
            super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.highlighted is None or self.highlighted <= 0:
            self.screen.focus_previous()
        else:
            super().action_cursor_up()

    def on_focus(self) -> None:
        # Highlight the first row on focus, so Space works at once (no row is highlighted by default).
        if self.highlighted is None and self.option_count:
            self.highlighted = 0

    def render_line(self, y: int) -> Strip:
        # ADR: skip SelectionList.render_line and build on OptionList's | Rationale: the stock method
        # prepends the half-block button; the base line is the plain prompt row
        line = OptionList.render_line(self, y)
        _, scroll_y = self.scroll_offset
        index = scroll_y + y
        try:
            selection = self.get_option_at_index(index)
        except OptionDoesNotExist:
            return line
        base_style = next(iter(line)).style or self.rich_style
        checked = selection.value in self._selected
        meta = Style(meta={"option": index})
        if checked:
            # ADR: a green check mark, not "x" | Rationale: user request -- a check reads as "this
            # model will be available"; "x" reads as "removed" | U+2713 is in the common monospace
            # fonts browser terminals use (CloudShell included)
            mark_style = base_style + Style(bold=True, color="green") + meta
            mark = "[✓] "
        else:
            mark_style = base_style + Style(dim=True) + meta
            mark = "[ ] "
        return Strip([Segment(mark, mark_style), *line])


class DropDown(Select, inherit_bindings=False):
    """A Select that opens on Enter or Space only.

    ADR: Up/Down do not open the list | Rationale: the stock Select opens on Up/Down too, so the
    wizard's "Down = next field" key stopped at every dropdown | Alternative: keep stock bindings
    (rejected: inconsistent with the key-hint bar)
    """

    BINDINGS = [Binding("enter,space", "show_overlay", "Open list", show=False)]


class Body(VerticalScroll, inherit_bindings=False):
    """A screen body that scrolls (mouse wheel, focus) but leaves the arrow keys to navigation.

    ADR: no scroll key bindings on screen bodies | Rationale: VerticalScroll binds Up/Down/PageUp/...
    to scrolling, so on a screen taller than the terminal (the Models screen in a CloudShell pane)
    Down scrolled the page and focus never moved; focusing a widget still scrolls it into view |
    Alternative: priority arrow bindings on the screen (rejected: would break lists and inputs)
    """
