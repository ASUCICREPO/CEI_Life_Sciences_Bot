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


def _linked_prefetcher():
    prefetcher = FakePrefetcher()
    sonnet = "anthropic.claude-sonnet-x"
    haiku = "anthropic.claude-haiku-x"
    prefetcher._discovery = models.DiscoveryResult(
        global_items=[
            models.ModelItem("global." + sonnet, "Global Sonnet", sonnet, "Anthropic"),
            models.ModelItem("global." + haiku, "Global Haiku", haiku, "Anthropic"),
        ],
        us_items=[
            models.ModelItem("us." + sonnet, "US Sonnet", sonnet, "Anthropic"),
            models.ModelItem("us." + haiku, "US Haiku", haiku, "Anthropic"),
        ],
        all_backend_ids=[sonnet, haiku],
        default_model_id="global." + haiku,
        router_model_id="global." + haiku,
    )
    return prefetcher


async def _press_until(pilot, key, predicate, limit=15):
    for _ in range(limit):
        if predicate():
            return True
        await pilot.press(key)
        await pilot.pause()
    return predicate()


def _focused_id(app):
    focused = app.screen.focused
    return focused.id if focused is not None else None


@pytest.mark.asyncio
async def test_keyboard_only_flow(tmp_path):
    """Arrow keys, Enter and Space alone take a user from Start to a finished dry run."""
    (tmp_path / "backend").mkdir()
    app = DeployApp(repo_root=tmp_path, dry_run=True, session_factory=_fake_session_factory,
                    prefetcher=_linked_prefetcher())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Start: the Region dropdown has focus; Down must move past it (not open it).
        assert _focused_id(app) == "region-select"
        await pilot.press("down")
        await pilot.pause()
        assert _focused_id(app) == "continue"
        await pilot.press("enter")
        await pilot.pause()

        # Basics: Enter moves from field to field.
        assert app.screen.__class__.__name__ == "BasicsScreen"
        assert _focused_id(app) == "admin-email"
        await pilot.press(*"admin@example.com")
        await pilot.press("enter")
        await pilot.pause()
        assert _focused_id(app) == "email-domains"
        assert await _press_until(pilot, "down", lambda: _focused_id(app) == "nav-back")
        await pilot.press("right")
        await pilot.pause()
        assert _focused_id(app) == "nav-next"
        await pilot.press("enter")
        await pilot.pause()

        # Models: reach the Global list, uncheck the first model with Space, switch to US with Right.
        for _ in range(20):
            if app.wizard.discovery is not None:
                break
            await pilot.pause()
        assert await _press_until(pilot, "tab", lambda: _focused_id(app) == "list-global")
        await pilot.press("space")
        await pilot.pause()
        global_list = app.screen.query_one("#list-global")
        us_list = app.screen.query_one("#list-us")
        assert "global.anthropic.claude-sonnet-x" not in global_list.selected
        await pilot.press("right")
        await pilot.pause()
        assert _focused_id(app) == "list-us"
        assert "us.anthropic.claude-sonnet-x" not in us_list.selected  # linked by base model
        await pilot.press("left")
        await pilot.pause()
        assert _focused_id(app) == "list-global"
        assert await _press_until(pilot, "tab", lambda: _focused_id(app) == "nav-next", limit=25)
        await pilot.press("enter")
        await pilot.pause()

        # Repository -> Review -> Dry run, keyboard only.
        assert app.screen.__class__.__name__ == "RepositoryScreen"
        for _ in range(10):
            await pilot.pause()
        assert await _press_until(pilot, "tab", lambda: _focused_id(app) == "nav-next", limit=25)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ReviewScreen"
        assert await _press_until(pilot, "tab", lambda: _focused_id(app) == "deploy", limit=10)
        await pilot.press("enter")
        for _ in range(40):
            if app.exit_code is not None:
                break
            await pilot.pause()
        # Esc on the first screen must never leave a blank screen.
    assert app.exit_code == 0
    assert app.wizard.cfg.blockedModelIds == ["anthropic.claude-sonnet-x"]


@pytest.mark.asyncio
async def test_saved_config_change_settings_confirms_region(tmp_path):
    """With a saved config, "Change settings" opens the Region picker and Continue starts pre-fetch."""
    from cei_deploy import config as config_mod

    (tmp_path / "backend").mkdir()
    cfg = config_mod.DeployConfig()
    cfg.region = "us-west-2"
    cfg.adminEmail = "admin@example.com"
    cfg.allowedEmailDomains = ["example.com"]
    cfg.defaultModelId = "global.anthropic.claude-sonnet-x"
    cfg.routerModelId = "global.anthropic.claude-sonnet-x"
    config_mod.save(cfg, config_mod.default_config_path(tmp_path))

    prefetcher = FakePrefetcher()
    app = DeployApp(repo_root=tmp_path, dry_run=True, session_factory=_fake_session_factory, prefetcher=prefetcher)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert _focused_id(app) == "redeploy"
        assert not app.screen.query_one("#region-section").display
        await pilot.press("right")
        await pilot.pause()
        assert _focused_id(app) == "change-settings"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#region-section").display
        assert _focused_id(app) == "region-select"
        await pilot.press("escape")  # Esc on step 1 must not pop to a blank screen
        await pilot.pause()
        assert app.screen.__class__.__name__ == "StartScreen"
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "BasicsScreen"
        assert prefetcher.region_jobs("us-west-2") is not None


@pytest.mark.asyncio
async def test_arrows_space_enter_only(tmp_path):
    """The whole flow works with only arrow keys, Space and Enter (no Tab, no mouse)."""
    from textual.widgets import Collapsible

    (tmp_path / "backend").mkdir()
    app = DeployApp(repo_root=tmp_path, dry_run=True, session_factory=_fake_session_factory,
                    prefetcher=_linked_prefetcher())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Start: Right moves from the dropdown to Continue; Space presses it.
        await pilot.press("right")
        await pilot.pause()
        assert _focused_id(app) == "continue"
        await pilot.press("space")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "BasicsScreen"

        await pilot.press(*"admin@example.com")
        assert await _press_until(pilot, "down", lambda: _focused_id(app) == "nav-next")
        await pilot.press("space")
        await pilot.pause()

        # Models: Down reaches the list; Enter also toggles a model.
        for _ in range(20):
            if app.wizard.discovery is not None:
                break
            await pilot.pause()
        assert app.screen.__class__.__name__ == "ModelsScreen"
        assert await _press_until(pilot, "down", lambda: _focused_id(app) == "list-global", limit=6)
        await pilot.press("enter")
        await pilot.pause()
        assert "global.anthropic.claude-sonnet-x" not in app.screen.query_one("#list-global").selected
        await pilot.press("enter")
        await pilot.pause()
        assert "global.anthropic.claude-sonnet-x" in app.screen.query_one("#list-global").selected
        # Down past the last row leaves the list, then Down keeps moving to the footer.
        assert await _press_until(pilot, "down", lambda: _focused_id(app) == "select-all", limit=5)
        await pilot.press("up")
        await pilot.pause()
        assert _focused_id(app) == "list-global"  # Up from the next control returns to the list
        assert await _press_until(pilot, "down", lambda: _focused_id(app) == "nav-next", limit=15)
        await pilot.press("enter")
        await pilot.pause()

        # Repository: Space opens the Advanced section.
        assert app.screen.__class__.__name__ == "RepositoryScreen"
        for _ in range(10):
            await pilot.pause()
        advanced = app.screen.query_one("#advanced", Collapsible)
        assert await _press_until(
            pilot, "down", lambda: type(app.screen.focused).__name__ == "CollapsibleTitle", limit=10,
        )
        await pilot.press("space")
        await pilot.pause()
        assert advanced.collapsed is False
        assert await _press_until(pilot, "down", lambda: _focused_id(app) == "nav-next", limit=15)
        await pilot.press("space")
        await pilot.pause()

        # Review: Left/Right reach the Dry run button; Space runs it.
        assert app.screen.__class__.__name__ == "ReviewScreen"
        assert await _press_until(pilot, "right", lambda: _focused_id(app) == "deploy", limit=6)
        await pilot.press("space")
        for _ in range(40):
            if app.exit_code is not None:
                break
            await pilot.pause()
    assert app.exit_code == 0


@pytest.mark.asyncio
async def test_model_search_does_not_rebuild_dropdowns_per_key(tmp_path):
    """Typing a search term refreshes the default/router dropdowns at most once, not per option."""
    from cei_deploy.tui.screens.models import ModelsScreen

    (tmp_path / "backend").mkdir()
    app = DeployApp(repo_root=tmp_path, dry_run=True, session_factory=_fake_session_factory,
                    prefetcher=_linked_prefetcher())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.wizard.cfg.region = "us-east-1"
        app.wizard.region_confirmed = True
        app.prefetcher.start_region("us-east-1")
        app.push_screen(ModelsScreen())
        for _ in range(20):
            if app.wizard.discovery is not None:
                break
            await pilot.pause()
        await pilot.pause()
        screen = app.screen
        calls = {"refresh": 0, "populate": 0}
        real_refresh, real_populate = screen._refresh_default_router_selects, screen._populate

        def counting_refresh():
            calls["refresh"] += 1
            real_refresh()

        def counting_populate(text):
            calls["populate"] += 1
            real_populate(text)

        screen._refresh_default_router_selects = counting_refresh
        screen._populate = counting_populate
        screen.query_one("#model-search").focus()
        await pilot.press(*"sonnet")
        await pilot.pause(0.3)
        assert calls["populate"] == 1  # debounced: one filter run for six keystrokes
        assert calls["refresh"] == 0   # re-adding options is not a selection change
        assert [o.prompt for o in screen.query_one("#list-global")._options]  # filter applied
        assert all("Sonnet" in str(o.prompt) for o in screen.query_one("#list-global")._options)
