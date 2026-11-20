"""Git remote/branch detection and commit-SHA pinning, ported from deploy.sh.

Mirrors deploy.sh's `to_https_github_url` and `resolve_source_version` exactly, and adds an
unauthenticated GitHub repo-visibility check used by the Repository screen's prefetch.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

_SSH_SCP_FORM = re.compile(r"^git@github\.com:(?P<path>.+)$")
_SSH_URL_FORM = re.compile(r"^ssh://git@github\.com/(?P<path>.+)$")
_HTTPS_OWNER_REPO = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


def to_https_github_url(url: str) -> str:
    """Normalize an SSH-form "origin" remote to https://, exactly as deploy.sh's helper does."""
    if not url:
        return url
    match = _SSH_SCP_FORM.match(url)
    if match:
        return "https://github.com/" + match.group("path")
    match = _SSH_URL_FORM.match(url)
    if match:
        return "https://github.com/" + match.group("path")
    return url


def _run_git(args, cwd: Optional[Path] = None, timeout: float = 10.0, env=None) -> str:
    # ADR: git never touches the terminal | Rationale: git ran with the TUI's stdin and could prompt
    # for GitHub credentials on the terminal (a private repo over https); that prompt read the user's
    # keystrokes, so keys typed into the TUI went missing | Alternative: none
    run_env = dict(os.environ if env is None else env)
    run_env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout,
            env=run_env, stdin=subprocess.DEVNULL,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


@dataclass
class GitRemoteInfo:
    url: str = ""
    owner: str = ""
    repo: str = ""
    branch: str = ""


def detect(repo_root: Path) -> GitRemoteInfo:
    remote = _run_git(["remote", "get-url", "origin"], cwd=repo_root)
    url = to_https_github_url(remote)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root) or "main"
    owner = repo = ""
    match = _HTTPS_OWNER_REPO.match(url)
    if match:
        owner, repo = match.group("owner"), match.group("repo")
    return GitRemoteInfo(url=url, owner=owner, repo=repo, branch=branch)


def check_visibility(owner: str, repo: str, timeout: float = 5.0) -> str:
    """"public", "private" (404) or "unknown" (any network/other error)."""
    if not owner or not repo:
        return "unknown"
    request = urllib.request.Request(
        "https://api.github.com/repos/%s/%s" % (owner, repo),
        headers={"User-Agent": "cei-deploy-tui", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed github.com host
            return "public" if response.status == 200 else "unknown"
    except urllib.error.HTTPError as exc:
        return "private" if exc.code == 404 else "unknown"
    except Exception:
        return "unknown"


def resolve_source_version(repo_root: Path, repo_url: str, branch: str) -> Tuple[str, bool]:
    """Port of deploy.sh `resolve_source_version`. Returns (version, pinned_to_a_sha)."""
    local_head = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    sha = ""
    if local_head:
        remote_head_line = _run_git(["ls-remote", "origin", "refs/heads/%s" % branch], cwd=repo_root)
        remote_head = remote_head_line.split("\t", 1)[0] if remote_head_line else ""
        if remote_head == local_head:
            sha = local_head
    if not sha:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        remote_sha_line = _run_git(
            ["ls-remote", repo_url, "refs/heads/%s" % branch], cwd=repo_root, timeout=15.0, env=env,
        )
        remote_sha = remote_sha_line.split("\t", 1)[0] if remote_sha_line else ""
        if remote_sha:
            sha = remote_sha
    if sha:
        return sha, True
    return branch, False
