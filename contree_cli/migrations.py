"""One-shot data layout migrations for ``CONTREE_HOME``.

DELETE-ME: this entire module is transitional. It carries users of
``contree-cli <= 0.4.x`` (flat layout, ``~/.config/contree-cli/``) onto
the current layout (``~/.config/contree/`` with a ``cli/`` subdir).

After ~2 minor releases (target: ``0.7.0``) drop this file, the
``run_migrations`` import and call in ``config.py``, and
``tests/test_migrations.py``. Anyone still on the old layout at that
point can do the rename by hand from the changelog.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

LEGACY_DIRNAME = "contree-cli"
LEGACY_CONFIG_BASENAME = "config.ini"


def run_migrations(home: Path) -> None:
    """Idempotent layout migrations into ``home``."""
    migrate_legacy_dir(home)
    migrate_into_cli_subdir(home)


def migrate_legacy_dir(home: Path) -> None:
    """Move ``<parent>/contree-cli/*`` into ``home``.

    Renames ``config.ini`` to ``auth.ini``. Skipped when ``home/auth.ini``
    already exists, so freshly-created session DBs in ``home`` don't
    block credential migration.
    """
    legacy_dir = home.parent / LEGACY_DIRNAME
    if not legacy_dir.is_dir():
        return
    if (home / "auth.ini").exists():
        return

    log.info("Migrating CONTREE_HOME: %s -> %s", legacy_dir, home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)

    for item in legacy_dir.iterdir():
        target_name = "auth.ini" if item.name == LEGACY_CONFIG_BASENAME else item.name
        target = home / target_name
        if target.exists():
            log.warning("Skipping %s: %s already exists", item, target)
            continue
        item.rename(target)

    try:
        legacy_dir.rmdir()
    except OSError:
        log.debug(
            "Legacy dir %s not empty after migration; leaving in place", legacy_dir
        )


def migrate_into_cli_subdir(home: Path) -> None:
    """Move flat session/skill DBs into the ``cli/`` subdirectory.

    - ``home/sessions-{name}.db*`` → ``home/cli/sessions/{name}.db*``
    - ``home/skills.db*``          → ``home/cli/skills.db*``
    """
    if not home.is_dir():
        return
    cli_dir = home / "cli"
    sessions_dir = cli_dir / "sessions"
    moved_any = False

    for item in home.iterdir():
        if not item.is_file():
            continue
        if item.name.startswith("sessions-"):
            target = sessions_dir / item.name[len("sessions-") :]
            sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif item.name.startswith("skills.db"):
            target = cli_dir / item.name
            cli_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            continue

        if target.exists():
            log.warning("Skipping %s: %s already exists", item, target)
            continue
        if not moved_any:
            log.info("Reorganising CLI state into %s", cli_dir)
            moved_any = True
        item.rename(target)
