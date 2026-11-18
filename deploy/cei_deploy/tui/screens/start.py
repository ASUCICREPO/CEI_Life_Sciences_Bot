"""Screen 1: AWS identity, Region selection, and the one-click "redeploy with saved settings" path."""
from __future__ import annotations

import os

from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Select, Static

from ..widgets import DropDown
from .base import WizardScreen
from .basics import BasicsScreen

_FALLBACK_REGIONS = (
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-central-1",
    "ap-northeast-1", "ap-southeast-2",
)


def _available_regions(session_factory) -> list:
    try:
        session = session_factory()
        regions = session.get_available_regions("bedrock-agentcore")
        if regions:
            return sorted(regions)
    except Exception:
        pass
    return list(_FALLBACK_REGIONS)


class StartScreen(WizardScreen):
    HINT = "Tab/Down next · Shift+Tab/Up back · Left/Right between buttons · Enter press/open · Ctrl+Q quit"

    def compose(self):
        yield Static("Step 1 of 5 · AWS account and Region", id="title")
        yield Static("Looking up your AWS identity...", id="identity-line")
        saved = self._has_saved_config()
        with VerticalScroll(id="start-body"):
            if saved:
                yield Static(
                    "Saved settings found for Region %s. Redeploy them as they are, or change them."
                    % self.wizard.cfg.region,
                    id="saved-notice",
                )
                with Horizontal(id="saved-actions"):
                    yield Button("Redeploy with saved settings", id="redeploy", variant="primary")
                    yield Button("Change settings", id="change-settings")
            # ADR: the Region picker is hidden behind "Change settings" when a saved config exists |
            # Rationale: users saw two competing primary actions on one screen; "Change settings" now
            # also confirms the Region, so the Region pre-fetch always starts (it was skipped before)
            with Vertical(id="region-section", classes="hidden" if saved else ""):
                yield Static("AWS Region to deploy into:", id="region-label")
                default_region = self.wizard.cfg.region or os.environ.get("AWS_REGION") or os.environ.get(
                    "AWS_DEFAULT_REGION"
                ) or "us-east-1"
                regions = _available_regions(self.wizard_app.session_factory)
                if default_region not in regions:
                    regions = [default_region] + regions
                yield DropDown(
                    [(r, r) for r in regions], value=default_region, id="region-select", allow_blank=False,
                )
                yield Button("Continue", id="continue", variant="primary")
        yield Horizontal(Static(self.HINT, id="key-hints"), id="nav-footer")

    def _has_saved_config(self) -> bool:
        return bool(self.wizard_app._has_saved_config)

    def on_mount(self) -> None:
        self._load_identity()
        first = "#redeploy" if self._has_saved_config() else "#region-select"
        self.query_one(first).focus()

    @work(thread=True, exclusive=True)
    def _load_identity(self) -> None:
        prefetcher = self.wizard_app.prefetcher
        try:
            identity = prefetcher.identity_future.result(timeout=30)
            line = "AWS account %s (%s)" % (identity.account_id, identity.arn)
            self.wizard.account_id = identity.account_id
            self.wizard.caller_arn = identity.arn
            self.wizard.partition = identity.partition
        except Exception as exc:  # noqa: BLE001
            line = "Could not determine your AWS identity: %s" % exc
        self.app.call_from_thread(self._set_identity_line, line)

    def _set_identity_line(self, line: str) -> None:
        try:
            self.query_one("#identity-line", Static).update(line)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "redeploy":
            region = self.wizard.cfg.region
            self.wizard.region_confirmed = True
            self.wizard_app.prefetcher.start_region(region)
            from .review import ReviewScreen

            self.app.push_screen(ReviewScreen())
        elif event.button.id == "change-settings":
            self.query_one("#region-section").remove_class("hidden")
            self.query_one("#region-select", Select).focus()
        elif event.button.id == "continue":
            select = self.query_one("#region-select", Select)
            region = select.value
            if not region:
                return
            self.wizard.cfg.region = region
            self.wizard.region_confirmed = True
            self.wizard_app.prefetcher.start_region(region)
            self.app.push_screen(BasicsScreen())
