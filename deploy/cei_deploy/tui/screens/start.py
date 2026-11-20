"""Screen 1: AWS identity, Region selection, and the one-click "redeploy with saved settings" path."""
from __future__ import annotations

import os

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Select, Static

from ...prefetch import _FALLBACK_REGIONS
from ..widgets import Body, DropDown
from .base import WizardScreen
from .basics import BasicsScreen

class StartScreen(WizardScreen):
    HINT = "↑ ↓ ← → move   Space/Enter select   Ctrl+Q quit"

    def compose(self):
        yield Static("Step 1 of 5 · AWS account and Region", id="title")
        saved = self._has_saved_config()
        with Body(id="start-body"):
            if saved:
                with Horizontal(id="saved-actions"):
                    yield Button(
                        "Redeploy saved settings (%s)" % self.wizard.cfg.region, id="redeploy", variant="primary",
                    )
                    yield Button("Change settings", id="change-settings")
            # ADR: the Region picker is hidden behind "Change settings" when a saved config exists |
            # Rationale: users saw two competing primary actions on one screen; "Change settings" now
            # also confirms the Region, so the Region pre-fetch always starts (it was skipped before)
            with Vertical(id="region-section", classes="hidden" if saved else ""):
                yield Static("AWS Region to deploy into:", id="region-label")
                default_region = self.wizard.cfg.region or os.environ.get("AWS_REGION") or os.environ.get(
                    "AWS_DEFAULT_REGION"
                ) or "us-east-1"
                # ADR: a short list first, the full list from a background job | Rationale: botocore's
                # endpoint data took the UI thread for about a second while the screen was built
                regions = list(_FALLBACK_REGIONS)
                if default_region not in regions:
                    regions = [default_region] + regions
                yield DropDown(
                    [(r, r) for r in regions], value=default_region, id="region-select", allow_blank=False,
                )
                yield Button("Continue", id="continue", variant="primary")
        yield Static("", id="error-message")
        yield Horizontal(Static(self.HINT, id="key-hints"), id="nav-footer")

    def _has_saved_config(self) -> bool:
        return bool(self.wizard_app._has_saved_config)

    def on_mount(self) -> None:
        self._load_identity()
        self._load_regions()
        first = "#redeploy" if self._has_saved_config() else "#region-select"
        self.query_one(first).focus()

    @work(thread=True, group="regions")
    def _load_regions(self) -> None:
        future = getattr(self.wizard_app.prefetcher, "regions_future", None)
        if future is None:
            return
        try:
            regions = future.result(timeout=30)
        except Exception:  # noqa: BLE001 - keep the short list
            return
        self.app.call_from_thread(self._set_regions, regions)

    def _set_regions(self, regions: list) -> None:
        select = self.query_one("#region-select", Select)
        current = select.value
        if current not in regions:
            regions = [current] + list(regions)
        select.set_options([(r, r) for r in regions])
        select.value = current

    @work(thread=True, group="identity")
    def _load_identity(self) -> None:
        # The account is shown on the Review screen; here only a failure is shown (as an error).
        prefetcher = self.wizard_app.prefetcher
        try:
            identity = prefetcher.identity_future.result(timeout=30)
            self.wizard.account_id = identity.account_id
            self.wizard.caller_arn = identity.arn
            self.wizard.partition = identity.partition
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.set_error, "Could not read your AWS identity: %s" % exc)

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
