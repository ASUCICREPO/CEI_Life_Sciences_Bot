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

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import git_info, models, provision


class CallerIdentity:
    def __init__(self, account_id: str, arn: str, partition: str):
        self.account_id = account_id
        self.arn = arn
        self.partition = partition


class RegionJobs:
    def __init__(self, region: str, models: "Future[models.DiscoveryResult]", pat_secret_arn: "Future[Optional[str]]",
                 cdk_toolkit: "Future[Dict[str, bool]]", spike_stacks: "Future[list]"):
        self.region = region
        self.models = models
        self.pat_secret_arn = pat_secret_arn
        self.cdk_toolkit = cdk_toolkit
        self.spike_stacks = spike_stacks


_FALLBACK_REGIONS = (
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-central-1",
    "ap-northeast-1", "ap-southeast-2",
)


class Prefetcher:
    """Runs the slow read-only AWS/git calls on background threads.

    ADR: one shared boto3 Session, made on a worker thread, with clients cached per
    (service, Region) | Rationale: every boto3 Session parses botocore's endpoint and service-model
    JSON again. The first version made a new Session per job (up to 6 at once) and one on the UI
    thread, so on AWS CloudShell's small CPU the parsing held the GIL and keystrokes waited for
    seconds | Alternative: a process pool (rejected: more moving parts for a few read-only calls).
    Client creation on one Session is not thread-safe, so it happens under a lock; the clients
    themselves are thread-safe and the AWS calls still run in parallel.
    """

    def __init__(self, session_factory, repo_root: Path, max_workers: int = 4):
        self._session_factory = session_factory
        self._repo_root = repo_root
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._session_lock = threading.Lock()
        self._session: Any = None
        self._clients: Dict[Tuple[str, Optional[str]], Any] = {}
        self.identity_future: Optional["Future[CallerIdentity]"] = None
        self.git_future: Optional["Future[git_info.GitRemoteInfo]"] = None
        self.github_visibility_future: Optional["Future[str]"] = None
        self.regions_future: Optional["Future[List[str]]"] = None
        self._region_jobs: Dict[str, RegionJobs] = {}

    # -- shared session --------------------------------------------------------
    def session(self) -> Any:
        """The one shared boto3 Session (made on first use; never call this on the UI thread)."""
        with self._session_lock:
            if self._session is None:
                self._session = self._session_factory()
            return self._session

    def client(self, service: str, region: Optional[str] = None) -> Any:
        key = (service, region)
        session = self.session()
        with self._session_lock:
            client = self._clients.get(key)
            if client is None:
                client = session.client(service, region_name=region) if region else session.client(service)
                self._clients[key] = client
            return client

    # -- region-free --------------------------------------------------------
    def start_global(self) -> None:
        self.identity_future = self._executor.submit(self._get_identity)
        self.git_future = self._executor.submit(git_info.detect, self._repo_root)
        self.github_visibility_future = self._executor.submit(self._check_visibility)
        self.regions_future = self._executor.submit(self._available_regions)
        self._executor.submit(self._warm_service_models)

    def _warm_service_models(self) -> None:
        # ADR: load botocore's service models while the user is on the Start screen | Rationale: the
        # Region jobs start when the user reaches the Basics screen and starts to type; parsing these
        # JSON models then held the GIL and delayed keystrokes. botocore caches a model per service,
        # not per Region, so a client for any Region warms the cache for the chosen one
        for service in ("bedrock", "secretsmanager", "cloudformation"):
            try:
                self.client(service, "us-east-1")
            except Exception:  # noqa: BLE001 - best effort only
                pass

    def _get_identity(self) -> CallerIdentity:
        identity = self.client("sts").get_caller_identity()
        arn = identity["Arn"]
        return CallerIdentity(identity["Account"], arn, arn.split(":")[1])

    def _available_regions(self) -> List[str]:
        try:
            regions = self.session().get_available_regions("bedrock-agentcore")
            if regions:
                return sorted(regions)
        except Exception:  # noqa: BLE001 - fall back to a short known-good list
            pass
        return list(_FALLBACK_REGIONS)

    def _check_visibility(self) -> str:
        info = self.git_future.result() if self.git_future else git_info.GitRemoteInfo()
        return git_info.check_visibility(info.owner, info.repo)

    # -- region-dependent ----------------------------------------------------
    def start_region(self, region: str) -> RegionJobs:
        existing = self._region_jobs.get(region)
        if existing is not None:
            return existing
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
        return models.discover(self.client("bedrock", region))

    def _describe_pat_secret(self, region: str) -> Optional[str]:
        return provision.describe_github_pat_secret(self.client("secretsmanager", region))

    def _check_cdk_toolkit(self, region: str) -> Dict[str, bool]:
        regions = [region]
        if region != "us-east-1":
            regions.append("us-east-1")
        return {r: provision.check_cdk_toolkit(self.client("cloudformation", r)) for r in regions}

    def _check_spike_stacks(self, region: str) -> list:
        return provision.check_leftover_spike_stacks(self.client("cloudformation", region))

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
