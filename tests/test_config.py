from pathlib import Path

import pytest

from opsfleet_agent.config import Role, load_config
from opsfleet_agent.errors import ConfigError
from opsfleet_agent.identity import resolve_user

SETTINGS = (Path(__file__).parents[1] / "config" / "settings.yaml").read_text()


def write_config(tmp_path: Path, users_yaml: str) -> Path:
    (tmp_path / "settings.yaml").write_text(SETTINGS)
    (tmp_path / "users.yaml").write_text(users_yaml)
    return tmp_path


def test_project_config_loads():
    cfg = load_config()
    assert cfg.settings.models.default == "gemini-3.5-flash-lite"
    assert {u.id for u in cfg.users.users} == {"ceo", "maya", "alex"}


def test_resolve_known_users():
    cfg = load_config()
    maya = resolve_user(cfg.users, "maya")
    assert maya.role is Role.MANAGER and maya.brands == ("Calvin Klein", "Carhartt")
    ceo = resolve_user(cfg.users, "ceo")
    assert ceo.all_brands and ceo.brands == ()


def test_unknown_user_stops_with_clear_message():
    cfg = load_config()
    with pytest.raises(ConfigError, match="Unknown user 'nobody'.*ceo, maya, alex"):
        resolve_user(cfg.users, "nobody")


def test_manager_without_brands_is_rejected(tmp_path):
    d = write_config(tmp_path, "users:\n  - {id: bob, name: Bob, role: manager}\n")
    with pytest.raises(ConfigError, match="manager without brands"):
        load_config(d, env_file=tmp_path / "missing.env")


def test_duplicate_user_ids_are_rejected(tmp_path):
    d = write_config(
        tmp_path,
        "users:\n  - {id: a, name: A, role: ceo}\n  - {id: a, name: B, role: ceo}\n",
    )
    with pytest.raises(ConfigError, match="unique"):
        load_config(d, env_file=tmp_path / "missing.env")


def test_unknown_setting_is_rejected(tmp_path):
    d = write_config(tmp_path, "users:\n  - {id: a, name: A, role: ceo}\n")
    (d / "settings.yaml").write_text(SETTINGS + "\nsurprise: 1\n")
    with pytest.raises(ConfigError, match="surprise"):
        load_config(d, env_file=tmp_path / "missing.env")
