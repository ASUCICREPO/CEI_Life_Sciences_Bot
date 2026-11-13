import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
# backfill_chat_metadata is a standalone script; it imports `shared.persistence` from backend/agents itself,
# but insert it here too so import order does not matter.
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "agents"))
