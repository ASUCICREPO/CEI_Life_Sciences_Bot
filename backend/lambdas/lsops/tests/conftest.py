import sys
from pathlib import Path

# The Lambda modules (handler, chemistry, ...) sit one level up; CI runs this suite from backend/agents.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
