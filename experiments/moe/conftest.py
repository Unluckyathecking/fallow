"""Put this experiment dir on sys.path so the smoke test can import the bench
modules by name without the tree being a package. Only loaded when pytest is
pointed at experiments/moe; the default `pytest` run (testpaths = packages,
tests) never descends here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
