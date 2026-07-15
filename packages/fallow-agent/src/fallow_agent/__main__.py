"""``python -m fallow_agent`` entrypoint.

Delegates to the runtime CLI: ``python -m fallow_agent run --config <path>``.
"""

from __future__ import annotations

from fallow_agent.main.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
