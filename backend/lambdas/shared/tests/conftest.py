import sys
from pathlib import Path

# http_errors ships as a top-level module in the python-deps layer; mirror that on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
