"""Screen 3: model discovery, provider-scoped pick list, and default/router selection."""
from __future__ import annotations

from typing import Dict, List

from textual import work
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button, Input, LoadingIndicator, Select, SelectionList, Static, TabbedContent, TabPane,
)

from ... import models as models_mod
from ...config import MODEL_ID_PATTERN
from ..widgets import CheckList
from .base import WizardScreen

NOTE_TEXT = (
    "Global and US share one setting per model. Unchecked models are blocked in the app. "
    "New models released later stay allowed."
)


def _option_label(item: "models_mod.ModelItem") -> str:
    return "%s (%s)" % (item.label, item.provider)


def _matches(item: "models_mod.ModelItem", needle: str) -> bool:
    if not needle:
        return True
    needle = needle.lower()
    return needle in item.label.lower() or needle in item.profile_id.lower() or needle in item.provider.lower()


class ModelsScreen(WizardScreen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._syncing = False
        self._displayed: Dict[str, List["models_mod.ModelItem"]] = {"global": [], "us": []}

    def compose(self):
        yield Static("Models", id="title")
        yield LoadingIndicator(id="models-loading")
        with VerticalScroll(id="models-error", classes="hidden"):
            yield Static("", id="models-error-text")
            yield Static("Default Bedrock model ID:")
            yield Input(value=self.wizard.cfg.defaultModelId, id="manual-default-model")
            yield Static("Router Bedrock model ID:")
            yield Input(value=self.wizard.cfg.routerModelId, id="manual-router-model")
        with VerticalScroll(id="models-body", classes="hidden"):
            yield Input(placeholder="Search models...", id="model-search")
            with TabbedContent(id="model-tabs"):
                with TabPane("Global", id="tab-global"):
                    yield CheckList(id="list-global")
                with TabPane("US", id="tab-us"):
                    yield CheckList(id="list-us")
            with Horizontal(id="select-actions"):
                yield Button("Select all", id="select-all")
                yield Button("Clear all", id="clear-all")
            yield Static(NOTE_TEXT, id="models-note")
            yield Static("Default model:")
            yield Select([], id="default-model-select", allow_blank=True)
            yield Static("Router (classifier) model:")
            yield Select([], id="router-model-select", allow_blank=True)
        yield Static("", id="error-message")
        yield self.footer_buttons()

    def on_mount(self) -> None:
        self._load_discovery()

    @work(thread=True, exclusive=True)
    def _load_discovery(self) -> None:
        region = self.wizard.cfg.region
        jobs = self.wizard_app.prefetcher.region_jobs(region) or self.wizard_app.prefetcher.start_region(region)
        try:
            discovery = jobs.models.result(timeout=120)
            self.wizard.discovery = discovery
            if not self.wizard.checked_base_ids:
                if self.wizard.cfg.blockedModelIds:
                    self.wizard.checked_base_ids = set(
                        models_mod.checked_base_ids_from_blocklist(discovery.all_items(), self.wizard.cfg.blockedModelIds)
                    )
                else:
                    self.wizard.checked_base_ids = {item.base_model_id for item in discovery.all_items()}
            self.app.call_from_thread(self._show_discovered)
        except Exception as exc:  # noqa: BLE001 - surfaced as a manual-input fallback
            self.wizard.discovery_error = str(exc)
            self.wizard.manual_models = True
            self.app.call_from_thread(self._show_manual, str(exc))

    def _show_discovered(self) -> None:
        self.query_one("#models-loading").display = False
        self.query_one("#models-body").remove_class("hidden")
        self._populate("")
        self._refresh_default_router_selects()

    def _show_manual(self, error: str) -> None:
        self.query_one("#models-loading").display = False
        error_box = self.query_one("#models-error")
        error_box.remove_class("hidden")
        permissions = ", ".join(models_mod.ModelDiscoveryError.REQUIRED_PERMISSIONS)
        self.query_one("#models-error-text", Static).update(
            "Could not list Bedrock models (%s). Your AWS credentials need: %s.\n"
            "Enter model IDs directly below." % (error, permissions)
        )

    # -- populate / filter ---------------------------------------------------
    def _populate(self, filter_text: str) -> None:
        discovery = self.wizard.discovery
        if discovery is None:
            return
        global_list = self.query_one("#list-global", SelectionList)
        us_list = self.query_one("#list-us", SelectionList)
        global_list.clear_options()
        us_list.clear_options()
        for widget, group, items in (
            (global_list, "global", discovery.global_items), (us_list, "us", discovery.us_items),
        ):
            visible = [item for item in items if _matches(item, filter_text)]
            self._displayed[group] = visible
            for item in visible:
                checked = item.base_model_id in self.wizard.checked_base_ids
                widget.add_option((_option_label(item), item.profile_id, checked))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-search":
            self._populate(event.value)

    # -- selection linking ----------------------------------------------------
    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if self._syncing:
            return
        global_list = self.query_one("#list-global", SelectionList)
        us_list = self.query_one("#list-us", SelectionList)
        changed_list, changed_group = (
            (global_list, "global") if event.selection_list is global_list else (us_list, "us")
        )
        other_list, other_group = (us_list, "us") if changed_group == "global" else (global_list, "global")

        selected_profile_ids = set(changed_list.selected)
        changed_items = self._displayed[changed_group]
        checked_bases = {i.base_model_id for i in changed_items if i.profile_id in selected_profile_ids}
        unchecked_bases = {i.base_model_id for i in changed_items} - checked_bases

        self._syncing = True
        try:
            other_selected = set(other_list.selected)
            for item in self._displayed[other_group]:
                if item.base_model_id in checked_bases and item.profile_id not in other_selected:
                    other_list.select(item.profile_id)
                elif item.base_model_id in unchecked_bases and item.profile_id in other_selected:
                    other_list.deselect(item.profile_id)
        finally:
            self._syncing = False

        self._recompute_checked_base_ids()
        self._refresh_default_router_selects()

    def _recompute_checked_base_ids(self) -> None:
        global_list = self.query_one("#list-global", SelectionList)
        us_list = self.query_one("#list-us", SelectionList)
        selected_ids = set(global_list.selected) | set(us_list.selected)
        for item in self._displayed["global"] + self._displayed["us"]:
            if item.profile_id in selected_ids:
                self.wizard.checked_base_ids.add(item.base_model_id)
            else:
                self.wizard.checked_base_ids.discard(item.base_model_id)

    def _refresh_default_router_selects(self) -> None:
        discovery = self.wizard.discovery
        if discovery is None:
            return
        checked_items = [i for i in discovery.all_items() if i.base_model_id in self.wizard.checked_base_ids]
        options = [
            ("%s [%s]" % (_option_label(i), "Global" if i.profile_id.startswith("global.") else "US"), i.profile_id)
            for i in checked_items
        ]
        valid_ids = {i.profile_id for i in checked_items}
        default_select = self.query_one("#default-model-select", Select)
        router_select = self.query_one("#router-model-select", Select)
        default_select.set_options(options)
        router_select.set_options(options)

        current_default = self.wizard.cfg.defaultModelId or discovery.default_model_id
        current_router = self.wizard.cfg.routerModelId or discovery.router_model_id
        fallback = next(iter(valid_ids)) if valid_ids else Select.BLANK
        default_select.value = current_default if current_default in valid_ids else fallback
        router_select.value = current_router if current_router in valid_ids else fallback

    # -- buttons ----------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nav-back":
            self.action_go_back()
            return
        if event.button.id == "select-all":
            self._bulk_set(True)
            return
        if event.button.id == "clear-all":
            self._bulk_set(False)
            return
        if event.button.id != "nav-next":
            return
        self._on_next()

    def _bulk_set(self, checked: bool) -> None:
        global_list = self.query_one("#list-global", SelectionList)
        us_list = self.query_one("#list-us", SelectionList)
        self._syncing = True
        try:
            for widget, group in ((global_list, "global"), (us_list, "us")):
                for item in self._displayed[group]:
                    if checked:
                        widget.select(item.profile_id)
                    else:
                        widget.deselect(item.profile_id)
        finally:
            self._syncing = False
        self._recompute_checked_base_ids()
        self._refresh_default_router_selects()

    def _on_next(self) -> None:
        if self.wizard.manual_models:
            default_id = self.query_one("#manual-default-model", Input).value.strip()
            router_id = self.query_one("#manual-router-model", Input).value.strip()
            if not default_id or not MODEL_ID_PATTERN.match(default_id):
                self.set_error("Enter a valid default model ID.")
                return
            if not router_id or not MODEL_ID_PATTERN.match(router_id):
                self.set_error("Enter a valid router model ID.")
                return
            self.wizard.cfg.defaultModelId = default_id
            self.wizard.cfg.routerModelId = router_id
            self.wizard.cfg.blockedModelIds = []
        else:
            discovery = self.wizard.discovery
            if discovery is None or not self.wizard.checked_base_ids:
                self.set_error("Keep at least one model checked.")
                return
            default_select = self.query_one("#default-model-select", Select)
            router_select = self.query_one("#router-model-select", Select)
            if default_select.value in (None, Select.BLANK) or router_select.value in (None, Select.BLANK):
                self.set_error("Choose a default model and a router model.")
                return
            self.wizard.cfg.defaultModelId = default_select.value
            self.wizard.cfg.routerModelId = router_select.value
            self.wizard.cfg.blockedModelIds = models_mod.compute_blocked_model_ids(
                discovery.all_backend_ids, sorted(self.wizard.checked_base_ids),
            )
        self.set_error("")

        from .repository import RepositoryScreen

        self.app.push_screen(RepositoryScreen())
