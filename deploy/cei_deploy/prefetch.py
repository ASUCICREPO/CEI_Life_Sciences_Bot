"""Background jobs the TUI starts the moment it can, so screens rarely wait on a cold network call.

Two tiers:
- Region-free jobs start at app launch (`start_global`): AWS caller identity, git remote/branch,
  GitHub repo visibility.
- Region-dependent jobs start only once the user confirms a Region (`start_region`): model
  discovery, the stored GitHub PAT secret, CDKToolkit presence, and leftover spike stacks. Calling
  `start_region` again with a new Region starts a fresh set of Futures; callers simply stop
  awaiting the old ones (threads cannot be cancelled, but nothing ever reads their result).
"""
from __future__ import annotations  # noqa: F404 - must be the first statement

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from . import git_info, models, provision


class CallerIdentity:
    def __init__(self, account_id: str, arn: str, partition: str):
        self.account_id = account_id
        self.arn = arn
        self.partition = partition


def _get_caller_identity(session: Any) -> CallerIdentity:
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    arn = identity["Arn"]
    partition = arn.split(":")[1]
    return CallerIdentity(account_id, arn, partition)


class RegionJobs:
    def __init__(self, region: str, models: "Future[models.DiscoveryResult]", pat_secret_arn: "Future[Optional[str]]",
                 cdk_toolkit: "Future[Dict[str, bool]]", spike_stacks: "Future[list]"):
        self.region = region
        self.models = models
        self.pat_secret_arn = pat_secret_arn
        self.cdk_toolkit = cdk_toolkit
        self.spike_stacks = spike_stacks


class Prefetcher:
    def __init__(self, session_factory, repo_root: Path, max_workers: int = 8):
        self._session_factory = session_factory
        self._repo_root = repo_root
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self.identity_future: Optional["Future[CallerIdentity]"] = None
        self.git_future: Optional["Future[git_info.GitRemoteInfo]"] = None
        self.github_visibility_future: Optional["Future[str]"] = None
        self._region_jobs: Dict[str, RegionJobs] = {}

    # -- region-free --------------------------------------------------------
    def start_global(self) -> None:
        self.identity_future = self._executor.submit(_get_caller_identity, self._session_factory())
        self.git_future = self._executor.submit(git_info.detect, self._repo_root)
        self.github_visibility_future = self._executor.submit(self._check_visibility)

    def _check_visibility(self) -> str:
        info = self.git_future.result() if self.git_future else git_info.GitRemoteInfo()
        return git_info.check_visibility(info.owner, info.repo)

    # -- region-dependent ----------------------------------------------------
    def start_region(self, region: str) -> RegionJobs:
        existing = self._region_jobs.get(region)
        if existing is not None:
            return existing

        def session_for_region():
            return self._session_factory(region_name=region)

        jobs = RegionJobs(
            region=region,
            models=self._executor.submit(self._discover_models, region),
            pat_secret_arn=self._executor.submit(self._describe_pat_secret, region),
            cdk_toolkit=self._executor.submit(self._check_cdk_toolkit, region),
            spike_stacks=self._executor.submit(self._check_spike_stacks, region),
        )
        self._region_jobs[region] = jobs
        return jobs

    def region_jobs(self, region: str) -> Optional[RegionJobs]:
        return self._region_jobs.get(region)

    def _discover_models(self, region: str) -> "models.DiscoveryResult":
        client = self._session_factory().client("bedrock", region_name=region)
        return models.discover(client)

    def _describe_pat_secret(self, region: str) -> Optional[str]:
        client = self._session_factory().client("secretsmanager", region_name=region)
        return provision.describe_github_pat_secret(client)

    def _check_cdk_toolkit(self, region: str) -> Dict[str, bool]:
        session = self._session_factory()
        regions = [region]
        if region != "us-east-1":
            regions.append("us-east-1")
        return {r: provision.check_cdk_toolkit(session.client("cloudformation", region_name=r)) for r in regions}

    def _check_spike_stacks(self, region: str) -> list:
        client = self._session_factory().client("cloudformation", region_name=region)
        return provision.check_leftover_spike_stacks(client)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
