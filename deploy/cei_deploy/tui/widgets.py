"""Widgets that render cleanly in browser terminals (AWS CloudShell, xterm.js)."""
from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widgets import OptionList, SelectionList
from textual.widgets.option_list import OptionDoesNotExist


class CheckList(SelectionList):
    """A SelectionList that draws plain "[x]" / "[ ]" marks.

    ADR: plain ASCII marks, not Textual's half-block toggle | Rationale: SelectionList draws its
    check box with the half-block glyphs U+2590/U+258C, which many browser-terminal fonts (AWS
    CloudShell) render with gaps or at the wrong width, and it shows the checked state by colour
    only | Alternative: keep the stock widget (rejected: hard to read in CloudShell and in 256 colours)
    """

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
        mark_style = base_style + Style(bold=checked, meta={"option": index})
        mark = "[x] " if checked else "[ ] "
        return Strip([Segment(mark, mark_style), *line])
