import configparser
import os
import stat
import sys
from pathlib import Path

import pytest
from contree_client.profiles import (
    AUTH_TYPE_IAM,
    AUTH_TYPE_JWT,
    DEFAULT_IAM_URL,
    PROFILE_PREFIX,
    Profile,
)

import contree_cli.config as config_mod
from contree_cli.config import (
    Config,
    get_default_path,
)

# ---------------------------------------------------------------------------
# save / load via Config
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_save_creates_file(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok123",
            url="https://test.dev",
        )
        assert (config_dir / "auth.ini").exists()

    def test_load_reads_saved_profile(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok123",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.token == "tok123"
        assert p.url == "https://test.dev"
        assert p.name == "default"

    def test_save_multiple_profiles(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = Profile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        p = Config().resolve()
        assert p.token == "tok1"  # default profile active

    def test_save_overwrites_existing(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="old",
            url="https://old.dev",
        )
        cfg["default"] = Profile(
            name="default",
            token="new",
            url="https://new.dev",
        )
        p = Config().resolve()
        assert p.token == "new"
        assert p.url == "https://new.dev"

    def test_load_defaults_when_no_file(self, config_dir):
        p = Config().resolve()
        assert p.name == "default"
        assert p.token is None
        assert p.url == ""
        assert p.auth_type == AUTH_TYPE_JWT


# ---------------------------------------------------------------------------
# Config with explicit path
# ---------------------------------------------------------------------------


class TestLoadConfigPath:
    def test_load_from_explicit_path(self, tmp_path):
        cfg_file = tmp_path / "custom.ini"
        cfg = Config(path=cfg_file)
        cfg["default"] = Profile(
            name="default",
            token="tok_custom",
            url="https://custom.dev",
        )
        p = Config(path=cfg_file).resolve()
        assert p.token == "tok_custom"
        assert p.url == "https://custom.dev"


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


class TestProfileResolution:
    def test_defaults_to_default_profile(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.name == "default"

    def test_uses_switched_profile(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = Profile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        cfg.switch("staging")
        p = Config().resolve()
        assert p.name == "staging"
        assert p.token == "tok2"
        assert p.url == "https://staging.dev"

    def test_env_profile_overrides_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = Profile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        monkeypatch.setenv("CONTREE_PROFILE", "staging")
        p = Config().resolve()
        assert p.name == "staging"
        assert p.token == "tok2"

    def test_env_token_does_not_override_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="cfg_token",
            url="https://test.dev",
        )
        monkeypatch.setenv("CONTREE_TOKEN", "env_token")
        p = Config().resolve()
        assert p.token == "cfg_token"

    def test_env_url_does_not_override_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://custom.dev",
        )
        monkeypatch.setenv("CONTREE_URL", "https://env.dev")
        p = Config().resolve()
        assert p.url == "https://custom.dev"

    def test_url_falls_back_for_jwt_when_missing(self, config_dir):
        """JWT profile with url key removed falls back to empty string."""
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        cp = configparser.ConfigParser()
        cp.read(config_dir / "auth.ini")
        cp.remove_option(PROFILE_PREFIX + "default", "url")
        with open(config_dir / "auth.ini", "w") as f:
            cp.write(f)
        p = Config().resolve()
        assert p.url == ""

    def test_url_falls_back_for_iam_when_missing(self, config_dir):
        """IAM profile with url key removed falls back to IAM default."""
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AUTH_TYPE_IAM,
        )
        cp = configparser.ConfigParser()
        cp.read(config_dir / "auth.ini")
        cp.remove_option(PROFILE_PREFIX + "default", "url")
        with open(config_dir / "auth.ini", "w") as f:
            cp.write(f)
        p = Config().resolve()
        assert p.url == DEFAULT_IAM_URL

    def test_nonexistent_profile_returns_defaults(self, config_dir, monkeypatch):
        monkeypatch.setenv("CONTREE_PROFILE", "nonexistent")
        p = Config().resolve()
        assert p.name == "nonexistent"
        assert p.token is None
        assert p.url == ""
        assert p.auth_type == AUTH_TYPE_JWT


# ---------------------------------------------------------------------------
# Auth type and project
# ---------------------------------------------------------------------------


class TestAuthType:
    def test_default_type_is_jwt(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.auth_type == AUTH_TYPE_JWT

    def test_iam_type_stored_and_loaded(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AUTH_TYPE_IAM,
            project="aiproject-x",
        )
        p = Config().resolve()
        assert p.auth_type == AUTH_TYPE_IAM
        assert p.project == "aiproject-x"

    def test_legacy_profile_without_type_is_jwt(self, config_dir):
        """Profile saved without type key (legacy) defaults to jwt."""
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://old.dev",
        )
        cp = configparser.ConfigParser()
        cp.read(config_dir / "auth.ini")
        cp.remove_option(PROFILE_PREFIX + "default", "type")
        with open(config_dir / "auth.ini", "w") as f:
            cp.write(f)
        p = Config().resolve()
        assert p.auth_type == AUTH_TYPE_JWT

    def test_project_none_when_not_set(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.project is None

    def test_env_project_does_not_override_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AUTH_TYPE_IAM,
            project="aiproject-cfg",
        )
        monkeypatch.setenv("CONTREE_PROJECT", "aiproject-env")
        p = Config().resolve()
        assert p.project == "aiproject-cfg"

    def test_save_clears_project_when_none(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AUTH_TYPE_IAM,
            project="aiproject-old",
        )
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://test.dev",
            auth_type=AUTH_TYPE_JWT,
        )
        p = Config().resolve()
        assert p.project is None


# ---------------------------------------------------------------------------
# ConfigProfile dataclass
# ---------------------------------------------------------------------------


class TestConfigProfileDataclass:
    def test_frozen(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        with pytest.raises(AttributeError):
            p.token = "other"

    def test_repr_masks_token(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="secret_tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        # The library Profile hides the token from repr entirely
        # (field(repr=False)).
        r = repr(p)
        assert "secret_tok" not in r
        assert "token" not in r

    def test_repr_none_token(self, config_dir):
        p = Config().resolve()
        r = repr(p)
        assert "None" in r


# ---------------------------------------------------------------------------
# switch
# ---------------------------------------------------------------------------


class TestSwitchProfile:
    def test_switch_updates_default(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = Profile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        cfg.switch("staging")
        p = Config().resolve()
        assert p.name == "staging"

    def test_switch_nonexistent_raises(self, config_dir):
        cfg = Config()
        with pytest.raises(ValueError, match="does not exist"):
            cfg.switch("nonexistent")


# ---------------------------------------------------------------------------
# auth.ini permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
class TestAuthFilePermissions:
    def test_file_mode_is_0600(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="secret-token",
            url="https://test.dev",
        )
        path = config_dir / "auth.ini"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_rewrite_keeps_0600(self, config_dir):
        cfg = Config()
        cfg["default"] = Profile(
            name="default",
            token="t1",
            url="https://test.dev",
        )
        path = config_dir / "auth.ini"
        os.chmod(path, 0o644)
        cfg["default"] = Profile(
            name="default",
            token="t2",
            url="https://test.dev",
        )
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# auth.ini + cli.ini are read together; auth.ini wins on conflict
# ---------------------------------------------------------------------------


class TestProfileMergeAcrossFiles:
    def test_profile_fields_merge_from_cli_and_auth(self, config_dir, monkeypatch):
        cli_path = config_dir / "cli.ini"
        cli_path.parent.mkdir(parents=True, exist_ok=True)
        cli_path.write_text(
            "[profile:default]\nurl = https://from-cli.dev\nproject = aiproject-cli\n"
        )
        monkeypatch.setattr(config_mod, "CLI_CONFIG_FILE", cli_path)

        auth_path = config_dir / "auth.ini"
        auth_path.write_text(
            "[DEFAULT]\nprofile = default\n[profile:default]\ntoken = secret-tok\n"
        )

        p = Config().resolve()
        assert p.token == "secret-tok"
        assert p.url == "https://from-cli.dev"
        assert p.project == "aiproject-cli"

    def test_auth_overrides_cli_on_conflict(self, config_dir, monkeypatch):
        cli_path = config_dir / "cli.ini"
        cli_path.parent.mkdir(parents=True, exist_ok=True)
        cli_path.write_text("[profile:default]\nurl = https://from-cli.dev\n")
        monkeypatch.setattr(config_mod, "CLI_CONFIG_FILE", cli_path)

        auth_path = config_dir / "auth.ini"
        auth_path.write_text(
            "[DEFAULT]\nprofile = default\n"
            "[profile:default]\n"
            "url = https://from-auth.dev\n"
            "token = tok\n"
        )

        p = Config().resolve()
        assert p.url == "https://from-auth.dev"


# ---------------------------------------------------------------------------
# default CONTREE_HOME respects XDG_CONFIG_HOME
# ---------------------------------------------------------------------------


class TestDefaultContreeHome:
    def test_uses_xdg_config_home_when_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTREE_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        xdg = get_default_path("XDG_CONFIG_HOME", "~/.config")
        home = get_default_path("CONTREE_HOME", xdg / "contree")
        assert home == tmp_path / "xdg" / "contree"

    def test_falls_back_to_dot_config_when_xdg_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTREE_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        xdg = get_default_path("XDG_CONFIG_HOME", "~/.config")
        home = get_default_path("CONTREE_HOME", xdg / "contree")
        assert home == tmp_path / ".config" / "contree"

    def test_contree_home_overrides_xdg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTREE_HOME", str(tmp_path / "explicit"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        xdg = get_default_path("XDG_CONFIG_HOME", "~/.config")
        home = get_default_path("CONTREE_HOME", xdg / "contree")
        assert home == tmp_path / "explicit"


# ---------------------------------------------------------------------------
# session_db_path respects CONTREE_SESSION_DB
# ---------------------------------------------------------------------------


class TestSessionDbPath:
    def test_default_path_is_per_profile(self, config_dir, monkeypatch):
        monkeypatch.delenv("CONTREE_SESSION_DB", raising=False)
        path = config_mod.session_db_path("default")
        assert path == config_mod.CONTREE_HOME / "cli" / "sessions" / "default.db"

    def test_env_var_overrides_computed_path(self, config_dir, tmp_path, monkeypatch):
        override = tmp_path / "custom.db"
        monkeypatch.setenv("CONTREE_SESSION_DB", str(override))
        assert config_mod.session_db_path("default") == override

    def test_env_var_expands_user(self, config_dir, monkeypatch):
        monkeypatch.setenv("CONTREE_SESSION_DB", "~/custom-session.db")
        assert (
            config_mod.session_db_path("default")
            == Path("~/custom-session.db").expanduser()
        )


# ---------------------------------------------------------------------------
# CONTREE_HOME ownership checks
# ---------------------------------------------------------------------------

needs_root = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="chown to another uid requires root",
)


class TestHomeOwnership:
    def test_home_owned_by_other_user_rejected(self, config_dir, monkeypatch):
        config_mod.CONTREE_HOME.mkdir(parents=True)
        monkeypatch.setattr(config_mod, "_euid", lambda: os.geteuid() + 1000)
        with pytest.raises(config_mod.InsecurePathError, match="owned by uid"):
            Config()

    @needs_root
    def test_foreign_auth_ini_rejected(self, config_dir):
        config_dir.mkdir(parents=True)
        auth = config_dir / "auth.ini"
        auth.write_text("[profile:default]\nurl = https://evil.example\n")
        os.chown(auth, 65534, 65534)
        with pytest.raises(config_mod.InsecurePathError, match="owned by uid"):
            Config()

    @needs_root
    def test_foreign_cli_ini_rejected(self, config_dir):
        config_dir.mkdir(parents=True)
        cli = config_dir / "cli.ini"
        cli.write_text("[profile:default]\nurl = https://evil.example\n")
        os.chown(cli, 65534, 65534)
        with pytest.raises(config_mod.InsecurePathError, match="owned by uid"):
            Config()

    def test_own_group_writable_home_is_tightened(self, config_dir):
        home = config_mod.CONTREE_HOME
        home.mkdir(parents=True)
        os.chmod(home, 0o770)
        Config()
        assert stat.S_IMODE(os.stat(home).st_mode) & 0o022 == 0

    def test_world_writable_home_rejected(self, config_dir):
        home = config_mod.CONTREE_HOME
        home.mkdir(parents=True)
        os.chmod(home, 0o777)
        with pytest.raises(config_mod.InsecurePathError, match="writable by other"):
            Config()

    def test_world_writable_cli_ini_rejected(self, config_dir):
        config_dir.mkdir(parents=True)
        cli = config_dir / "cli.ini"
        cli.write_text("[cli]\neditor = touch /tmp/pwned\n")
        os.chmod(cli, 0o666)
        with pytest.raises(config_mod.InsecurePathError, match="writable by other"):
            Config()

    def test_home_in_writable_non_sticky_parent_rejected(self, tmp_path, monkeypatch):
        parent = tmp_path / "shared"
        parent.mkdir()
        os.chmod(parent, 0o777)
        monkeypatch.setattr(config_mod, "CONTREE_HOME", parent / "home")
        with pytest.raises(config_mod.InsecurePathError, match="parent of"):
            Config(parent / "home" / "auth.ini")

    @needs_root
    def test_import_time_settings_skip_foreign_cli_ini(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(mode=0o700)
        cli = home / "cli.ini"
        cli.write_text("[cli]\neditor = touch /tmp/pwned\n")
        os.chown(cli, 65534, 65534)
        monkeypatch.setattr(config_mod, "CONTREE_HOME", home)
        monkeypatch.setattr(config_mod, "CLI_CONFIG_FILE", cli)
        monkeypatch.setattr(config_mod, "CONFIG_FILE", home / "auth.ini")
        settings, err = config_mod._load_settings()
        assert err is not None
        assert not settings.has_section("cli")

    @needs_root
    def test_foreign_intermediate_dir_rejected(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(mode=0o700)
        evil = tmp_path / "evil"
        evil.mkdir()
        os.chown(evil, 65534, 65534)
        (home / "cli").symlink_to(evil)
        monkeypatch.setattr(config_mod, "CONTREE_HOME", home)
        with pytest.raises(config_mod.InsecurePathError, match="owned by uid"):
            config_mod.prepare_private_db(home / "cli" / "sessions" / "x.db")
        assert not (evil / "sessions").exists()

    def test_home_created_private(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(config_mod, "CONTREE_HOME", home)
        cfg = Config(home / "auth.ini")
        cfg["default"] = Profile(name="default", token="t", url="https://x.dev")
        assert stat.S_IMODE(os.stat(home).st_mode) == 0o700

    def test_save_replaces_symlink_instead_of_following(self, config_dir):
        config_dir.mkdir(parents=True)
        victim = config_dir.parent / "victim.txt"
        victim.write_text("[keep]\nme = 1\n")
        auth = config_dir / "auth.ini"
        auth.symlink_to(victim)
        cfg = Config()
        cfg["default"] = Profile(name="default", token="t", url="https://x.dev")
        assert victim.read_text() == "[keep]\nme = 1\n"
        assert not auth.is_symlink()
        assert stat.S_IMODE(os.stat(auth).st_mode) == 0o600

    def test_dangling_symlink_rejected(self, config_dir):
        config_dir.mkdir(parents=True)
        (config_dir / "auth.ini").symlink_to(config_dir / "missing")
        with pytest.raises(config_mod.InsecurePathError, match="dangling"):
            Config()

    def test_check_home_reports_import_time_error(self, monkeypatch):
        err = config_mod.InsecurePathError("/tmp/x is owned by uid 65534")
        monkeypatch.setattr(config_mod, "HOME_ERROR", err)
        with pytest.raises(config_mod.InsecurePathError, match="uid 65534"):
            config_mod.check_home()


class TestPreparePrivateDb:
    def test_creates_db_with_0600(self, tmp_path):
        db = tmp_path / "sessions" / "default.db"
        config_mod.prepare_private_db(db)
        assert stat.S_IMODE(os.stat(db).st_mode) == 0o600

    def test_creates_every_home_level_0700(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(config_mod, "CONTREE_HOME", home)
        config_mod.prepare_private_db(home / "cli" / "sessions" / "x.db")
        for d in (home, home / "cli", home / "cli" / "sessions"):
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700

    def test_symlinked_wal_rejected(self, tmp_path):
        db = tmp_path / "default.db"
        (tmp_path / "default.db-wal").symlink_to(tmp_path / "nowhere")
        with pytest.raises(config_mod.InsecurePathError):
            config_mod.prepare_private_db(db)

    @needs_root
    def test_foreign_db_rejected(self, tmp_path):
        db = tmp_path / "default.db"
        db.write_bytes(b"")
        os.chown(db, 65534, 65534)
        with pytest.raises(config_mod.InsecurePathError, match="owned by uid"):
            config_mod.prepare_private_db(db)
