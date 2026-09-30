from __future__ import annotations

import base64
import gzip
import json
import os
import posixpath
import sqlite3
import sys
import uuid
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePosixPath

from contree_cli.config import prepare_private_db

CONTREE_CONCURRENCY = int(os.getenv("CONTREE_CONCURRENCY", "8"))
CONTREE_DB_TIMEOUT = float(os.getenv("CONTREE_DB_TIMEOUT", "30"))


@dataclass(frozen=True)
class Session:
    session_key: str
    active_branch: str
    current_image: str
    last_kind: str
    last_title: str
    updated_at: str
    cwd: str = ""


@dataclass(frozen=True)
class PendingFile:
    instance_path: str
    file_uuid: str
    uid: int
    gid: int
    mode: str


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    image_uuid: str
    parent_id: int | None
    kind: str
    title: str
    operation_uuid: str
    created_at: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS session_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key    TEXT NOT NULL,
    image_uuid     TEXT NOT NULL,
    parent_id      INTEGER REFERENCES session_history(id),
    kind           TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    operation_uuid TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS session_branches (
    session_key  TEXT NOT NULL,
    branch_name  TEXT NOT NULL,
    history_id   INTEGER NOT NULL REFERENCES session_history(id),
    PRIMARY KEY (session_key, branch_name)
);

CREATE TABLE IF NOT EXISTS session_state (
    session_key    TEXT PRIMARY KEY,
    active_branch  TEXT NOT NULL DEFAULT 'main',
    cwd            TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS session_files (
    history_id    INTEGER NOT NULL REFERENCES session_history(id),
    instance_path TEXT NOT NULL,
    file_uuid     TEXT NOT NULL,
    uid           INTEGER NOT NULL DEFAULT 0,
    gid           INTEGER NOT NULL DEFAULT 0,
    mode          TEXT NOT NULL DEFAULT '0644',
    PRIMARY KEY (history_id, instance_path)
);

CREATE TABLE IF NOT EXISTS shell_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key  TEXT NOT NULL,
    line         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS image_cache (
    image_uuid TEXT NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (image_uuid, kind)
);

CREATE TABLE IF NOT EXISTS session_env (
    session_key TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    PRIMARY KEY (session_key, key)
);

CREATE INDEX IF NOT EXISTS ix_history_session ON session_history(session_key);
CREATE INDEX IF NOT EXISTS ix_history_parent ON session_history(parent_id);
CREATE INDEX IF NOT EXISTS ix_shell_history_session ON shell_history(session_key);
CREATE INDEX IF NOT EXISTS ix_image_cache_kind ON image_cache(kind);
"""


def _entry_from_row(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"],
        image_uuid=row["image_uuid"],
        parent_id=row["parent_id"],
        kind=row["kind"],
        title=row["title"],
        operation_uuid=row["operation_uuid"],
        created_at=row["created_at"],
    )


CacheKey = tuple[str, str]

_GZIP_THRESHOLD = 1024


class ImageCache(MutableMapping[CacheKey, object]):
    """Persistent cache for immutable sandbox image data.

    Key is ``(image_uuid, kind)`` where *kind* encodes the cache
    entry type (e.g. ``"files:/etc/"`` or ``"images"``).  Values are
    transparently JSON-serialised.  Payloads above 1 KiB are
    gzip-compressed and base64-encoded (``gzip:...`` prefix);
    smaller payloads are stored as plain JSON (``json:...`` prefix).
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @staticmethod
    def _encode(value: object) -> str:
        raw = json.dumps(value)
        if len(raw) <= _GZIP_THRESHOLD:
            return f"json:{raw}"
        compressed = base64.b64encode(
            gzip.compress(raw.encode()),
        ).decode("ascii")
        return f"gzip:{compressed}"

    @staticmethod
    def _decode(blob: str) -> object:
        if blob.startswith("json:"):
            return json.loads(blob[5:])
        if blob.startswith("gzip:"):
            return json.loads(
                gzip.decompress(base64.b64decode(blob[5:])),
            )
        # No prefix: plain JSON payload.
        return json.loads(blob)

    def __getitem__(self, key: CacheKey) -> object:
        image_uuid, kind = key
        row = self._conn.execute(
            "SELECT value FROM image_cache WHERE image_uuid=? AND kind=?",
            (image_uuid, kind),
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return self._decode(row["value"])

    def __setitem__(self, key: CacheKey, value: object) -> None:
        image_uuid, kind = key
        self._conn.execute(
            "INSERT OR REPLACE INTO image_cache "
            "(image_uuid, kind, value) VALUES (?, ?, ?)",
            (image_uuid, kind, self._encode(value)),
        )
        self._conn.commit()

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, tuple) or len(key) != 2:
            return False
        image_uuid, kind = key
        row = self._conn.execute(
            "SELECT 1 FROM image_cache WHERE image_uuid=? AND kind=?",
            (image_uuid, kind),
        ).fetchone()
        return row is not None

    def __delitem__(self, key: CacheKey) -> None:
        image_uuid, kind = key
        cur = self._conn.execute(
            "DELETE FROM image_cache WHERE image_uuid=? AND kind=?",
            (image_uuid, kind),
        )
        if cur.rowcount == 0:
            raise KeyError(key)
        self._conn.commit()

    def __iter__(self) -> Iterator[CacheKey]:
        rows = self._conn.execute(
            "SELECT image_uuid, kind FROM image_cache",
        ).fetchall()
        return iter((row["image_uuid"], row["kind"]) for row in rows)

    def __len__(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM image_cache",
        ).fetchone()
        assert row is not None
        return row[0]  # type: ignore[no-any-return]

    def local_file_paths(self) -> dict[str, str]:
        """Map remote file UUID to whatever this machine uploaded it from.

        Reads every ``local_file:*`` cache entry. The value is either an
        absolute host path (for files uploaded by ``run --file`` /
        ``COPY``) or a ``https://...`` URL (for ``ADD URL``); both come
        out of the same cache namespace so callers get a single mapping
        regardless of upload source.
        """
        cur = self._conn.execute(
            "SELECT value FROM image_cache WHERE kind LIKE 'local_file:%'",
        )
        result: dict[str, str] = {}
        for row in cur.fetchall():
            value = self._decode(row["value"])
            if not isinstance(value, dict):
                continue
            uuid_str = value.get("uuid")
            if not isinstance(uuid_str, str):
                continue
            origin = value.get("local_path") or value.get("url")
            if isinstance(origin, str) and origin:
                result[uuid_str] = origin
        return result

    def invalidate_prefix(
        self,
        *,
        image_prefix: str | None = None,
        kind_prefix: str | None = None,
    ) -> int:
        """Drop cache entries by image_uuid prefix and/or kind prefix.

        Returns the number of rows removed. ``image_prefix`` and
        ``kind_prefix`` may be combined; both default to "match anything"
        when omitted (caller must pass at least one).
        """
        if image_prefix is None and kind_prefix is None:
            raise ValueError("invalidate_prefix needs image_prefix or kind_prefix")
        clauses: list[str] = []
        params: list[object] = []
        if image_prefix is not None:
            clauses.append("image_uuid LIKE ?")
            params.append(image_prefix + "%")
        if kind_prefix is not None:
            clauses.append("kind LIKE ?")
            params.append(kind_prefix + "%")
        cur = self._conn.execute(
            "DELETE FROM image_cache WHERE " + " AND ".join(clauses),
            tuple(params),
        )
        self._conn.commit()
        return cur.rowcount


class SessionStore:
    MAX_SHELL_HISTORY = 10_000

    def __init__(self, db_path: Path, session_key: str) -> None:
        self._session_key = session_key
        prepare_private_db(db_path)
        self._conn = sqlite3.connect(str(db_path), timeout=CONTREE_DB_TIMEOUT)
        self._conn.row_factory = sqlite3.Row
        # WAL: concurrent readers + one writer; safe across processes.
        # synchronous=NORMAL: faster commits in WAL, still durable on
        # crash; shortens write-lock hold time, reducing SQLITE_BUSY
        # between two contree shells sharing the per-profile DB.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(CONTREE_DB_TIMEOUT * 1000)}")
        self._conn.executescript(SCHEMA)

    @cached_property
    def cache(self) -> ImageCache:
        return ImageCache(self._conn)

    def _query_sessions(
        self,
        suffix: str = "",
        params: tuple[object, ...] = (),
    ) -> list[Session]:
        # Not SQLAlchemy — plain sqlite3. The suffix is always a
        # hardcoded string literal from calling code (WHERE/ORDER BY),
        # never user input. All values go through ? placeholders.
        # nosemgrep: sqlalchemy-execute-raw-query
        cur = self._conn.execute(
            """
            SELECT s.session_key, s.active_branch, s.cwd,
                   h.image_uuid, h.kind, h.title, s.updated_at
            FROM session_state s
            JOIN session_branches b
                ON b.session_key = s.session_key
               AND b.branch_name = s.active_branch
            JOIN session_history h ON h.id = b.history_id
            """
            + suffix,
            params,
        )
        return [
            Session(
                session_key=row["session_key"],
                active_branch=row["active_branch"],
                current_image=row["image_uuid"],
                last_kind=row["kind"],
                last_title=row["title"],
                updated_at=row["updated_at"],
                cwd=row["cwd"],
            )
            for row in cur.fetchall()
        ]

    @property
    def session_key(self) -> str:
        return self._session_key

    def select_session(self, session_key: str) -> None:
        """Select another session in the same profile database.

        A session key is only a scope for queries made through this store;
        changing it does not require opening a second SQLite connection.
        """
        self._session_key = session_key

    @property
    def current_image(self) -> str:
        s = self.session
        if s is None:
            print(
                "No active session. Run `contree use IMAGE` to start one.\n"
                "Agents: read `contree agent` for workflow and set a session first.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return s.current_image

    @property
    def session(self) -> Session | None:
        rows = self._query_sessions(
            "WHERE s.session_key = ?",
            (self._session_key,),
        )
        return rows[0] if rows else None

    def history_depth(self) -> int:
        """Count steps from root to the current branch tip."""
        cur = self._conn.execute(
            """
            SELECT b.history_id
            FROM session_state s
            JOIN session_branches b
                ON b.session_key = s.session_key
               AND b.branch_name = s.active_branch
            WHERE s.session_key = ?
            """,
            (self._session_key,),
        )
        row = cur.fetchone()
        if row is None:
            return 0
        depth = 0
        hid: int | None = row["history_id"]
        while hid is not None:
            depth += 1
            cur = self._conn.execute(
                "SELECT parent_id FROM session_history WHERE id = ?",
                (hid,),
            )
            parent = cur.fetchone()
            hid = parent["parent_id"] if parent is not None else None
        return depth

    def _get_history_entry(self, history_id: int) -> HistoryEntry:
        cur = self._conn.execute(
            "SELECT * FROM session_history WHERE id = ?",
            (history_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"History entry {history_id} not found")
        return _entry_from_row(row)

    def branch_tip(self, branch_name: str) -> HistoryEntry:
        cur = self._conn.execute(
            "SELECT history_id FROM session_branches WHERE session_key = ? "
            "AND branch_name = ?",
            (self._session_key, branch_name),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Branch {branch_name!r} does not exist")
        return self._get_history_entry(row["history_id"])

    def create_detached_branch(self, op_uuid: str, title: str) -> str:
        branch_name = f"detached-{op_uuid}"
        tip = self.branch_tip(self._active_branch() or "main")
        cur = self._conn.execute(
            """
            INSERT INTO session_history
                (session_key, image_uuid, parent_id, kind, title, operation_uuid)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._session_key,
                tip.image_uuid,
                tip.id,
                "run-detached",
                title,
                op_uuid,
            ),
        )
        new_id = cur.lastrowid
        assert new_id is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO session_branches "
            "(session_key, branch_name, history_id) VALUES (?, ?, ?)",
            (self._session_key, branch_name, new_id),
        )
        self._conn.commit()
        return branch_name

    def create_disposable_branch(self, op_uuid: str, title: str) -> str:
        branch_name = f"disposable-{op_uuid}"
        tip = self.branch_tip(self._active_branch() or "main")
        cur = self._conn.execute(
            """
            INSERT INTO session_history
                (session_key, image_uuid, parent_id, kind, title, operation_uuid)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._session_key,
                tip.image_uuid,
                tip.id,
                "run-disposable",
                title,
                op_uuid,
            ),
        )
        new_id = cur.lastrowid
        assert new_id is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO session_branches "
            "(session_key, branch_name, history_id) VALUES (?, ?, ?)",
            (self._session_key, branch_name, new_id),
        )
        self._conn.commit()
        return branch_name

    def navigate(self, target: int) -> HistoryEntry:
        """Navigate session history.

        target > 0: absolute jump to history entry with that id.
        target < 0: go back abs(target) steps (relative backward).
        target == 0: error.
        """
        if target == 0:
            raise ValueError("Navigation target must not be 0")

        # Get active branch tip
        cur = self._conn.execute(
            """
            SELECT b.history_id, s.active_branch
            FROM session_state s
            JOIN session_branches b
                ON b.session_key = s.session_key
               AND b.branch_name = s.active_branch
            WHERE s.session_key = ?
            """,
            (self._session_key,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("No active session")
        history_id: int = row["history_id"]
        branch: str = row["active_branch"]

        if target > 0:
            # Absolute jump — validate entry exists in this session
            entry_row = self._conn.execute(
                "SELECT id FROM session_history WHERE id = ? AND session_key = ?",
                (target, self._session_key),
            ).fetchone()
            if entry_row is None:
                raise ValueError(f"History entry {target} not found in this session")
            current_id = target
        else:
            # Relative backward: walk parent chain
            n = abs(target)
            current_id = history_id
            for i in range(n):
                entry = self._get_history_entry(current_id)
                if entry.parent_id is None:
                    raise ValueError(
                        f"Cannot go back {n} steps: only {i} ancestors available"
                    )
                current_id = entry.parent_id

        # Move branch pointer
        self._conn.execute(
            "UPDATE session_branches SET history_id = ? "
            "WHERE session_key = ? AND branch_name = ?",
            (current_id, self._session_key, branch),
        )
        self._conn.execute(
            "UPDATE session_state "
            "SET updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE session_key = ?",
            (self._session_key,),
        )
        self._conn.commit()
        return self._get_history_entry(current_id)

    def navigate_forward(self, steps: int) -> HistoryEntry:
        """Go forward N steps, picking the latest child at branch points."""
        if steps < 1:
            raise ValueError("Forward steps must be >= 1")

        # Get active branch tip
        cur = self._conn.execute(
            """
            SELECT b.history_id, s.active_branch
            FROM session_state s
            JOIN session_branches b
                ON b.session_key = s.session_key
               AND b.branch_name = s.active_branch
            WHERE s.session_key = ?
            """,
            (self._session_key,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("No active session")
        current_id: int = row["history_id"]
        branch: str = row["active_branch"]

        for i in range(steps):
            child = self._conn.execute(
                "SELECT id FROM session_history "
                "WHERE parent_id = ? AND session_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (current_id, self._session_key),
            ).fetchone()
            if child is None:
                raise ValueError(
                    f"Cannot go forward {steps} steps: only {i} children available"
                )
            current_id = child["id"]

        # Move branch pointer
        self._conn.execute(
            "UPDATE session_branches SET history_id = ? "
            "WHERE session_key = ? AND branch_name = ?",
            (current_id, self._session_key, branch),
        )
        self._conn.execute(
            "UPDATE session_state "
            "SET updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE session_key = ?",
            (self._session_key,),
        )
        self._conn.commit()
        return self._get_history_entry(current_id)

    def rollback(self, n: int = 1) -> HistoryEntry:
        """Go back N steps. Thin wrapper around navigate()."""
        if n < 1:
            raise ValueError("Rollback steps must be >= 1")
        return self.navigate(-n)

    def tip_history_id(self) -> int:
        cur = self._conn.execute(
            """
            SELECT b.history_id
            FROM session_state s
            JOIN session_branches b
                ON b.session_key = s.session_key
               AND b.branch_name = s.active_branch
            WHERE s.session_key = ?
            """,
            (self._session_key,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("No active session")
        return int(row["history_id"])

    def resolve_history_spec(self, spec: str) -> HistoryEntry:
        """Resolve a non-mutating history reference.

        Accepted forms (mirrors ``session rollback`` semantics):

        - ``""`` (empty)     -- the active branch tip (current entry).
        - ``"N"`` (positive) -- absolute history id.
        - ``"-N"``           -- walk N steps back from the active branch tip.
        - ``"+N"``           -- walk N steps forward from the tip, picking the
          latest child at each branch point.

        ``"0"`` is rejected because it is ambiguous (matches both
        "absolute 0" and "no movement"). Raises :class:`ValueError` if
        the spec is malformed, the resulting entry does not exist in
        this session, or the requested walk exceeds the available
        ancestors/children.
        """
        if spec == "":
            tip_id = self.tip_history_id()
            return self._get_history_entry(tip_id)

        if spec[:1] in ("+", "-"):
            sign = spec[0]
            digits = spec[1:]
        else:
            sign = ""
            digits = spec
        if not digits.isdigit():
            raise ValueError(f"Invalid history reference: {spec!r}")
        n = int(digits)
        if n == 0:
            raise ValueError("History reference must be a non-zero number")

        if sign == "":
            return self._get_history_entry(n)

        current_id = self.tip_history_id()

        if sign == "-":
            for i in range(n):
                entry = self._get_history_entry(current_id)
                if entry.parent_id is None:
                    raise ValueError(
                        f"Cannot go back {n} steps: only {i} ancestors available"
                    )
                current_id = entry.parent_id
        else:
            for i in range(n):
                child = self._conn.execute(
                    "SELECT id FROM session_history "
                    "WHERE parent_id = ? AND session_key = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (current_id, self._session_key),
                ).fetchone()
                if child is None:
                    raise ValueError(
                        f"Cannot go forward {n} steps: only {i} children available"
                    )
                current_id = child["id"]
        return self._get_history_entry(current_id)

    def create_branch(
        self,
        name: str,
        from_branch: str | None = None,
    ) -> None:
        source = from_branch or self._active_branch()
        if source is None:
            raise ValueError("No active session")

        cur = self._conn.execute(
            "SELECT history_id FROM session_branches "
            "WHERE session_key = ? AND branch_name = ?",
            (self._session_key, source),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"Source branch {source!r} does not exist",
            )
        history_id: int = row["history_id"]

        cur = self._conn.execute(
            "SELECT 1 FROM session_branches WHERE session_key = ? AND branch_name = ?",
            (self._session_key, name),
        )
        if cur.fetchone() is not None:
            raise ValueError(f"Branch {name!r} already exists")

        self._conn.execute(
            "INSERT INTO session_branches "
            "(session_key, branch_name, history_id) "
            "VALUES (?, ?, ?)",
            (self._session_key, name, history_id),
        )
        self._conn.commit()

    def list_branches(self) -> list[tuple[str, bool]]:
        active = self._active_branch()
        if active is None:
            return []
        cur = self._conn.execute(
            "SELECT branch_name FROM session_branches "
            "WHERE session_key = ? ORDER BY branch_name",
            (self._session_key,),
        )
        return [
            (row["branch_name"], row["branch_name"] == active) for row in cur.fetchall()
        ]

    def delete_branch(self, name: str) -> None:
        active = self._active_branch()
        if name == active:
            raise ValueError("Cannot delete the active branch")
        cur = self._conn.execute(
            "DELETE FROM session_branches WHERE session_key = ? AND branch_name = ?",
            (self._session_key, name),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Branch {name!r} does not exist")
        self._conn.execute(
            "UPDATE session_state SET updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE session_key = ?",
            (self._session_key,),
        )
        self._conn.commit()

    def prune_branches(self) -> list[str]:
        active = self._active_branch()
        cur = self._conn.execute(
            "SELECT branch_name FROM session_branches WHERE session_key = ?",
            (self._session_key,),
        )
        removed: list[str] = []
        for row in cur.fetchall():
            name = row["branch_name"]
            if name == active:
                continue
            if name.startswith("detached-") or name.startswith("disposable-"):
                self._conn.execute(
                    "DELETE FROM session_branches WHERE session_key = ? "
                    "AND branch_name = ?",
                    (self._session_key, name),
                )
                removed.append(name)
        if removed:
            self._conn.execute(
                "UPDATE session_state SET updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE session_key = ?",
                (self._session_key,),
            )
            self._conn.commit()
        return removed

    def switch_branch(self, name: str) -> HistoryEntry:
        cur = self._conn.execute(
            "SELECT history_id FROM session_branches "
            "WHERE session_key = ? AND branch_name = ?",
            (self._session_key, name),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Branch {name!r} does not exist")
        history_id: int = row["history_id"]

        self._conn.execute(
            "UPDATE session_state SET active_branch = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE session_key = ?",
            (name, self._session_key),
        )
        self._conn.commit()
        return self._get_history_entry(history_id)

    def list_sessions(self) -> list[Session]:
        return self._query_sessions("ORDER BY s.updated_at DESC")

    def delete_session(self, key: str) -> bool:
        """Delete all data for a session. Returns True if it existed."""
        row = self._conn.execute(
            "SELECT 1 FROM session_state WHERE session_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return False
        history_ids = [
            r["id"]
            for r in self._conn.execute(
                "SELECT id FROM session_history WHERE session_key = ?",
                (key,),
            ).fetchall()
        ]
        if history_ids:
            placeholders = ",".join("?" * len(history_ids))
            # Not SQLAlchemy — plain sqlite3. placeholders is just
            # ",".join("?" * N), a safe parameterised IN clause.
            # nosemgrep: sqlalchemy-execute-raw-query
            self._conn.execute(
                f"DELETE FROM session_files WHERE history_id IN ({placeholders})",
                history_ids,
            )
        self._conn.execute("DELETE FROM session_env WHERE session_key = ?", (key,))
        self._conn.execute("DELETE FROM shell_history WHERE session_key = ?", (key,))
        self._conn.execute("DELETE FROM session_branches WHERE session_key = ?", (key,))
        self._conn.execute("DELETE FROM session_history WHERE session_key = ?", (key,))
        self._conn.execute("DELETE FROM session_state WHERE session_key = ?", (key,))
        self._conn.commit()
        return True

    def history_dag(
        self,
    ) -> tuple[list[HistoryEntry], dict[int, list[str]]]:
        return self.history_dag_for(self._session_key)

    def history_dag_for(
        self,
        session_key: str,
    ) -> tuple[list[HistoryEntry], dict[int, list[str]]]:
        cur = self._conn.execute(
            "SELECT * FROM session_history WHERE session_key = ? ORDER BY id",
            (session_key,),
        )
        entries = [_entry_from_row(row) for row in cur.fetchall()]

        cur = self._conn.execute(
            "SELECT history_id, branch_name "
            "FROM session_branches WHERE session_key = ?",
            (session_key,),
        )
        branch_map: dict[int, list[str]] = {}
        for row in cur.fetchall():
            branch_map.setdefault(row["history_id"], []).append(
                row["branch_name"],
            )
        return entries, branch_map

    def find_session(self, name: str) -> Session:
        # Try suffix match first
        rows = self._query_sessions(
            "WHERE s.session_key LIKE ?",
            (f"%_{name}",),
        )
        if not rows:
            # Try exact match
            rows = self._query_sessions(
                "WHERE s.session_key = ?",
                (name,),
            )
        if not rows:
            raise ValueError(f"Session {name!r} not found")
        if len(rows) > 1:
            keys = ", ".join(r.session_key for r in rows)
            raise ValueError(
                f"Ambiguous session {name!r}: matches {keys}",
            )
        return rows[0]

    def _active_branch(self) -> str | None:
        cur = self._conn.execute(
            "SELECT active_branch FROM session_state WHERE session_key = ?",
            (self._session_key,),
        )
        row = cur.fetchone()
        return row["active_branch"] if row else None

    def set_image(
        self,
        image_uuid: str,
        *,
        kind: str = "",
        title: str = "",
        operation_uuid: str = "",
    ) -> int:
        return self._set_image_on_branch(
            None, image_uuid, kind=kind, title=title, operation_uuid=operation_uuid
        )

    def set_image_on_branch(
        self,
        branch_name: str,
        image_uuid: str,
        *,
        kind: str = "",
        title: str = "",
        operation_uuid: str = "",
    ) -> int:
        return self._set_image_on_branch(
            branch_name,
            image_uuid,
            kind=kind,
            title=title,
            operation_uuid=operation_uuid,
        )

    def _set_image_on_branch(
        self,
        branch_name: str | None,
        image_uuid: str,
        *,
        kind: str = "",
        title: str = "",
        operation_uuid: str = "",
    ) -> int:
        if branch_name is None:
            cur = self._conn.execute(
                """
                SELECT b.history_id, s.active_branch
                FROM session_state s
                JOIN session_branches b
                    ON b.session_key = s.session_key
                   AND b.branch_name = s.active_branch
                WHERE s.session_key = ?
                """,
                (self._session_key,),
            )
            row = cur.fetchone()
            parent_id: int | None = row["history_id"] if row else None
            branch: str = row["active_branch"] if row else "main"
        else:
            cur = self._conn.execute(
                "SELECT history_id FROM session_branches WHERE session_key = ? "
                "AND branch_name = ?",
                (self._session_key, branch_name),
            )
            row = cur.fetchone()
            parent_id = row["history_id"] if row else None
            branch = branch_name

        cur = self._conn.execute(
            """
            INSERT INTO session_history
                (session_key, image_uuid, parent_id, kind, title, operation_uuid)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._session_key,
                image_uuid,
                parent_id,
                kind,
                title,
                operation_uuid,
            ),
        )
        new_id = cur.lastrowid
        assert new_id is not None

        self._conn.execute(
            """
            INSERT INTO session_branches
                (session_key, branch_name, history_id)
            VALUES (?, ?, ?)
            ON CONFLICT(session_key, branch_name) DO UPDATE SET
                history_id = excluded.history_id
            """,
            (self._session_key, branch, new_id),
        )

        self._conn.execute(
            """
            INSERT INTO session_state
                (session_key, active_branch, updated_at)
            VALUES (?, 'main', strftime('%Y-%m-%dT%H:%M:%S','now'))
            ON CONFLICT(session_key) DO UPDATE SET
                updated_at = strftime('%Y-%m-%dT%H:%M:%S','now')
            """,
            (self._session_key,),
        )
        self._conn.commit()
        return new_id

    def add_pending_file(
        self,
        history_id: int,
        instance_path: str,
        file_uuid: str,
        uid: int = 0,
        gid: int = 0,
        mode: str = "0644",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO session_files
                (history_id, instance_path, file_uuid, uid, gid, mode)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (history_id, instance_path, file_uuid, uid, gid, mode),
        )
        self._conn.commit()

    def pending_files(self) -> list[PendingFile]:
        """Return pending files that haven't been baked in by a run yet.

        Walks history from the branch tip backwards, collecting
        ``kind='file'`` entry IDs until hitting a ``kind='run'`` entry.
        Looks up the corresponding file records in session_files.
        """
        branch = self._active_branch()
        if branch is None:
            return []
        cur = self._conn.execute(
            "SELECT history_id FROM session_branches "
            "WHERE session_key = ? AND branch_name = ?",
            (self._session_key, branch),
        )
        row = cur.fetchone()
        if row is None:
            return []

        # Walk history from tip, collect file entry IDs until a run
        file_history_ids: list[int] = []
        history_id: int = row["history_id"]
        while True:
            entry = self._get_history_entry(history_id)
            if entry.kind == "run":
                break
            if entry.kind == "file":
                file_history_ids.append(entry.id)
            if entry.parent_id is None:
                break
            history_id = entry.parent_id

        if not file_history_ids:
            return []

        placeholders = ",".join("?" for _ in file_history_ids)
        cur = self._conn.execute(
            "SELECT instance_path, file_uuid, uid, gid, mode "
            "FROM session_files "
            f"WHERE history_id IN ({placeholders}) "
            "ORDER BY history_id DESC",
            tuple(file_history_ids),
        )
        # Deduplicate by path — most recent edit (highest history_id) wins
        seen: set[str] = set()
        result: list[PendingFile] = []
        for r in cur.fetchall():
            if r["instance_path"] not in seen:
                seen.add(r["instance_path"])
                result.append(
                    PendingFile(
                        instance_path=r["instance_path"],
                        file_uuid=r["file_uuid"],
                        uid=r["uid"],
                        gid=r["gid"],
                        mode=r["mode"],
                    )
                )
        return result

    def clear_pending_files(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM session_files "
            "WHERE history_id IN ("
            "  SELECT id FROM session_history "
            "  WHERE session_key = ?"
            ")",
            (self._session_key,),
        )
        self._conn.commit()
        return cur.rowcount

    def get_cwd(self) -> str:
        """Return the current working directory from session history.

        Walks the history chain from the branch tip backwards until a
        ``kind='cd'`` entry is found.  Returns its *title* (the path).
        Returns ``""`` when no ``cd`` entry exists or no session is active.
        """
        branch = self._active_branch()
        if not branch:
            return ""
        cur = self._conn.execute(
            "SELECT history_id FROM session_branches "
            "WHERE session_key = ? AND branch_name = ?",
            (self._session_key, branch),
        )
        row = cur.fetchone()
        if not row:
            return ""
        history_id: int = row["history_id"]
        while True:
            entry = self._get_history_entry(history_id)
            if entry.kind == "cd":
                return entry.title
            if entry.parent_id is None:
                return ""
            history_id = entry.parent_id

    def resolve_path(self, path: str) -> str:
        """Resolve a sandbox path against the session cwd.

        Relative paths are joined with cwd.  The result is always
        normalised.  Returns cwd (or ``/``) for empty input.
        """
        if not path:
            return self.get_cwd() or "/"
        if not PurePosixPath(path).is_absolute():
            cwd = self.get_cwd() or "/"
            path = cwd.rstrip("/") + "/" + path
        return posixpath.normpath(path)

    def set_cwd(self, cwd: str) -> None:
        """Store the cwd as a ``kind='cd'`` entry in session history."""
        s = self.session
        if s is None:
            return  # No active session — nothing to persist
        self.set_image(s.current_image, kind="cd", title=cwd)

    def load_shell_history(self) -> list[str]:
        """Return shell history lines for this session, oldest first."""
        cur = self._conn.execute(
            "SELECT line FROM shell_history WHERE session_key = ? ORDER BY id",
            (self._session_key,),
        )
        return [row["line"] for row in cur.fetchall()]

    def add_shell_history(self, line: str) -> None:
        """Append a single line to the shell history."""
        self._conn.execute(
            "INSERT INTO shell_history (session_key, line) VALUES (?, ?)",
            (self._session_key, line),
        )
        self._conn.commit()

    def trim_shell_history(self) -> None:
        """Delete the oldest entries that exceed the maximum."""
        self._conn.execute(
            "DELETE FROM shell_history WHERE id IN ("
            "  SELECT id FROM shell_history "
            "  WHERE session_key = ? "
            "  ORDER BY id "
            "  LIMIT MAX(0, ("
            "    SELECT COUNT(*) FROM shell_history "
            "    WHERE session_key = ?"
            "  ) - ?)"
            ")",
            (
                self._session_key,
                self._session_key,
                self.MAX_SHELL_HISTORY,
            ),
        )
        self._conn.commit()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Session environment variables ────────────────────

    def get_env(self) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM session_env WHERE session_key = ? ORDER BY key",
            (self._session_key,),
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_env(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO session_env"
            " (session_key, key, value) VALUES (?, ?, ?)",
            (self._session_key, key, value),
        )
        self._conn.commit()

    def unset_env(self, *keys: str) -> None:
        for key in keys:
            self._conn.execute(
                "DELETE FROM session_env WHERE session_key = ? AND key = ?",
                (self._session_key, key),
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def get_session_key(profile_name: str, override: str | None = None) -> str:
    if override:
        return override

    env = os.environ.get("CONTREE_SESSION")
    if env:
        return env

    base = Path.cwd().name or "session"
    ppid = os.getppid()
    try:
        tty = os.ttyname(sys.stdin.fileno())
        tty_part = tty.replace("/", "_")
    except (OSError, AttributeError):
        tty_part = "notty"
    stable = uuid.uuid5(uuid.NAMESPACE_OID, f"{profile_name}_{ppid}_{tty_part}")
    suffix = stable.hex[:8]
    return f"{base}+{suffix}"
