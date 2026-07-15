"""User-facing CLI errors.

`CliError` carries a friendly, already-formatted message plus the process exit
code to use. Commands catch it at the boundary, print the message to stderr, and
exit — no tracebacks ever reach the user for an expected failure.
"""

from __future__ import annotations

# Exit codes (documented, stable): 1 = generic failure, 2 = auth / config.
EXIT_GENERIC = 1
EXIT_AUTH = 2


class CliError(Exception):
    """An expected, user-facing failure with a friendly message and exit code."""

    def __init__(self, message: str, *, exit_code: int = EXIT_GENERIC) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
