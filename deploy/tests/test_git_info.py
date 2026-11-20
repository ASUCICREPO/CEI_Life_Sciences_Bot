from __future__ import annotations

import subprocess

from cei_deploy import git_info


def test_to_https_github_url_scp_form():
    assert git_info.to_https_github_url("git@github.com:acme/repo.git") == "https://github.com/acme/repo.git"


def test_to_https_github_url_ssh_url_form():
    assert git_info.to_https_github_url("ssh://git@github.com/acme/repo.git") == "https://github.com/acme/repo.git"


def test_to_https_github_url_already_https_is_unchanged():
    url = "https://github.com/acme/repo"
    assert git_info.to_https_github_url(url) == url


def test_to_https_github_url_empty():
    assert git_info.to_https_github_url("") == ""


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)


def test_detect_without_remote_returns_empty_owner_repo(tmp_path):
    _init_repo(tmp_path)
    info = git_info.detect(tmp_path)
    assert info.url == ""
    assert info.owner == ""
    assert info.repo == ""
    assert info.branch == "main"


def test_detect_parses_owner_and_repo_from_remote(tmp_path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/repo.git"], cwd=tmp_path, check=True,
    )
    info = git_info.detect(tmp_path)
    assert info.url == "https://github.com/acme/repo.git"
    assert info.owner == "acme"
    assert info.repo == "repo"


def test_resolve_source_version_pins_sha_when_local_matches_remote(tmp_path):
    _init_repo(tmp_path)
    remote = tmp_path.parent / (tmp_path.name + "-remote.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=tmp_path, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=tmp_path, check=True)

    version, pinned = git_info.resolve_source_version(tmp_path, str(remote), "main")
    assert pinned is True
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert version == head


def test_resolve_source_version_falls_back_to_branch_when_unresolvable(tmp_path):
    _init_repo(tmp_path)
    version, pinned = git_info.resolve_source_version(
        tmp_path, "https://github.com/no-such-org-xyz/no-such-repo-xyz.git", "main",
    )
    assert pinned is False
    assert version == "main"


def test_check_visibility_with_no_owner_repo_is_unknown():
    assert git_info.check_visibility("", "") == "unknown"


def test_git_never_reads_the_terminal(monkeypatch, tmp_path):
    """git gets no stdin and may not prompt, so it can never take the TUI's keystrokes."""
    import subprocess

    from cei_deploy import git_info

    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="abc\n", stderr="")

    monkeypatch.setattr(git_info.subprocess, "run", fake_run)
    assert git_info._run_git(["rev-parse", "HEAD"], cwd=tmp_path) == "abc"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GCM_INTERACTIVE"] == "never"
