"""Warp-style branch dropdown: filter, switch, or create a branch."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

GREEN, MUTED = "#6fcf97", "#8e8e93"


class BranchPicker(ModalScreen[tuple[str, str] | None]):
    """Dismisses with ("switch", name), ("create", name) or None."""

    # Styles live in tui/theme.tcss: app CSS outranks the widgets' own defaults.
    BINDINGS = [Binding("escape", "dismiss_none", "Close", show=False),
                Binding("down", "move(1)", show=False), Binding("up", "move(-1)", show=False)]

    def __init__(self, branches: list[tuple[str, bool]], current: str, anchor: tuple[int, int] | None = None):
        super().__init__()
        self.branches = sorted(branches, key=lambda b: b[0] != current)  # current first, rest keep order
        self.current = current
        self.anchor = anchor  # screen (x, y) right under the branch name

    def compose(self) -> ComposeResult:
        with Vertical(id="branch-picker"):
            yield Input(placeholder="Filter or new branch...", id="branch-filter")
            yield OptionList(id="branch-options")

    def on_mount(self) -> None:
        box = self.query_one("#branch-picker")
        longest = max((len(n) + (8 if remote else 0) for n, remote in self.branches), default=10)
        width = min(max(longest + 8, 36), 64)
        box.styles.width = width
        if self.anchor:  # align the text of the list with the branch name above it
            x, y = self.anchor
            box.styles.offset = (max(0, min(x - 4, self.app.size.width - width - 1)), y)
        else:
            box.styles.offset = (max(0, self.app.size.width - width - 4), 6)
        # Fill the list after the first refresh: OptionList caches rendered lines, and before
        # that point the app theme's styles are not applied yet (rows would keep default colors).
        self.call_after_refresh(self._refresh, "")
        self.query_one(Input).focus()
        box.styles.animate("opacity", value=1.0, duration=0.12, easing="out_cubic")

    def _refresh(self, query: str) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        q = query.strip()
        for name, remote in self.branches:
            if q.lower() not in name.lower():
                continue
            label = Text()
            label.append("● " if name == self.current else "  ", style=GREEN)  # aligns names with the input
            label.append(name, style="bold" if name == self.current else "")
            if remote:
                label.append("  remote", style=MUTED)
            options.add_option(Option(label, id=f"switch:{name}"))
        if q and q not in (n for n, _ in self.branches):
            options.add_option(Option(Text.assemble(("+ ", GREEN), f'Create branch "{q}"'), id=f"create:{q}"))
        if options.option_count:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._refresh(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # never let the filter text reach the app as a message to the agent
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            self._choose(options.get_option_at_index(options.highlighted).id or "")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose(event.option.id or "")

    def action_move(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if options.option_count:
            current = options.highlighted if options.highlighted is not None else -delta
            options.highlighted = (current + delta) % options.option_count

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def on_click(self, event: events.Click) -> None:
        """A click anywhere outside the dropdown closes it (including on the branch name again)."""
        if not self.query_one("#branch-picker").region.contains_point(event.screen_offset):
            event.stop()
            self.dismiss(None)

    def _choose(self, option_id: str) -> None:
        kind, _, name = option_id.partition(":")
        if kind == "switch" and name == self.current:
            self.dismiss(None)
        elif kind in ("switch", "create") and name:
            self.dismiss((kind, name))
