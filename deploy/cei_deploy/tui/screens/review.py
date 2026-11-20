"""Screen 5: summary, Deploy button, provisioning log, and build polling."""
from __future__ import annotations

from textual import work

from textual.widgets import Button, RichLog, Static

from ... import config as config_mod
from ... import provision
from ...git_info import resolve_source_version
from ..widgets import Body
from .base import WizardScreen


def _summary_lines(wizard) -> list:
    cfg = wizard.cfg
    lines = [
        "Region:                 %s" % cfg.region,
        "Repository:              %s @ %s" % (cfg.repository.url, cfg.repository.branch),
        "Default model:           %s" % cfg.defaultModelId,
        "Router model:            %s" % cfg.routerModelId,
        "Blocked models:          %d" % len(cfg.blockedModelIds),
        "Allowed email domains:   %s" % ", ".join(cfg.allowedEmailDomains),
        "Admin email:             %s" % cfg.adminEmail,
        "Requests/sec limit:      %d" % cfg.limits.requestsPerSecond,
        "Max real-time runs/user: %d" % cfg.limits.maxRealtimeRunsPerUser,
        "Max background runs:     %d" % cfg.limits.maxBackgroundRunsPerUser,
        "Allow localhost callback: %s" % cfg.allowLocalhostCallback,
        "Tool policy mode:        %s" % cfg.toolPolicyMode,
        "GitHub PAT:              %s" % (
            "new token" if wizard.pat_token else ("stored token" if wizard.use_stored_pat else "none")
        ),
        "AWS account:             %s" % wizard.account_id,
        "Dry run:                 %s" % wizard.dry_run,
    ]
    if wizard.source_version:
        pin_note = "pinned commit" if wizard.source_pinned else "branch head (could not resolve a commit SHA)"
        lines.append("Source version:          %s (%s)" % (wizard.source_version, pin_note))
    if wizard.leftover_spike_stacks:
        lines.append("")
        lines.append("Leftover spike stacks found (not deleted automatically):")
        for name in wizard.leftover_spike_stacks:
            lines.append("  aws cloudformation delete-stack --stack-name %s --region %s" % (name, cfg.region))
    return lines


class ReviewScreen(WizardScreen):
    def compose(self):
        yield Static("Step 5 of 5 · Review and deploy", id="title")
        with Body(id="review-body"):
            yield Static("", id="summary")
            yield RichLog(id="provision-log", wrap=True, highlight=False, markup=False)
        # ADR: Deploy lives in the docked footer, not the scrolling body | Rationale: in a 25-row
        # CloudShell pane the summary pushes a body button below the fold, so users saw only "Back"
        deploy_label = "Dry run" if self.wizard.dry_run else "Deploy"
        yield self.footer_buttons(next_label="Finish", extra=[Button(deploy_label, id="deploy", variant="primary")])

    def on_mount(self) -> None:
        self.query_one("#nav-next").display = False
        self._prepare_summary()

    @work(thread=True, group="summary")
    def _prepare_summary(self) -> None:
        wizard = self.wizard
        region = wizard.cfg.region
        wizard.source_version, wizard.source_pinned = resolve_source_version(
            wizard.repo_root, wizard.cfg.repository.url, wizard.cfg.repository.branch,
        )
        jobs = self.wizard_app.prefetcher.region_jobs(region)
        if jobs is not None:
            try:
                wizard.leftover_spike_stacks = jobs.spike_stacks.result(timeout=15)
            except Exception:
                wizard.leftover_spike_stacks = []
            try:
                toolkit = jobs.cdk_toolkit.result(timeout=15)
                wizard.bootstrap_regions = [r for r, present in toolkit.items() if not present]
            except Exception:
                wizard.bootstrap_regions = []
        self.app.call_from_thread(self._render_summary)

    def _render_summary(self) -> None:
        try:
            self.query_one("#summary", Static).update("\n".join(_summary_lines(self.wizard)))
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nav-back":
            self.action_go_back()
            return
        if event.button.id == "nav-next":
            self.wizard_app.action_quit()
            return
        if event.button.id == "deploy":
            self._start_deploy()

    def _log(self, message: str) -> None:
        self.wizard_app.transcript.append(message)
        # ADR: fall back to a direct call | Rationale: _log is shared between the main-thread
        # validation/dry-run path and the @work(thread=True) deploy path; call_from_thread raises
        # if it is ever invoked from the app's own thread
        try:
            self.app.call_from_thread(self._append_log_line, message)
        except RuntimeError:
            self._append_log_line(message)

    def _append_log_line(self, message: str) -> None:
        try:
            self.query_one("#provision-log", RichLog).write(message)
        except Exception:
            pass

    def _start_deploy(self) -> None:
        self.query_one("#deploy", Button).disabled = True
        errors = config_mod.validate(self.wizard.cfg)
        if errors:
            for error in errors:
                self._log("Configuration error: %s" % error)
            self.query_one("#deploy", Button).disabled = False
            return
        config_mod.save(self.wizard.cfg, self.wizard.config_path)
        self._log("Wrote %s" % self.wizard.config_path)

        if self.wizard.dry_run:
            self._run_dry_run()
        else:
            self._run_deploy()

    def _run_dry_run(self) -> None:
        self._log("")
        self._log("Dry run complete: config validation passed and no AWS resources were created.")
        self._log("A real run would create/update:")
        self._log("  - IAM role:          %s" % provision.ROLE_NAME)
        self._log("  - CodeBuild project: %s" % provision.PROJECT_NAME)
        self._log("  - CodeBuild GitHub source credentials (only if a PAT was supplied)")
        self.wizard_app.exit_code = 0
        self.query_one("#nav-next").display = True

    @work(thread=True, group="deploy", exclusive=True)
    def _run_deploy(self) -> None:
        wizard = self.wizard
        try:
            result = provision.deploy(
                session=self.wizard_app.session_factory(),
                region=wizard.cfg.region,
                repo_url=wizard.cfg.repository.url,
                branch=wizard.cfg.repository.branch,
                source_version=wizard.source_version or wizard.cfg.repository.branch,
                config_json=wizard.cfg.to_json(),
                pat_token=wizard.pat_token,
                use_stored_pat=wizard.use_stored_pat,
                log=self._log,
            )
            self.wizard_app.exit_code = 0 if result.status == "SUCCEEDED" else 1
        except Exception as exc:  # noqa: BLE001
            self._log("Deployment failed: %s" % exc)
            self.wizard_app.exit_code = 1
        self.app.call_from_thread(self._deploy_finished)

    def _deploy_finished(self) -> None:
        try:
            self.query_one("#nav-next").display = True
        except Exception:
            pass
