"""Entry-point wiring: resource lifecycle around command dispatch.

The transport backend is auto-detected and session-based backends
(requests/httpx/urllib3) pool connections, so main() must drive the
client through its context manager. These tests run main() end to end
with a spy client to pin that contract.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from contree_client.profiles import Profile

from contree_cli.__main__ import main
from contree_cli.config import Config


class DummyChecker:
    """UpdateChecker stand-in: no PyPI traffic, always up to date."""

    def refresh(self) -> None:
        pass

    def is_latest(self) -> bool:
        return True


class SpyClient:
    """Records context-manager transitions instead of doing HTTP."""

    def __init__(self, events: list[str], raises: BaseException | None = None) -> None:
        self.events = events
        self.raises = raises

    def __enter__(self) -> SpyClient:
        self.events.append("enter")
        return self

    def __exit__(self, *exc: object) -> None:
        self.events.append("exit")

    def iter_images(self, **kwargs: object) -> Iterator[object]:
        if self.raises is not None:
            raise self.raises
        return iter(())


def write_profile(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "auth.ini"
    cfg = Config(cfg_path)
    cfg["default"] = Profile(
        name="default",
        url="https://contree.dev",
        token="tok",
    )
    return cfg_path


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    cfg_path: Path,
    events: list[str],
    command: str,
    raises: BaseException | None = None,
) -> pytest.ExceptionInfo[SystemExit]:
    monkeypatch.setattr("contree_cli.__main__.UpdateChecker", DummyChecker)
    monkeypatch.setattr(
        "contree_cli.__main__.client_from_profile",
        lambda profile, timeout=300.0: SpyClient(events, raises=raises),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["contree", "--config", str(cfg_path), command],
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    return exc_info


class TestNetworkErrorHandling:
    """A transport-layer failure must print a friendly message and exit
    1, not an unhandled traceback -- regardless of which HTTP backend
    raised it. `images` (bare, list) calls `client.iter_images(...)`
    directly, so SpyClient's `raises` fires from inside the real
    try/except in main(), unlike patching a handler function (whose
    reference argparse already captured at parser-build time)."""

    def test_urllib3_error_caught(self, tmp_path, monkeypatch, capsys):
        urllib3 = pytest.importorskip("urllib3")
        cfg_path = write_profile(tmp_path)
        events: list[str] = []

        exc_info = run_main(
            monkeypatch,
            cfg_path,
            events,
            "images",
            raises=urllib3.exceptions.HTTPError("boom"),
        )

        assert exc_info.value.code == 1
        assert "Network error" in capsys.readouterr().err

    def test_httpx_error_caught(self, tmp_path, monkeypatch, capsys):
        httpx = pytest.importorskip("httpx")
        cfg_path = write_profile(tmp_path)
        events: list[str] = []

        exc_info = run_main(
            monkeypatch,
            cfg_path,
            events,
            "images",
            raises=httpx.HTTPError("boom"),
        )

        assert exc_info.value.code == 1
        assert "Network error" in capsys.readouterr().err


class TestClientLifecycle:
    def test_client_entered_and_exited(self, tmp_path, monkeypatch, capsys):
        """main() drives the client through __enter__/__exit__ so
        session-based transports release pooled connections."""
        cfg_path = write_profile(tmp_path)
        events: list[str] = []

        # `session` without an active session prints an error and
        # exits 1 without touching the API: the lifecycle is observed
        # without mocking any API method.
        run_main(monkeypatch, cfg_path, events, "session")

        assert events == ["enter", "exit"]

    def test_local_command_creates_no_client(self, tmp_path, monkeypatch, capsys):
        cfg_path = write_profile(tmp_path)
        events: list[str] = []

        run_main(monkeypatch, cfg_path, events, "agent")

        assert events == []


class TestCliSettingsRouting:
    """A ``[cli]`` section must not redirect where the saved token goes."""

    def test_cli_ini_url_and_token_ignored(self, tmp_path, monkeypatch, capsys):
        import configparser

        cfg_path = write_profile(tmp_path)
        settings = configparser.ConfigParser()
        settings.read_string(
            "[cli]\nurl = https://evil.example\ntoken = evil\nproject = p\n"
        )
        monkeypatch.setattr("contree_cli.__main__.SETTINGS", settings)
        seen: list[Profile] = []
        monkeypatch.setattr("contree_cli.__main__.UpdateChecker", DummyChecker)

        def fake_client(profile: Profile, timeout: float = 300.0) -> SpyClient:
            seen.append(profile)
            return SpyClient([])

        monkeypatch.setattr("contree_cli.__main__.client_from_profile", fake_client)
        monkeypatch.setattr(
            sys, "argv", ["contree", "--config", str(cfg_path), "images"]
        )
        try:
            with pytest.raises(SystemExit):
                main()
        finally:
            from contree_cli.arguments import parser

            parser.set_defaults(url=None, token=None, project=None)
        assert seen[0].url == "https://contree.dev"
        assert seen[0].token == "tok"
        assert "Ignoring unsupported [cli] keys" in capsys.readouterr().err

    def test_untrusted_home_exits(self, tmp_path, monkeypatch, capsys):
        import contree_cli.config as config_mod

        cfg_path = write_profile(tmp_path)
        monkeypatch.setattr(
            config_mod,
            "HOME_ERROR",
            config_mod.InsecurePathError("/tmp/x is owned by uid 65534"),
        )
        exc = run_main(monkeypatch, cfg_path, [], "images")
        assert exc.value.code == 1
        assert "uid 65534" in capsys.readouterr().err
