"""Load only known, source-bound profiles; never treat caller input as a path."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from shared.tools.data_sources import TOOLS_BY_SOURCE_ID

SOURCE_IDS = frozenset(TOOLS_BY_SOURCE_ID) | {"ncbi"}
_PROFILES = Path(__file__).parent / "profiles"


@dataclass(frozen=True)
class Profile:
    source_id: str
    scope: str
    name: str
    instruction: str


@lru_cache(maxsize=len(SOURCE_IDS))
def load_profile(source_id: str) -> Profile:
    if not isinstance(source_id, str) or source_id not in SOURCE_IDS:
        raise ValueError("unknown source")
    try:
        data = yaml.safe_load((_PROFILES / f"{source_id}.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("source profile unavailable") from exc
    if not isinstance(data, dict) or set(data) != {"source_id", "name", "instruction"}:
        raise ValueError("invalid source profile")
    if data["source_id"] != source_id or not all(isinstance(data[key], str) and 0 < len(data[key]) <= 1000 for key in ("name", "instruction")):
        raise ValueError("invalid source profile")
    return Profile(source_id, f"cei-tools/src.{source_id}", data["name"], data["instruction"])
