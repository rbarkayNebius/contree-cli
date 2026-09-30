import contextvars
import http.client
import logging
import sys
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import replace

from contree_client.exceptions import ContreeError

import contree_cli.config as config_mod
from contree_cli import CLIENT, FORMATTER, PROFILE, SESSION_STORE, ArgumentsProtocol
from contree_cli.arguments import parser
from contree_cli.client import client_from_profile
from contree_cli.config import SETTINGS, Config, InsecurePathError
from contree_cli.log import setup_logging
from contree_cli.output import FORMATTERS
from contree_cli.session import SessionStore, get_session_key
from contree_cli.update_check import UpdateChecker

log = logging.getLogger(__name__)

# The only ``[cli]`` keys honoured as argparse defaults. Anything that
# routes credentials (url, token, project, profile, config_path,
# session_key) or dispatch (command, handler) must come from flags.
CLI_SETTINGS_KEYS = frozenset({"log_level", "output_format", "editor"})

# Network errors raised by the auto-detected transport backend. The
# stdlib ones are always available; urllib3/httpx raise their own
# hierarchies (neither inherits from OSError) when auto-detected as
# the backend instead of `requests` (whose exceptions do subclass
# OSError). Both imports are optional, mirroring how
# contree_client.sync.detect_backend() itself probes for them.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (OSError, http.client.HTTPException)
try:
    import urllib3.exceptions  # type: ignore[import-not-found]

    NETWORK_ERRORS = (*NETWORK_ERRORS, urllib3.exceptions.HTTPError)
except ModuleNotFoundError:
    pass
try:
    import httpx  # type: ignore[import-not-found]

    NETWORK_ERRORS = (*NETWORK_ERRORS, httpx.HTTPError)
except ModuleNotFoundError:
    pass


def main() -> None:
    if len(sys.argv) == 1:
        parser.print_help()
        exit(0)

    ignored_settings: list[str] = []
    if SETTINGS.has_section("cli"):
        cli_settings = SETTINGS["cli"]
        parser.set_defaults(
            **{k: v for k, v in cli_settings.items() if k in CLI_SETTINGS_KEYS}
        )
        # Keys inherited from [DEFAULT] (e.g. auth.ini's ``profile``)
        # are not [cli] settings; don't warn about them.
        ignored_settings = sorted(
            k
            for k in cli_settings
            if k not in CLI_SETTINGS_KEYS and k not in SETTINGS.defaults()
        )

    args = parser.parse_args()
    setup_logging(level=getattr(logging, args.log_level.upper(), logging.INFO))

    if ignored_settings:
        log.warning(
            "Ignoring unsupported [cli] keys in cli.ini: %s (supported: %s)",
            ", ".join(ignored_settings),
            ", ".join(sorted(CLI_SETTINGS_KEYS)),
        )
    try:
        config_mod.check_home()
    except InsecurePathError as exc:
        log.error("%s", exc)
        exit(1)

    # Update check runs only after argparse so it skips --help / --version
    # / no-command paths and so the warning respects --log-level. refresh()
    # is best-effort; check() is a pure predicate.
    checker = UpdateChecker()
    with suppress(Exception):
        checker.refresh()
    if not checker.is_latest():
        log.warning(
            "A new version of contree-cli is available: %s (installed: %s)."
            " Upgrade with `uv tool install -U contree-cli` or"
            " `pip install -U contree-cli`.",
            checker.state.latest_version,
            checker.current_version,
        )

    config_mod.CONFIG_FILE = args.config_path
    config_mod.CONFIG_DIR = args.config_path.parent

    try:
        cfg = Config(args.config_path)
    except InsecurePathError as exc:
        log.error("%s", exc)
        exit(1)
    profile = cfg.resolve(profile_override=args.profile)

    # CLI flags override resolved profile fields
    if args.token:
        profile = replace(profile, token=args.token)
    if args.url:
        profile = replace(profile, url=args.url)
    if args.project:
        profile = replace(profile, project=args.project)

    # Local-only commands don't need a client or a configured profile:
    # auth bootstraps its own; agent/man/skill operate purely on local files.
    LOCAL_COMMANDS = ("auth", "agent", "man", "skill")
    needs_client = args.command not in LOCAL_COMMANDS

    if needs_client and profile.name not in cfg:
        log.error(
            "Profile %r does not exist. Run `contree auth` first.",
            profile.name,
        )
        exit(1)

    # One stack owns every resource the command needs; entries are
    # added as they come to life and unwound together on the way out.
    with ExitStack() as stack:
        if needs_client:
            # Session-based transports (requests/httpx/urllib3) hold
            # pooled connections; enter the client so open()/close()
            # run around the whole command.
            try:
                client = client_from_profile(profile)
            except ValueError as exc:
                log.error("%s", exc)
                exit(1)
            CLIENT.set(stack.enter_context(client))

        formatter = FORMATTERS[args.output_format]()

        def close_formatter() -> None:
            # A closed stdout (e.g. piping into `less` and quitting
            # early) can make this final flush itself raise
            # BrokenPipeError -- the handler's own _NETWORK_ERRORS catch
            # already reported and exited by the time this callback
            # runs, so a second traceback here would just be noise.
            with suppress(BrokenPipeError):
                formatter.close()

        stack.callback(close_formatter)

        session_key = get_session_key(profile.name, override=args.session_key)
        db_path = config_mod.session_db_path(profile.name)
        log.debug("Running in session: %s", session_key)

        PROFILE.set(profile)
        FORMATTER.set(formatter)
        try:
            store = SessionStore(db_path, session_key)
        except InsecurePathError as exc:
            log.error("%s", exc)
            exit(1)
        SESSION_STORE.set(stack.enter_context(store))
        ctx = contextvars.copy_context()

        loader: type[ArgumentsProtocol] = args.load_args
        handler: Callable[[ArgumentsProtocol], int | None] = args.handler

        try:
            exit_code = ctx.run(handler, loader.from_args(args))
        except ContreeError as exc:
            log.error("%s", exc)
            exit(1)
        except ValueError as exc:
            # Raised by loader.from_args for malformed user input
            # (invalid UUIDs, etc.); the message is already user-facing.
            log.error("%s", exc)
            exit(1)
        except BrokenPipeError:
            # Reader closed its end of the pipe (e.g. `| head`, `| less`
            # and quit) -- not a network error, exit with the SIGPIPE
            # convention instead of logging a misleading message.
            exit(141)
        except NETWORK_ERRORS as exc:
            log.error("Network error: %s", exc)
            exit(1)
        except KeyboardInterrupt:
            log.error("User interrupted")
            exit(1)

    exit(exit_code or 0)


if __name__ == "__main__":
    main()
