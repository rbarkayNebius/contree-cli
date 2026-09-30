"""PyPI update check, rate-limited to once per day.

State file at ``$CONTREE_HOME/cli/version_check.json``::

    {
      "last_check": 1762555200,
      "latest_version": "0.5.0"
    }

``last_check`` is a Unix epoch timestamp; storing seconds keeps the
freshness check trivial (one subtraction) and immune to timezone /
ISO-format quirks.

Network errors, malformed cache files, and parse failures are swallowed:
the update check must never break a user's command.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from contree_cli import config
from contree_cli.client import CLI_USER_AGENT, cli_version


@dataclass(frozen=True)
class UpdateState:
    last_check: int = 0
    latest_version: str = ""

    @classmethod
    def from_file(cls, path: Path) -> UpdateState:
        try:
            config.check_file(path)
            with path.open() as f:
                data = json.load(f)
            return cls(
                last_check=int(data["last_check"]),
                latest_version=str(data["latest_version"]),
            )
        except Exception:
            return cls()

    def to_file(self, path: Path) -> None:
        with suppress(OSError, config.InsecurePathError):
            config.ensure_private_dir(path.parent)
            config.write_private(path, json.dumps(asdict(self), indent=1))


class UpdateChecker:
    PYPI_URL = "https://pypi.org/pypi/contree-cli/json"
    CHECK_INTERVAL = timedelta(days=1)
    NETWORK_TIMEOUT = 2.0
    OPT_OUT_ENV = "CONTREE_NO_UPDATE_CHECK"
    STATE_PATH = config.CONTREE_HOME / "cli" / "version_check.json"
    # Capture leading digits of each dot-separated component; anything
    # past the digits (``a1``, ``-rc.1``, etc.) marks a pre-release.
    COMPONENT_REGEX = re.compile(r"\d+")

    def __init__(
        self,
        *,
        state_path: Path = STATE_PATH,
        current_version: str = cli_version(),
    ) -> None:
        self.state_path = state_path
        self.current_version = current_version
        # ``state`` holds whatever we know about PyPI's latest version.
        # Default sentinel ("", last_check=0) means "no cache yet" —
        # is_latest() treats it as up-to-date so callers don't warn.
        self.state: UpdateState = UpdateState()

    def parse_version(self, value: str) -> tuple[tuple[int, int], ...]:
        """Parse ``value`` into a sortable tuple of ``(number, rank)``.

        ``rank`` is ``1`` for a clean numeric component and ``0`` for a
        pre-release suffix (``a1``, ``-rc.1``, …). With this encoding,
        ``0.4.2a1`` < ``0.4.2`` < ``0.4.21`` as expected. Components with
        no digits at all are dropped.
        """
        parts: list[tuple[int, int]] = []
        for raw in value.split("."):
            match = self.COMPONENT_REGEX.search(raw)
            if not match:
                continue
            tail = raw[match.end() :]
            parts.append((int(match.group()), 0 if tail else 1))
        return tuple(parts)

    def fetch_latest_version(self) -> str | None:
        try:
            request = urllib.request.Request(
                self.PYPI_URL,
                headers={
                    "User-Agent": CLI_USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(  # nosemgrep
                request, timeout=self.NETWORK_TIMEOUT
            ) as resp:
                payload = json.loads(resp.read())
        except Exception:
            return None
        info = payload.get("info") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            return None
        version = info.get("version")
        return version if isinstance(version, str) else None

    @property
    def enabled(self) -> bool:
        return self.OPT_OUT_ENV not in os.environ and self.current_version != "editable"

    def is_cache_fresh(self, state: UpdateState) -> bool:
        """True if ``state.last_check`` is within ``CHECK_INTERVAL``."""
        return time.time() - state.last_check < self.CHECK_INTERVAL.total_seconds()

    def refresh(self) -> None:
        """Load the cache, refetch from PyPI if stale, persist new state.

        Populates ``self.state`` with whatever we know after this call.
        :meth:`is_latest` then decides whether to warn based purely on
        in-memory state — no further file IO.
        """
        if not self.enabled:
            return

        self.state = UpdateState.from_file(self.state_path)
        if self.is_cache_fresh(self.state):
            return

        latest = self.fetch_latest_version()
        if latest is None:
            # Network failed; keep whatever was cached.
            return

        self.state = UpdateState(
            last_check=int(time.time()),
            latest_version=latest,
        )
        self.state.to_file(self.state_path)

    def is_latest(self) -> bool:
        """Return True if the installed version is at or ahead of the cached
        ``latest_version``.

        Returns True when checks are disabled or the cached
        ``latest_version`` is the empty sentinel — callers default to
        "no warning" in those cases. Pure decision based on in-memory
        state populated by :meth:`refresh`; never touches the network
        or filesystem.
        """
        if not self.enabled or not self.state.latest_version:
            return True
        return self.parse_version(self.current_version) >= self.parse_version(
            self.state.latest_version,
        )
