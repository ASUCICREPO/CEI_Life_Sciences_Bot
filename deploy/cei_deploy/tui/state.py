"""Shared, mutable wizard state the TUI screens read and write on `self.app.wizard`."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from ..config import DeployConfig
from ..models import DiscoveryResult


@dataclass
class WizardState:
    cfg: DeployConfig
    config_path: Path
    repo_root: Path
    dry_run: bool = False

    # Repository / PAT
    pat_token: Optional[str] = None
    use_stored_pat: bool = False
    stored_pat_exists: bool = False
    repo_visibility: str = "unknown"  # "public" | "private" | "unknown"

    # Models
    discovery: Optional[DiscoveryResult] = None
    discovery_error: Optional[str] = None
    checked_base_ids: Set[str] = field(default_factory=set)
    manual_models: bool = False

    # AWS identity
    account_id: str = ""
    caller_arn: str = ""
    partition: str = ""

    # Review
    bootstrap_regions: List[str] = field(default_factory=list)
    leftover_spike_stacks: List[str] = field(default_factory=list)
    source_version: str = ""
    source_pinned: bool = False

    # Basics screen tracks whether the user has hand-edited the email-domain field
    email_domain_auto: bool = True

    region_confirmed: bool = False
