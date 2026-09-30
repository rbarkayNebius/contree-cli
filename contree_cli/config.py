"""CLI-owned configuration on top of ``contree_client.profiles``.

Profile parsing, the auth.ini/cli.ini merge and the resolution
precedence (explicit name > ``CONTREE_PROFILE`` > active) live in the
library; this module keeps what only the CLI needs: the writable
profile store (atomic 0600 save, active-profile switching, deletion
with session-DB cleanup), the ``[cli]`` settings section, the editor
choice and the session database layout.
"""

from __future__ import annotations

import configparser
import io
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, MutableMapping
from contextlib import suppress
from pathlib import Path

from contree_client.profiles import (
    AUTH_TYPE_IAM,
    AUTH_TYPE_JWT,
    DEFAULT_IAM_URL,
    Profile,
    ProfileError,
    load_profiles,
    resolve_profile,
)

from .migrations import run_migrations

__all__ = [
    "AUTH_TYPE_IAM",
    "AUTH_TYPE_JWT",
    "CLI_CONFIG_FILE",
    "CONFIG_DIR",
    "CONFIG_FILE",
    "CONTREE_HOME",
    "DEFAULT_IAM_URL",
    "EDITOR",
    "SETTINGS",
    "Config",
    "InsecurePathError",
    "Profile",
    "check_dir",
    "check_file",
    "check_home",
    "ensure_private_dir",
    "get_default_path",
    "prepare_private_db",
    "remove_session_db",
    "session_db_path",
    "write_private",
]

log = logging.getLogger(__name__)


def get_default_path(env: str, default: str | Path) -> Path:
    return Path(os.getenv(env) or default).expanduser()


XDG_CONFIG_HOME = get_default_path("XDG_CONFIG_HOME", "~/.config")
CONTREE_HOME = get_default_path("CONTREE_HOME", XDG_CONFIG_HOME / "contree")
CONFIG_DIR = CONTREE_HOME
CONFIG_FILE = CONTREE_HOME / "auth.ini"
CLI_CONFIG_FILE = CONTREE_HOME / "cli.ini"


# -- ownership checks ----------------------------------------------------------
#
# Everything under CONTREE_HOME is trusted as the invoking user's own
# state (credentials, argparse defaults, the editor command, session
# data), so it must actually be theirs: a directory another local user
# created first (e.g. a predictable name under /tmp) must never become
# the CLI's configuration source or write target.


class InsecurePathError(Exception):
    """A path under CONTREE_HOME is not safely owned by the current user."""


_GO_WRITE = stat.S_IWGRP | stat.S_IWOTH


def _euid() -> int | None:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid is not None else None


def _check(path: Path, *, is_dir: bool, home: bool = False) -> None:
    """Raise :class:`InsecurePathError` unless *path* is safely ours.

    A missing path passes (callers create it privately). A symlink is
    followed only when the link itself is ours or root's. The target
    must be of the expected type and owned by us (or, except for the
    home itself, by root). Group/other write access is stripped when
    we own the path; a directory we do not own may keep it only when
    sticky (``/tmp``), where files inside are checked individually.
    """
    uid = _euid()
    if uid is None:  # no POSIX ownership model (Windows)
        return
    trusted = {uid} if home else {uid, 0}
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise InsecurePathError(f"cannot inspect {path}: {exc}") from None
    if stat.S_ISLNK(lst.st_mode) and lst.st_uid not in {uid, 0}:
        raise InsecurePathError(f"{path} is a symlink owned by uid {lst.st_uid}")
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise InsecurePathError(f"{path} is a dangling symlink") from None
    except OSError as exc:
        raise InsecurePathError(f"cannot inspect {path}: {exc}") from None
    kind_ok = stat.S_ISDIR(st.st_mode) if is_dir else stat.S_ISREG(st.st_mode)
    if not kind_ok:
        kind = "directory" if is_dir else "regular file"
        raise InsecurePathError(f"{path} is not a {kind}")
    if st.st_uid not in trusted:
        raise InsecurePathError(
            f"{path} is owned by uid {st.st_uid}, not by the current user"
            f" (uid {uid}); refusing to use it"
        )
    if not st.st_mode & _GO_WRITE:
        return
    if st.st_uid == uid:
        mode = stat.S_IMODE(st.st_mode) & ~_GO_WRITE
        log.warning("Removing group/other write access from %s", path)
        try:
            os.chmod(path, mode)
        except OSError as exc:
            raise InsecurePathError(
                f"{path} is writable by other users and cannot be fixed: {exc}"
            ) from None
        return
    if is_dir and not home and st.st_mode & stat.S_ISVTX:
        return
    raise InsecurePathError(f"{path} is writable by other users; refusing to use it")


def check_dir(path: Path, *, home: bool = False) -> None:
    _check(path, is_dir=True, home=home)


def check_file(path: Path) -> None:
    _check(path, is_dir=False)


def ensure_private_dir(path: Path) -> None:
    """Create *path* with mode 0700 if missing, then verify it is ours."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_dir(path, home=path == CONTREE_HOME)


def write_private(path: Path, data: str) -> None:
    """Atomically replace *path* with *data*, readable only by us.

    The content goes to a fresh 0600 ``mkstemp`` file in the same
    directory which is then renamed over *path*, so an existing file
    or symlink at *path* is replaced, never written through, and no
    byte is written before the mode is established.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def prepare_private_db(db_path: Path) -> None:
    """Make *db_path* safe to hand to ``sqlite3.connect``.

    The parent directory is created privately and verified, existing
    database/journal files must be ours, and a missing database is
    pre-created with mode 0600 (SQLite gives its journal files the
    database's mode).
    """
    ensure_private_dir(db_path.parent)
    for suffix in ("", "-wal", "-shm", "-journal"):
        check_file(db_path.with_name(db_path.name + suffix))
    try:
        fd = os.open(
            db_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
    except FileExistsError:
        return
    os.close(fd)


def _check_home_files() -> InsecurePathError | None:
    try:
        check_dir(CONTREE_HOME, home=True)
        check_file(CLI_CONFIG_FILE)
        check_file(CONFIG_FILE)
    except InsecurePathError as exc:
        return exc
    return None


# Parsed at import time. Paths are fixed by CONTREE_HOME (env), so the
# ``[cli]`` section is available before argparse runs and can supply
# defaults that beat hardcoded ones but still lose to flags. Nothing is
# read from a home that fails the ownership checks; main() reports it.
SETTINGS = configparser.ConfigParser()
HOME_ERROR = _check_home_files()
if HOME_ERROR is None:
    SETTINGS.read([CLI_CONFIG_FILE, CONFIG_FILE])


def check_home() -> None:
    """Raise if CONTREE_HOME failed the import-time ownership checks."""
    if HOME_ERROR is not None:
        raise InsecurePathError(
            f"{HOME_ERROR}. CONTREE_HOME must be a directory owned by you"
            " and not writable by others (e.g. ~/.config/contree)."
        )
    with suppress(OSError):
        parent = os.stat(CONTREE_HOME.parent)
        if parent.st_mode & stat.S_IWOTH:
            log.warning(
                "CONTREE_HOME %s is inside world-writable directory %s;"
                " use a private location such as ~/.config/contree",
                CONTREE_HOME,
                CONTREE_HOME.parent,
            )


# Default editor for ``contree file edit`` when ``--editor`` is not given.
# Resolved once at import time. Priority: $EDITOR > cli.ini > vim > nano > vi.
EDITOR = (
    os.environ.get("EDITOR")
    or SETTINGS.get("cli", "editor", fallback=None)
    or shutil.which("vim")
    or shutil.which("nano")
    or "vi"
)


def session_db_path(profile_name: str) -> Path:
    """Per-profile session database location (CLI-owned layout).

    ``CONTREE_SESSION_DB`` overrides the computed path entirely.
    """
    override = os.getenv("CONTREE_SESSION_DB")
    if override:
        return Path(override).expanduser()
    return CONTREE_HOME / "cli" / "sessions" / f"{profile_name}.db"


def remove_session_db(profile_name: str) -> None:
    db = session_db_path(profile_name)
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        p.unlink(missing_ok=True)


class Config(MutableMapping[str, Profile]):
    """Writable INI-backed profile store over the library reader.

    Dict-like: ``cfg[name]``, ``cfg[name] = profile``,
    ``del cfg[name]``, ``name in cfg``, ``len(cfg)``, iteration.
    """

    def __init__(self, path: Path | None = None) -> None:
        check_dir(CONTREE_HOME, home=True)
        run_migrations(CONTREE_HOME)
        self.__path = path or CONFIG_FILE
        # The library reads the sibling cli.ini alongside auth.ini.
        check_dir(self.__path.parent, home=self.__path.parent == CONTREE_HOME)
        check_file(self.__path.parent / "cli.ini")
        check_file(self.__path)
        self.__profiles: dict[str, Profile] = {}
        self.__active: str = "default"
        self._load()

    @property
    def path(self) -> Path:
        return self.__path

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        log.debug("Loading profiles for %s", self.__path)
        self.__profiles, self.__active = load_profiles(self.__path)

    def _save(self) -> None:
        cp = configparser.ConfigParser()
        cp["DEFAULT"]["profile"] = self.__active
        for profile in self.__profiles.values():
            profile.save(cp)
        ensure_private_dir(self.__path.parent)
        buf = io.StringIO()
        cp.write(buf)
        # 0600 temp file renamed over auth.ini: the token is never
        # readable by others and an existing file/symlink is replaced,
        # not written through.
        write_private(self.__path, buf.getvalue())

    # -- MutableMapping interface --------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self.__profiles

    def __getitem__(self, name: str) -> Profile:
        return self.__profiles[name]

    def __setitem__(self, name: str, profile: Profile) -> None:
        assert name == profile.name, "profile name must match key"
        self.__profiles[name] = profile
        self._save()

    def __delitem__(self, name: str) -> None:
        if name not in self.__profiles:
            raise KeyError(name)
        self.__profiles.pop(name)
        if self.__active == name:
            self.__active = next(iter(self.__profiles), "default")
        self._save()
        remove_session_db(name)

    def __len__(self) -> int:
        return len(self.__profiles)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__profiles)

    # -- profile management --------------------------------------------------

    @property
    def current(self) -> Profile:
        return self.__profiles[self.__active]

    @current.setter
    def current(self, profile: Profile) -> None:
        self.__active = profile.name
        self._save()

    def resolve(self, profile_override: str | None = None) -> Profile:
        """Resolve the active profile by name.

        The library owns the precedence (*profile_override* >
        ``CONTREE_PROFILE`` > config default). A missing profile
        yields a credential-less stub instead of an error so local
        commands still run and main() reports remote ones itself.
        Credentials come strictly from the saved profile; runtime
        commands do not read tokens, URLs, or project IDs from the
        environment. To register/refresh credentials from env vars use
        ``contree auth``.
        """
        try:
            return resolve_profile(profile_override, path=self.__path)
        except ProfileError:
            name = (
                profile_override or os.environ.get("CONTREE_PROFILE") or self.__active
            )
            return Profile(name=name, url="", token=None)

    def switch(self, name: str) -> None:
        """Set the active profile."""
        if name not in self.__profiles:
            raise ValueError(f"profile {name!r} does not exist")
        self.__active = name
        self._save()
