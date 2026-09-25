"""Tests for timu.toml loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from timu.config import DEFAULT_BASE_URL, Config, ConfigError, config_path, load_config

TOML = """
[provider]
base_url = "http://localhost:8080/v1/"
api_key_env = "MY_KEY"
model = "big"
timeout = 30
extra_body = { cache_control = { type = "ephemeral" } }

[roles.reviewer]
model = "small"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "timu.toml"
    path.write_text(text)
    return path


def test_load(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, TOML), env={})
    assert config.base_url == "http://localhost:8080/v1/"
    assert config.api_key_env == "MY_KEY"
    assert config.timeout == 30.0
    assert config.extra_body == {"cache_control": {"type": "ephemeral"}}
    assert config.model_for("coder") == "big"
    assert config.model_for("reviewer") == "small"


def test_provider(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, TOML), env={})
    provider = config.provider("reviewer", env={"MY_KEY": "sk-1"})
    assert provider.url == "http://localhost:8080/v1/chat/completions"
    assert provider.model == "small"
    assert provider.extra_body == {"cache_control": {"type": "ephemeral"}}
    assert provider.timeout == 30.0


def test_env_overrides(tmp_path: Path) -> None:
    env = {"TIMU_MODEL": "env-model", "TIMU_BASE_URL": "http://h/v1"}
    config = load_config(write(tmp_path, TOML), env=env)
    assert (config.model, config.base_url) == ("env-model", "http://h/v1")
    assert config.model_for("reviewer") == "small"  # a role's own model still wins


def test_defaults_without_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no ./timu.toml
    config = load_config(None, env={"HOME": "/nonexistent"})
    assert config.base_url == DEFAULT_BASE_URL
    assert config.source == "defaults"


def test_missing_model() -> None:
    with pytest.raises(ConfigError, match="no model"):
        Config().provider("coder", env={"OPENROUTER_API_KEY": "k"})


def test_missing_key() -> None:
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY is not set"):
        Config(model="m").provider("coder", env={})


def test_keyless_server() -> None:
    provider = Config(model="m", api_key_env="").provider("coder", env={})
    assert provider.model == "m"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[provider]\nmodel = 3\n", "model has the wrong type"),
        ("[provider]\ntimeout = true\n", "timeout has the wrong type"),
        ("provider = 1\n", "provider must be a table"),
        ("[roles]\nreviewer = 'x'\n", "reviewer must be a table"),
        ("[provider\n", "timu.toml"),
    ],
)
def test_invalid(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text), env={})


def test_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    user = tmp_path / "xdg" / "timu" / "timu.toml"
    user.parent.mkdir(parents=True)
    user.write_text("")
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert config_path(env) == user
    write(tmp_path, "")
    assert config_path(env) == Path("timu.toml")  # the local file wins
