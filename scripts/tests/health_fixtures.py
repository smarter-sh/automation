"""What the tests of the organization health modules share: a repository, a fixed time, and a GitHub failure."""

import subprocess  # nosec B404: only to build a CalledProcessError
from datetime import datetime, timezone

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
"""The time the stale scan runs at, in its tests."""

REPOSITORY = "smarter-sh/example"
"""The repository the fake GitHub answers for."""


def api_error(status: int) -> subprocess.CalledProcessError:
    """
    Build the failure that ``gh api`` raises on an HTTP error.

    :param status: The HTTP status, e.g. 404.
    :returns: The failure, with the status in its standard error as ``gh`` prints it.
    """
    return subprocess.CalledProcessError(1, ["gh"], stderr=f"HTTP {status}: fake")
