"""End-to-end Pilot smoke test: Start -> Basics -> Models -> Repository -> Review, dry run, no network."""
from __future__ import annotations

from concurrent.futures import Future

import pytest

from cei_deploy import git_info, models
from cei_deploy.prefetch import CallerIdentity, RegionJobs
from cei_deploy.tui.app import DeployApp


def _done_future(value):
    future = Future()
    future.set_result(value)
    return future


class FakePrefetcher:
    def __init__(self):
        self.identity_future = _done_future(CallerIdentity("123456789012", "arn:aws:iam::123456789012:user/me", "aws"))
        self.git_future = _done_future(
            git_info.GitRemoteInfo(url="https://github.com/acme/repo", owner="acme", repo="repo", branch="main")
        )
        self.github_visibility_future = _done_future("public")
        self._discovery = models.DiscoveryResult(
            global_items=[models.ModelItem("global.anthropic.claude-sonnet-x", "Sonnet", "anthropic.claude-sonnet-x", "Anthropic")],
            us_items=[],
            all_backend_ids=["anthropic.claude-sonnet-x"],
            default_model_id="global.anthropic.claude-sonnet-x",
            router_model_id="global.anthropic.claude-sonnet-x",
        )
        self._region_jobs = {}

    def start_global(self):
        return None

    def start_region(self, region):
        if region not in self._region_jobs:
            self._region_jobs[region] = RegionJobs(
                region=region,
                models=_done_future(self._discovery),
                pat_secret_arn=_done_future(None),
                cdk_toolkit=_done_future({region: True}),
                spike_stacks=_done_future([]),
            )
        return self._region_jobs[region]

    def region_jobs(self, region):
        return self._region_jobs.get(region)

    def shutdown(self):
        return None


def _fake_session_factory(region_name=None):
    raise AssertionError("no real AWS session should be created in this smoke test")


@pytest.mark.asyncio
async def test_full_wizard_flow_dry_run(tmp_path):
    repo_root = tmp_path
    (repo_root / "backend").mkdir()

    app = DeployApp(
        repo_root=repo_root, dry_run=True, session_factory=_fake_session_factory, prefetcher=FakePrefetcher(),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        # Start screen: Region select is pre-filled; confirm it.
        await pilot.click("#continue")
        await pilot.pause()

        # Basics screen
        admin_email = app.screen.query_one("#admin-email")
        admin_email.value = "admin@example.com"
        await pilot.pause()
        domains = app.screen.query_one("#email-domains")
        domains.value = "example.com"
        await pilot.click("#nav-next")
        await pilot.pause()

        # Models screen: wait for the fake discovery future to be consumed.
        for _ in range(20):
            if app.wizard.discovery is not None:
                break
            await pilot.pause()
        assert app.wizard.discovery is not None
        await pilot.click("#nav-next")
        await pilot.pause()

        # Repository screen: URL/branch are pre-filled from the fake git info.
        for _ in range(20):
            if app.wizard.repo_visibility != "unknown" or app.screen.query_one("#repo-url").value:
                break
            await pilot.pause()
        await pilot.click("#nav-next")
        await pilot.pause()

        # Review screen: deploy (dry run)
        assert app.screen.__class__.__name__ == "ReviewScreen"
        await pilot.click("#deploy")
        for _ in range(40):
            if app.exit_code is not None:
                break
            await pilot.pause()

    assert app.exit_code == 0
    assert app.wizard.cfg.adminEmail == "admin@example.com"
    assert app.wizard.cfg.allowedEmailDomains == ["example.com"]
    assert app.wizard.cfg.defaultModelId == "global.anthropic.claude-sonnet-x"
    assert (repo_root / "backend" / "cei.deploy.json").is_file()


@pytest.mark.asyncio
async def test_private_repo_without_token_blocks_real_run(tmp_path):
    (tmp_path / "backend").mkdir()
    prefetcher = FakePrefetcher()
    prefetcher.github_visibility_future = _done_future("private")
    app = DeployApp(repo_root=tmp_path, dry_run=False, session_factory=_fake_session_factory, prefetcher=prefetcher)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        await pilot.click("#continue")
        await pilot.pause()
        app.screen.query_one("#admin-email").value = "admin@example.com"
        app.screen.query_one("#email-domains").value = "example.com"
        await pilot.click("#nav-next")
        for _ in range(20):
            if app.wizard.discovery is not None:
                break
            await pilot.pause()
        await pilot.click("#nav-next")
        for _ in range(20):
            await pilot.pause()
            if app.wizard.repo_visibility == "private":
                break
        await pilot.click("#nav-next")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "RepositoryScreen"
        app.screen.query_one("#pat-token").value = "ghp_test_token"
        app.screen._on_next()
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ReviewScreen"
        assert app.wizard.pat_token == "ghp_test_token"
