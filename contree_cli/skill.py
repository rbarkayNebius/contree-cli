from __future__ import annotations

import abc
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import contree_cli.config as config_mod

SKILL_NAME = "contree"
SKILL_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS installed_skills (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@cache
def skill_body_template() -> str:
    path = Path(__file__).resolve().parent / "skill_body.md"
    return path.read_text(encoding="utf-8")


def skill_version() -> str:
    try:
        from importlib.metadata import version

        return version("contree-cli")
    except Exception:
        return "unknown"


def parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in v.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def default_claude_home() -> Path:
    return Path.home() / ".claude"


def default_agents_home() -> Path:
    return Path.home() / ".agents"


# ── registry ─────────────────────────────────────────────


@contextmanager
def connect_registry() -> Iterator[sqlite3.Connection]:
    db_path = config_mod.CONTREE_HOME / "cli" / "skills.db"
    config_mod.prepare_private_db(db_path)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SKILL_REGISTRY_SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def normalize_install_path(path: Path) -> str:
    return str(path.expanduser())


def prune_empty_parents(path: Path) -> None:
    """Remove empty skill-layout parents (`skills`, `agents`, dot-dirs) of path.

    Stops at the first directory that is not part of a skill layout or is
    not empty, so project and home directories are never touched.
    """
    for directory in path.parents:
        name = directory.name
        if name not in {"skills", "agents"} and not name.startswith("."):
            return
        try:
            directory.rmdir()
        except OSError:
            return


def remember_installed(skill: Skill) -> None:
    normalized = normalize_install_path(skill.path)
    with connect_registry() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO installed_skills (path, kind, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (normalized, skill.kind),
        )


def forget_installed(skill: Skill) -> None:
    normalized = normalize_install_path(skill.path)
    with connect_registry() as conn:
        conn.execute("DELETE FROM installed_skills WHERE path = ?", (normalized,))


# ── text constants ───────────────────────────────────────


SKILL_DESCRIPTION = """\
Use when the user needs to operate ConTree sandboxes through the contree CLI: \
choose or resume explicit sessions, run commands in VM-isolated environments, \
inspect and search image files without spawning a VM, stage file changes, \
branch or roll back sandbox state, reuse tagged images, or automate \
rollback-safe agent workflows.\
"""

BUNDLED_INTRO = """\
This skill is installed by `contree skill install` (version `{version}`). \
It uses the `contree` CLI from PATH.\
"""

CODEX_SANDBOX = """\

## Codex Sandbox

`contree` needs network access and write access to its data directory, \
which resolved to `{contree_home}` when this skill was installed.

Codex config for that directory:

```toml
[sandbox_workspace_write]
network_access = true
writable_roots = ["{contree_home}"]
```

If `CONTREE_HOME` or `XDG_CONFIG_HOME` changes, the writable root must \
point at the newly resolved ConTree data directory. Without this, the CLI \
can fail with `sqlite3.OperationalError`. If the sandbox cannot be \
configured, stop and ask the user.
"""

BUNDLED_FALLBACK = """\

## Quick reference

```bash
# 1) Discover what images are available -- do NOT assume a tag exists.
contree images --prefix ubuntu         # narrow listing (preferred)
# Fallback for when you don't know the prefix shape -- much slower:
# contree -o plain images | grep -i ubuntu

# 2) Bootstrap a session against a tag actually present in the listing.
contree -S <key> use <image-or-tag-from-list>

# 3) Run plain executables in direct mode; reach for -s only for
#    pipes / redirects / && / ; / variable expansion.
contree -S <key> run -- true
contree -S <key> run -- uname -a
```\
"""

SUBAGENT_DESCRIPTION = """\
Use proactively when the user needs to operate ConTree sandboxes through the \
contree CLI, especially for explicit session management, rollback-safe \
execution, image inspection, file staging, branching, and tagged environment \
reuse.\
"""

SUBAGENT_INTRO = """\
This Claude Code subagent is installed by `contree skill install` in \
Claude-compatible Markdown format. It assumes the `contree` CLI is installed \
and available on `PATH`.\
"""

AGENT_DESCRIPTION = (
    "Use proactively when the user needs sandboxed execution "
    "through ConTree: sessions, images, run, rollback, tagging."
)

AGENT_TEMPLATE = """\
---
name: {name}
description: >-
  {description}
tools: Bash, Read, Grep
skills:
  - contree
---

ConTree sandbox agent. Preloads the `contree` skill for all operations.

When there are multiple approaches to a task, launch separate subagents
with isolated contree sessions (`-S <unique_key>`) for each approach,
then compare results.

Run `contree agent` for the full built-in manual.
Run `contree agent <topic>` for details on a specific topic.
"""

OPENAI_DESCRIPTION = """\
Operate ConTree sandboxes through the contree CLI\
"""

OPENAI_TEMPLATE = """\
interface:
  display_name: "ConTree"
  short_description: "{description}"
  default_prompt: "Use ${name} to operate ConTree with explicit sessions, \
read-only inspection first, and small rollback-safe sandbox steps."
"""

CODEX_RULES = 'prefix_rule(pattern=["contree"], decision="allow")\n'


# ── Skill base ───────────────────────────────────────────


@dataclass(frozen=True)
class Skill(abc.ABC):
    """Agent-tool skill with a resolved install path."""

    path: Path

    name: str = SKILL_NAME
    kind: str = ""

    @property
    def allowed_tools(self) -> str:
        return "Bash(contree:*)"

    @property
    def description(self) -> str:
        return SKILL_DESCRIPTION

    def intro(self) -> str:
        return BUNDLED_INTRO.format(version=skill_version())

    def sandbox(self) -> str:
        return ""

    def fallback(self) -> str:
        return BUNDLED_FALLBACK

    def frontmatter(self) -> str:
        return f'---\nname: "{self.name}"\ndescription: "{self.description}"\n---\n'

    def body(self) -> str:
        return skill_body_template().format(
            intro=self.intro(),
            sandbox=self.sandbox(),
            fallback=self.fallback(),
        )

    def render(self) -> str:
        """Generate SKILL.md content (frontmatter + body)."""
        fm = self.frontmatter()
        return fm + "\n" + self.body() if fm else self.body()

    def openai_yaml(self) -> str:
        return OPENAI_TEMPLATE.format(description=OPENAI_DESCRIPTION, name=SKILL_NAME)

    @classmethod
    @abc.abstractmethod
    def home_dir(cls) -> Path: ...

    @classmethod
    def skills_dir(cls) -> Path:
        return cls.home_dir() / "skills"

    @classmethod
    def project_path(cls, root: Path) -> Path:
        return root / f".{cls.kind}" / "skills" / SKILL_NAME

    @classmethod
    def global_path(cls) -> Path:
        return cls.skills_dir() / SKILL_NAME

    @classmethod
    def resolve_path(cls, hint: str) -> Path:
        if not hint:
            return cls.project_path(Path()).resolve()
        if hint == "~":
            return cls.global_path()
        path = Path(hint).expanduser().resolve()
        if is_skill_target(path):
            return path
        return cls.project_path(path)

    @property
    def installed_version(self) -> str:
        vf = self.path / ".version"
        if vf.is_file():
            return vf.read_text(encoding="utf-8").strip()
        return ""

    @property
    def needs_upgrade(self) -> bool:
        v = self.installed_version
        if not v:
            return True
        return parse_version(v) < parse_version(skill_version())

    @property
    def exists(self) -> bool:
        return (self.path / "SKILL.md").is_file()

    def owned_files(self) -> tuple[Path, ...]:
        """Files this install writes; the only paths remove() may touch."""
        return (
            self.path / "SKILL.md",
            self.path / ".version",
            self.path / "agents" / "openai.yaml",
        )

    def install(self, *, force: bool = False) -> None:
        if not force and any(f.is_file() for f in self.owned_files()):
            raise FileExistsError(self.path)
        self.build_tree(self.path)

    def build_tree(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        agents = dest / "agents"
        agents.mkdir(parents=True, exist_ok=True)

        (dest / ".version").write_text(skill_version(), encoding="utf-8")
        (dest / "SKILL.md").write_text(self.render(), encoding="utf-8")
        (agents / "openai.yaml").write_text(self.openai_yaml(), encoding="utf-8")

    def remove(self) -> None:
        for file in self.owned_files():
            if file.is_file():
                file.unlink()
        for directory in (self.path / "agents", self.path):
            with suppress(OSError):
                directory.rmdir()
        prune_empty_parents(self.path)

    def __hash__(self) -> int:
        return hash((self.kind, self.path))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Skill):
            return NotImplemented
        return self.kind == other.kind and self.path == other.path


# ── Concrete skill types ─────────────────────────────────


@dataclass(frozen=True)
class ClaudeSkill(Skill):
    kind: str = "claude"

    @classmethod
    def home_dir(cls) -> Path:
        return default_claude_home()

    def frontmatter(self) -> str:
        return (
            "---\n"
            f'name: "{self.name}"\n'
            f'description: "{self.description}"\n'
            f"allowed-tools: {self.allowed_tools}\n"
            "---\n"
        )


@dataclass(frozen=True)
class CodexSkill(Skill):
    """Codex reads skills from `.agents/skills` trees; rules stay in CODEX_HOME."""

    kind: str = "codex"

    @classmethod
    def home_dir(cls) -> Path:
        return default_codex_home()

    @classmethod
    def skills_dir(cls) -> Path:
        return default_agents_home() / "skills"

    @classmethod
    def project_path(cls, root: Path) -> Path:
        return root / ".agents" / "skills" / SKILL_NAME

    @classmethod
    def rules_path(cls) -> Path:
        return cls.home_dir() / "rules" / f"{SKILL_NAME}.rules"

    def sandbox(self) -> str:
        home = config_mod.CONTREE_HOME
        try:
            display = "~/" + home.relative_to(Path.home()).as_posix()
        except ValueError:
            display = home.as_posix()
        return CODEX_SANDBOX.format(contree_home=display)

    def install(self, *, force: bool = False) -> None:
        super().install(force=force)
        rules = self.rules_path()
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text(CODEX_RULES, encoding="utf-8")

    def remove(self) -> None:
        super().remove()
        rules = self.rules_path()
        if rules.exists():
            rules.unlink()


@dataclass(frozen=True)
class OpenCodeSkill(Skill):
    kind: str = "opencode"

    @classmethod
    def home_dir(cls) -> Path:
        raw = os.environ.get("OPENCODE_HOME")
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".config" / "opencode"


@dataclass(frozen=True)
class AmpSkill(Skill):
    kind: str = "amp"

    @classmethod
    def home_dir(cls) -> Path:
        return Path.home() / ".config" / "agents"


@dataclass(frozen=True)
class ClineSkill(Skill):
    kind: str = "cline"

    @classmethod
    def home_dir(cls) -> Path:
        raw = os.environ.get("CLINE_DIR")
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".cline"


@dataclass(frozen=True)
class ClaudeSubagentSkill(Skill):
    kind: str = "claude-subagent"

    @classmethod
    def home_dir(cls) -> Path:
        return default_claude_home()

    @classmethod
    def project_path(cls, root: Path) -> Path:
        return root / ".claude" / "agents" / f"{SKILL_NAME}-subagent.md"

    @classmethod
    def global_path(cls) -> Path:
        return cls.home_dir() / "agents" / f"{SKILL_NAME}-subagent.md"

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def description(self) -> str:
        return SUBAGENT_DESCRIPTION

    def intro(self) -> str:
        return SUBAGENT_INTRO

    def fallback(self) -> str:
        return ""

    def frontmatter(self) -> str:
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            "tools: Bash, Read, Grep\n"
            "---\n"
        )

    def install(self, *, force: bool = False) -> None:
        if self.path.exists() and not force:
            raise FileExistsError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")

    def remove(self) -> None:
        self.path.unlink()
        prune_empty_parents(self.path)


@dataclass(frozen=True)
class ClaudeAgentSkill(Skill):
    kind: str = "claude-agent"

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @classmethod
    def home_dir(cls) -> Path:
        return default_claude_home()

    @classmethod
    def project_path(cls, root: Path) -> Path:
        return root / ".claude" / "agents" / f"{SKILL_NAME}.md"

    @classmethod
    def global_path(cls) -> Path:
        return cls.home_dir() / "agents" / f"{SKILL_NAME}.md"

    def render(self) -> str:
        return AGENT_TEMPLATE.format(
            name=self.name,
            description=AGENT_DESCRIPTION,
        )

    def install(self, *, force: bool = False) -> None:
        if self.path.exists() and not force:
            raise FileExistsError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")

    def remove(self) -> None:
        self.path.unlink()
        prune_empty_parents(self.path)


# ── Skill type registry ──────────────────────────────────

ALL_SKILL_TYPES: list[type[Skill]] = [
    CodexSkill,
    ClaudeSkill,
    OpenCodeSkill,
    AmpSkill,
    ClineSkill,
    ClaudeSubagentSkill,
    ClaudeAgentSkill,
]

SKILL_BY_KIND: dict[str, type[Skill]] = {cls.kind: cls for cls in ALL_SKILL_TYPES}

PATH_MARKERS: dict[str, type[Skill]] = {
    ".claude": ClaudeSkill,
    ".codex": CodexSkill,
    ".agents": CodexSkill,
    "opencode": OpenCodeSkill,
    "agents": AmpSkill,
    ".cline": ClineSkill,
}


def is_skill_target(path: Path) -> bool:
    """True when path points at a skill artifact rather than a project root.

    Only signals of the artifact itself count (`SKILL.md` inside, an
    `.md` file, or the skill basename sitting directly under a
    `skills/` directory as every install layout produces); the other
    directories the path goes through do not, so a project living
    inside `.claude`, `.codex`, or `.agents` still expands as a
    project root. Requiring the `skills/` parent also keeps an
    unrelated project directory that merely happens to be named
    `contree` from being mistaken for the artifact itself.
    """
    if path.suffix == ".md":
        return True
    if path.name == SKILL_NAME and path.parent.name == "skills":
        return True
    return (path / "SKILL.md").is_file()


def skill_from_spec(spec: str) -> Skill:
    """Parse a spec string into a Skill instance.

    claude:       → ClaudeSkill(path=$PWD/.claude/skills/contree)
    claude:~      → ClaudeSkill(path=~/.claude/skills/contree)
    claude:DIR    → ClaudeSkill(path=DIR/.claude/skills/contree)
    ./path        → guessed class with explicit path
    """
    if ":" in spec:
        kind, hint = spec.split(":", 1)
        skill_cls = SKILL_BY_KIND.get(kind)
        if skill_cls is not None:
            return skill_cls(path=skill_cls.resolve_path(hint))
    path = Path(spec).expanduser().resolve()
    return guess_skill(path)


def skills_from_spec(spec: str) -> tuple[Skill, ...]:
    """Parse a CLI spec into one or more Skill instances.

    kind:HINT specs and paths that point at a skill artifact resolve to a
    single skill. Any other directory path is a project root: it expands
    to every kind at its project location under that root.
    """
    if spec.split(":", 1)[0] in SKILL_BY_KIND:
        return (skill_from_spec(spec),)
    path = Path(spec).expanduser().resolve()
    if is_skill_target(path):
        return (guess_skill(path),)
    return tuple(project_install_specs(path))


CLAUDE_SKILL_TYPES: frozenset[type[Skill]] = frozenset(
    {ClaudeSkill, ClaudeAgentSkill, ClaudeSubagentSkill}
)


def default_install_specs() -> Iterable[Skill]:
    """Return skill instances for default global install.

    Claude-based types require ``~/.claude`` to exist.
    All other types are installed unconditionally.
    """
    specs: list[Skill] = [
        skill_from_spec(f"{cls.kind}:~")
        for cls in ALL_SKILL_TYPES
        if cls not in CLAUDE_SKILL_TYPES
    ]
    if default_claude_home().is_dir():
        for cls in ALL_SKILL_TYPES:
            if cls in CLAUDE_SKILL_TYPES:
                specs.append(skill_from_spec(f"{cls.kind}:~"))
    return specs


def project_install_specs(root: Path) -> Iterable[Skill]:
    """Return skill instances for installing every kind under a project root.

    Same gating as the global default: Claude-based types require
    ``~/.claude`` to exist, all other types are included unconditionally.
    """
    specs: list[Skill] = [
        cls(path=cls.project_path(root))
        for cls in ALL_SKILL_TYPES
        if cls not in CLAUDE_SKILL_TYPES
    ]
    if default_claude_home().is_dir():
        for cls in ALL_SKILL_TYPES:
            if cls in CLAUDE_SKILL_TYPES:
                specs.append(cls(path=cls.project_path(root)))
    return specs


def guess_skill(path: Path) -> Skill:
    """Guess skill type from a raw path."""
    normalized = path.expanduser()
    if normalized.suffix == ".md":
        return ClaudeSubagentSkill(path=normalized)
    for marker, cls in PATH_MARKERS.items():
        if marker in normalized.parts:
            return cls(path=normalized)
    return ClaudeSkill(path=normalized)


def list_installed() -> frozenset[Skill]:
    with connect_registry() as conn:
        rows = conn.execute(
            "SELECT path, kind FROM installed_skills ORDER BY path"
        ).fetchall()
        stale: list[str] = []
        results: list[Skill] = []
        for row in rows:
            kind, path = row["kind"], Path(row["path"])
            if not path.exists():
                stale.append(row["path"])
                continue
            cls = SKILL_BY_KIND.get(kind or "")
            if cls is not None:
                results.append(cls(path=path))
            else:
                results.append(guess_skill(path))
        if stale:
            conn.executemany(
                "DELETE FROM installed_skills WHERE path = ?",
                [(p,) for p in stale],
            )
            conn.commit()
    return frozenset(results)
