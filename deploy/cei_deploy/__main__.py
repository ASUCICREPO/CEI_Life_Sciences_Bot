"""Entry point: `python -m cei_deploy [options]` (invoked by the deploy.sh wrapper)."""
from __future__ import annotations

import argparse
import sys

from . import config as config_mod
from . import provision


def _make_session(region_name=None):
    import boto3  # imported lazily so --help works even without boto3 installed yet

    if region_name:
        return boto3.Session(region_name=region_name)
    return boto3.Session()


def _run_yes_redeploy(repo_root) -> int:
    """`--yes`: redeploy from the saved cei.deploy.json with no TUI."""
    from . import git_info

    path = config_mod.default_config_path(repo_root)
    cfg = config_mod.load(path)
    if cfg is None:
        print("No saved configuration at %s. Run without --yes first." % path, file=sys.stderr)
        return 1

    errors = config_mod.validate(cfg)
    if errors:
        print("Saved configuration at %s is invalid:" % path, file=sys.stderr)
        for error in errors:
            print("  - %s" % error, file=sys.stderr)
        return 1

    def log(message: str) -> None:
        print(message)

    source_version, pinned = git_info.resolve_source_version(repo_root, cfg.repository.url, cfg.repository.branch)
    if not pinned:
        log(
            "Warning: could not resolve a commit SHA for '%s' (local HEAD does not match the remote "
            "branch, and 'git ls-remote' could not reach the repository). The build will track the "
            "branch head instead of a pinned commit." % cfg.repository.branch
        )

    session = _make_session()
    result = provision.deploy(
        session=session,
        region=cfg.region,
        repo_url=cfg.repository.url,
        branch=cfg.repository.branch,
        source_version=source_version,
        config_json=cfg.to_json(),
        pat_token=None,
        use_stored_pat=True,
        log=log,
    )
    return 0 if result.status == "SUCCEEDED" else 1


def _run_dry_run(repo_root) -> int:
    """`--dry-run` with no saved config and no TTY interaction: validate and stop."""
    path = config_mod.default_config_path(repo_root)
    cfg = config_mod.load(path)
    if cfg is None:
        print("No saved configuration at %s to validate. Run the interactive TUI first." % path, file=sys.stderr)
        return 1
    errors = config_mod.validate(cfg)
    if errors:
        print("Configuration at %s is invalid:" % path, file=sys.stderr)
        for error in errors:
            print("  - %s" % error, file=sys.stderr)
        return 1
    print("Dry run complete: config at %s validated. No AWS resources were created." % path)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cei_deploy",
        description=(
            "Guided deployment configurator for the CEI Life Sciences Bot. Collects deployment "
            "configuration, writes backend/cei.deploy.json, then creates/updates the CodeBuild "
            "deployment pipeline and starts a build. All actual build/deploy steps run inside "
            "CodeBuild, never on your machine."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Write/validate configuration only; make no AWS writes.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Redeploy from the saved backend/cei.deploy.json with no interactive TUI.",
    )
    args = parser.parse_args(argv)

    repo_root = config_mod.repo_root_from_here()

    if args.yes:
        return _run_yes_redeploy(repo_root)

    if args.dry_run and not sys.stdin.isatty():
        return _run_dry_run(repo_root)

    from .tui.app import DeployApp

    app = DeployApp(repo_root=repo_root, dry_run=args.dry_run, session_factory=_make_session)
    app.run()
    return app.exit_code if app.exit_code is not None else 1


if __name__ == "__main__":
    sys.exit(main())
