"""`backend/cei.deploy.json` load/save and validation.

Mirrors `backend/lib/config/deploy-config.ts` field-for-field: the output JSON schema must stay
identical to what that file parses. Keeping the two in sync is manual (TypeScript and Python
cannot share one source file here); `deploy/tests/test_config.py` exercises every regex with the
same sample values the TS validator's own tests use.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ADR: regexes copied character-for-character from deploy-config.ts | Rationale: the CDK app is the
# single source of truth for what a valid config looks like; this tool must reject nothing the TS
# validator would accept, and accept nothing it would reject
REGION_PATTERN = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-[0-9]$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.IGNORECASE)
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
REPOSITORY_URL_PATTERN = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$")
REPOSITORY_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

TOOL_POLICY_MODES = ("LOG_ONLY", "ENFORCE")
DEFAULT_TOOL_POLICY_MODE = "ENFORCE"

CONFIG_FILE_NAME = "cei.deploy.json"

# ADR: B22 defaults | Rationale: matches deploy-config.ts DEFAULT_LIMITS (20 rps / 2 real-time / 1 background)
DEFAULT_REQUESTS_PER_SECOND = 20
DEFAULT_MAX_REALTIME_RUNS_PER_USER = 2
DEFAULT_MAX_BACKGROUND_RUNS_PER_USER = 1


@dataclass
class DeployLimits:
    requestsPerSecond: int = DEFAULT_REQUESTS_PER_SECOND
    maxRealtimeRunsPerUser: int = DEFAULT_MAX_REALTIME_RUNS_PER_USER
    maxBackgroundRunsPerUser: int = DEFAULT_MAX_BACKGROUND_RUNS_PER_USER

    def to_dict(self) -> Dict[str, int]:
        return {
            "requestsPerSecond": self.requestsPerSecond,
            "maxRealtimeRunsPerUser": self.maxRealtimeRunsPerUser,
            "maxBackgroundRunsPerUser": self.maxBackgroundRunsPerUser,
        }


@dataclass
class Repository:
    url: str = ""
    branch: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"url": self.url, "branch": self.branch}


@dataclass
class DeployConfig:
    region: str = ""
    defaultModelId: str = ""
    routerModelId: str = ""
    blockedModelIds: List[str] = field(default_factory=list)
    allowedEmailDomains: List[str] = field(default_factory=list)
    adminEmail: str = ""
    limits: DeployLimits = field(default_factory=DeployLimits)
    webSearchEnabled: bool = False
    allowLocalhostCallback: bool = False
    toolPolicyMode: str = DEFAULT_TOOL_POLICY_MODE
    repository: Repository = field(default_factory=Repository)
    # ADR: unknown top-level keys (e.g. accountDeletionEnabled) round-trip untouched | Rationale: a
    # separate tool/phase may own fields this TUI never asks about; re-saving must not drop them
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = dict(self.extra)
        out.update({
            "region": self.region,
            "defaultModelId": self.defaultModelId,
            "routerModelId": self.routerModelId,
            "blockedModelIds": list(self.blockedModelIds),
            "allowedEmailDomains": list(self.allowedEmailDomains),
            "adminEmail": self.adminEmail,
            "limits": self.limits.to_dict(),
            "webSearchEnabled": self.webSearchEnabled,
            "allowLocalhostCallback": self.allowLocalhostCallback,
            "toolPolicyMode": self.toolPolicyMode,
            "repository": self.repository.to_dict(),
        })
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"


_KNOWN_TOP_LEVEL_KEYS = (
    "region", "defaultModelId", "routerModelId", "blockedModelIds", "allowedEmailDomains",
    "adminEmail", "limits", "webSearchEnabled", "allowLocalhostCallback", "toolPolicyMode",
    "repository",
)


def from_dict(raw: Dict[str, Any]) -> DeployConfig:
    raw = dict(raw or {})
    limits_raw = raw.get("limits") or {}
    repo_raw = raw.get("repository") or {}
    cfg = DeployConfig(
        region=str(raw.get("region") or ""),
        defaultModelId=str(raw.get("defaultModelId") or ""),
        routerModelId=str(raw.get("routerModelId") or ""),
        blockedModelIds=list(raw.get("blockedModelIds") or []),
        allowedEmailDomains=list(raw.get("allowedEmailDomains") or []),
        adminEmail=str(raw.get("adminEmail") or ""),
        limits=DeployLimits(
            requestsPerSecond=int(limits_raw.get("requestsPerSecond", DEFAULT_REQUESTS_PER_SECOND)),
            maxRealtimeRunsPerUser=int(
                limits_raw.get("maxRealtimeRunsPerUser", DEFAULT_MAX_REALTIME_RUNS_PER_USER)
            ),
            maxBackgroundRunsPerUser=int(
                limits_raw.get("maxBackgroundRunsPerUser", DEFAULT_MAX_BACKGROUND_RUNS_PER_USER)
            ),
        ),
        webSearchEnabled=bool(raw.get("webSearchEnabled", False)),
        allowLocalhostCallback=bool(raw.get("allowLocalhostCallback", False)),
        toolPolicyMode=str(raw.get("toolPolicyMode") or DEFAULT_TOOL_POLICY_MODE),
        repository=Repository(url=str(repo_raw.get("url") or ""), branch=str(repo_raw.get("branch") or "")),
        extra={k: v for k, v in raw.items() if k not in _KNOWN_TOP_LEVEL_KEYS},
    )
    return cfg


def validate(cfg: DeployConfig) -> List[str]:
    """Same checks as deploy-config.ts `parseDeployConfig`. Returns every error, empty if valid."""
    errors: List[str] = []

    if not cfg.region:
        errors.append("region: is required")
    elif not REGION_PATTERN.match(cfg.region):
        errors.append('region: "%s" is not a valid AWS Region code' % cfg.region)

    if not cfg.defaultModelId:
        errors.append("defaultModelId: is required")
    elif not MODEL_ID_PATTERN.match(cfg.defaultModelId):
        errors.append("defaultModelId: does not match the expected model ID format")

    if not cfg.routerModelId:
        errors.append("routerModelId: is required")
    elif not MODEL_ID_PATTERN.match(cfg.routerModelId):
        errors.append("routerModelId: does not match the expected model ID format")

    for model_id in cfg.blockedModelIds:
        if not model_id or not model_id.strip():
            errors.append("blockedModelIds: entries must not be empty")
        elif not MODEL_ID_PATTERN.match(model_id):
            errors.append('blockedModelIds: "%s" does not match the expected model ID format' % model_id)

    if cfg.defaultModelId and cfg.defaultModelId in cfg.blockedModelIds:
        errors.append('defaultModelId: "%s" is also in blockedModelIds' % cfg.defaultModelId)
    if cfg.routerModelId and cfg.routerModelId in cfg.blockedModelIds:
        errors.append('routerModelId: "%s" is also in blockedModelIds' % cfg.routerModelId)

    if not cfg.allowedEmailDomains:
        errors.append("allowedEmailDomains: must be a non-empty array of strings")
    else:
        for domain in cfg.allowedEmailDomains:
            if not domain or not domain.strip():
                errors.append("allowedEmailDomains: entries must not be empty")
            elif not DOMAIN_PATTERN.match(domain):
                errors.append('allowedEmailDomains: "%s" is not a valid domain' % domain)

    if not cfg.adminEmail:
        errors.append("adminEmail: is required")
    elif not EMAIL_PATTERN.match(cfg.adminEmail):
        errors.append('adminEmail: "%s" is not a valid email address' % cfg.adminEmail)

    for field_name in ("requestsPerSecond", "maxRealtimeRunsPerUser", "maxBackgroundRunsPerUser"):
        value = getattr(cfg.limits, field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append("limits.%s: must be a positive integer" % field_name)

    if cfg.toolPolicyMode not in TOOL_POLICY_MODES:
        errors.append("toolPolicyMode: must be one of %s" % ", ".join(TOOL_POLICY_MODES))

    if not cfg.repository.url or not REPOSITORY_URL_PATTERN.match(cfg.repository.url):
        errors.append("repository.url: must be https://github.com/<owner>/<repo>[.git] with no credentials")

    branch = cfg.repository.branch
    if not branch or not REPOSITORY_BRANCH_PATTERN.match(branch) or ".." in branch:
        errors.append('repository.branch: must match ^[A-Za-z0-9._/-]{1,255}$ and must not contain ".."')

    return errors


def default_config_path(repo_root: Path) -> Path:
    return repo_root / "backend" / CONFIG_FILE_NAME


def repo_root_from_here() -> Path:
    # deploy/cei_deploy/config.py -> deploy/ -> repo root
    return Path(__file__).resolve().parents[2]


def load(path: Path) -> Optional[DeployConfig]:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return from_dict(raw)


def save(cfg: DeployConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfg.to_json(), encoding="utf-8", newline="\n")
