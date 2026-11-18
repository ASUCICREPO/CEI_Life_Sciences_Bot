"""Screen 4: repository URL/branch (+ PAT for private repos) and the Advanced settings."""
from __future__ import annotations

from textual import work
from textual.containers import Vertical
from textual.widgets import Button, Checkbox, Collapsible, Input, Select, Static, Switch

from ...config import REPOSITORY_BRANCH_PATTERN, REPOSITORY_URL_PATTERN, TOOL_POLICY_MODES
from ...git_info import to_https_github_url
from ..widgets import Body, DropDown
from .base import WizardScreen


class RepositoryScreen(WizardScreen):
    def compose(self):
        yield Static("Step 4 of 5 · Repository and advanced settings", id="title")
        with Body(id="repo-body"):
            yield Static("GitHub repository URL:")
            yield Input(
                value=self.wizard.cfg.repository.url, placeholder="https://github.com/org/repo", id="repo-url",
            )
            yield Static("Branch:")
            yield Input(value=self.wizard.cfg.repository.branch or "main", id="repo-branch")
            with Vertical(id="pat-section", classes="hidden"):
                if self.wizard.stored_pat_exists:
                    yield Checkbox("Use stored token", value=True, id="use-stored-pat")
                yield Static("GitHub personal access token:", id="pat-label")
                yield Input(password=True, id="pat-token")
            with Collapsible(title="Advanced", collapsed=True, id="advanced"):
                yield Static("Per-user requests/second limit:")
                yield Input(value=str(self.wizard.cfg.limits.requestsPerSecond), id="rps")
                yield Static("Max concurrent real-time runs per user:")
                yield Input(value=str(self.wizard.cfg.limits.maxRealtimeRunsPerUser), id="max-realtime")
                yield Static("Max concurrent background runs per user:")
                yield Input(value=str(self.wizard.cfg.limits.maxBackgroundRunsPerUser), id="max-background")
                yield Static("Tool Gateway policy mode:")
                yield DropDown(
                    [(m, m) for m in TOOL_POLICY_MODES], value=self.wizard.cfg.toolPolicyMode,
                    allow_blank=False, id="tool-policy-mode",
                )
                yield Static("Add http://localhost:5173 as a Cognito callback/logout URL (local dev only):")
                yield Switch(value=self.wizard.cfg.allowLocalhostCallback, id="allow-localhost-callback")
            yield Static("", id="error-message")
        yield self.footer_buttons()

    def on_mount(self) -> None:
        if not self.wizard.cfg.repository.url:
            self._prefill_from_git()
        self._load_visibility()

    def _prefill_from_git(self) -> None:
        prefetcher = self.wizard_app.prefetcher
        try:
            info = prefetcher.git_future.result(timeout=10)
        except Exception:
            return
        if info.url:
            self.query_one("#repo-url", Input).value = info.url
        if info.branch:
            self.query_one("#repo-branch", Input).value = info.branch

    @work(thread=True, exclusive=True)
    def _load_visibility(self) -> None:
        prefetcher = self.wizard_app.prefetcher
        try:
            visibility = prefetcher.github_visibility_future.result(timeout=15)
        except Exception:
            visibility = "unknown"
        self.wizard.repo_visibility = visibility

        region = self.wizard.cfg.region
        jobs = prefetcher.region_jobs(region)
        stored_arn = None
        if jobs is not None:
            try:
                stored_arn = jobs.pat_secret_arn.result(timeout=15)
            except Exception:
                stored_arn = None
        self.wizard.stored_pat_exists = bool(stored_arn)
        self.app.call_from_thread(self._apply_visibility, visibility)

    def _apply_visibility(self, visibility: str) -> None:
        try:
            # The field label carries the status, so no separate status line is needed.
            if visibility in ("private", "unknown"):
                self.query_one("#pat-label", Static).update(
                    "GitHub personal access token (private repository):"
                    if visibility == "private"
                    else "GitHub personal access token (only if the repository is private):"
                )
                self.query_one("#pat-section").remove_class("hidden")
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nav-back":
            self.action_go_back()
            return
        if event.button.id != "nav-next":
            return
        self._on_next()

    def _on_next(self) -> None:
        url = to_https_github_url(self.query_one("#repo-url", Input).value.strip())
        branch = self.query_one("#repo-branch", Input).value.strip()
        if not url or not REPOSITORY_URL_PATTERN.match(url):
            self.set_error("Repository URL must be https://github.com/<owner>/<repo>[.git].")
            return
        if not branch or not REPOSITORY_BRANCH_PATTERN.match(branch) or ".." in branch:
            self.set_error("Branch name is not valid.")
            return

        try:
            rps = int(self.query_one("#rps", Input).value)
            max_realtime = int(self.query_one("#max-realtime", Input).value)
            max_background = int(self.query_one("#max-background", Input).value)
        except ValueError:
            self.set_error("Requests/second and run limits must be whole numbers.")
            return
        if rps <= 0 or max_realtime <= 0 or max_background <= 0:
            self.set_error("Requests/second and run limits must be positive.")
            return

        pat_input = self.query_one("#pat-token", Input) if self._pat_section_visible() else None
        use_stored_checkbox = self._query_optional("#use-stored-pat", Checkbox)
        pat_value = pat_input.value.strip() if pat_input is not None else ""
        use_stored = bool(use_stored_checkbox.value) if use_stored_checkbox is not None else False
        if pat_value:
            self.wizard.pat_token = pat_value
            self.wizard.use_stored_pat = False
        elif use_stored and self.wizard.stored_pat_exists:
            self.wizard.pat_token = None
            self.wizard.use_stored_pat = True
        else:
            # ADR: a private repository needs a token before a real run | Rationale: without one,
            # CodeBuild cannot clone the source and the build fails after the IAM/CodeBuild writes |
            # Alternative: allow "unknown" visibility through (network errors must not block a public repo)
            if self.wizard.repo_visibility == "private" and not self.wizard.dry_run:
                self.set_error("This repository is private. Enter a GitHub personal access token.")
                return
            self.wizard.pat_token = None
            self.wizard.use_stored_pat = False

        self.wizard.cfg.repository.url = url
        self.wizard.cfg.repository.branch = branch
        self.wizard.cfg.limits.requestsPerSecond = rps
        self.wizard.cfg.limits.maxRealtimeRunsPerUser = max_realtime
        self.wizard.cfg.limits.maxBackgroundRunsPerUser = max_background
        self.wizard.cfg.toolPolicyMode = self.query_one("#tool-policy-mode", Select).value
        self.wizard.cfg.allowLocalhostCallback = self.query_one("#allow-localhost-callback", Switch).value
        self.set_error("")

        from .review import ReviewScreen

        self.app.push_screen(ReviewScreen())

    def _pat_section_visible(self) -> bool:
        return "hidden" not in self.query_one("#pat-section").classes

    def _query_optional(self, selector, widget_type):
        try:
            return self.query_one(selector, widget_type)
        except Exception:
            return None
