"""Unit contracts for the external-service helpers; no running lab required."""
from pathlib import Path
import sys

# Support recursive unittest discovery and the helpers' existing script imports.
HERE = Path(__file__).resolve().parent
for directory in (HERE.parent, HERE):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
