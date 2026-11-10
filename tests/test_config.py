"""Tests for timu.toml loading."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path
from typing import Any

import pytest

from timu import provider as providers
from timu.config import (
    DEFAULT_BASE_URL,
    Config,
    ConfigError,
    load_config,
    user_config_path,
)
from timu.provider import ProviderSettings
from timu.provider.fake import FakeProvider
from timu.provider.openai import OpenAIProvider


def builtin(p: object) -> OpenAIProvider:
    assert isinstance(p, OpenAIProvider)
    return p


TOML = """
[provider]
base_url = "http://localhost:8080/v1/"
api_key_env = "MY_KEY"
model = "big"
timeout = 30
extra_body = { cache_control = { type = "ephemeral" } }

[roles.reviewer]
model = "small"

[projects]
roots = ["/p"]
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
    assert config.project_roots == (Path("/p"),)


def test_provider(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, TOML), env={})
    provider = builtin(config.provider("reviewer", env={"MY_KEY": "sk-1"}))
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
    provider = builtin(Config(model="m", api_key_env="").provider("coder", env={}))
    assert provider.model == "m"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[provider]\nmodel = 3\n", "model has the wrong type"),
        ("[provider]\ntimeout = true\n", "timeout has the wrong type"),
        ("provider = 1\n", "provider must be a table"),
        ("[roles]\nreviewer = 'x'\n", "reviewer must be a table"),
        ("[provider\n", "timu.toml"),
        ("[provider]\nmodle = 'x'\n", "provider: unknown keys: modle"),
        ("[project]\nroots = []\n", "unknown keys: project"),
        ("[search]\nkey = 'k'\n", "search: unknown keys: key"),
        ("[roles.reviewr]\nmodel = 'x'\n", "unknown role reviewr; roles: coder,"),
        ("[roles.reviewer]\nmodle = 'x'\n", "roles.reviewer: unknown keys: modle"),
        ("[provider]\ntimeout = 0\n", "timeout must be positive"),
        ("[provider]\ntimeout = -5\n", "timeout must be positive"),
        ("[provider]\ntimeout = nan\n", "timeout must be positive and finite"),
        ("[provider]\ntimeout = inf\n", "timeout must be positive and finite"),
    ],
)
def test_invalid(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text), env={})


def test_user_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    write(tmp_path, "")
    assert user_config_path(env) is None  # ./timu.toml is not the user file
    user = tmp_path / "xdg" / "timu" / "timu.toml"
    user.parent.mkdir(parents=True)
    user.write_text("")
    assert user_config_path(env) == user


def test_workspace_file_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cloned repo's timu.toml must not choose models, roles or where requests go."""
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        '[provider]\nbase_url = "https://attacker.example/v1"\nmodel = "repo"\n'
        '[roles.coder]\nmodel = "repo"\nskills = ["repo"]\n',
    )
    user = tmp_path / "xdg" / "timu" / "timu.toml"
    user.parent.mkdir(parents=True)
    user.write_text(TOML)
    config = load_config(None, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert config.base_url == "http://localhost:8080/v1/"
    assert config.model_for("coder") == "big"
    assert "coder" not in config.role_skills
    assert config.source == str(user)


def test_source_names_env_overrides(tmp_path: Path) -> None:
    path = write(tmp_path, TOML)
    env = {"TIMU_MODEL": "m", "TIMU_BASE_URL": "http://h/v1"}
    config = load_config(path, env=env)
    assert config.source == f"{path} + TIMU_BASE_URL + TIMU_MODEL"


def test_explicit_config_is_trusted(tmp_path: Path) -> None:
    text = '[provider]\nbase_url = "http://h/v1"\n[sandbox]\nextra_read = ["/opt"]\n'
    config = load_config(write(tmp_path, text), env={})
    assert config.base_url == "http://h/v1"


def key_file(tmp_path: Path, text: str = "sk-file\n", mode: int = 0o600) -> Path:
    path = tmp_path / "openrouter.key"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_key_file_wins_over_the_environment(tmp_path: Path) -> None:
    key = key_file(tmp_path)
    config = load_config(
        write(tmp_path, f'[provider]\nmodel = "m"\napi_key_file = "{key}"\n'), env={}
    )
    assert config.api_key_file == key
    provider = builtin(config.provider("coder", env={"OPENROUTER_API_KEY": "sk-env"}))
    assert provider._key == "sk-file"


@pytest.mark.parametrize(
    ("text", "mode", "message"),
    [
        ("sk\n", 0o644, "open to other users; run chmod 600"),
        ("  \n", 0o600, "is empty"),
    ],
)
def test_bad_key_files(tmp_path: Path, text: str, mode: int, message: str) -> None:
    config = Config(model="m", api_key_file=key_file(tmp_path, text, mode))
    with pytest.raises(ConfigError, match=message):
        config.provider("coder", env={})


def test_missing_key_file(tmp_path: Path) -> None:
    config = Config(model="m", api_key_file=tmp_path / "none.key")
    with pytest.raises(ConfigError, match="cannot read key file"):
        config.provider("coder", env={})


def test_search_key_file(tmp_path: Path) -> None:
    key = key_file(tmp_path, "brave-1\n")
    config = load_config(write(tmp_path, f'[search]\napi_key_file = "{key}"\n'), env={})
    assert config.search_tool(env={}) is not None
    with pytest.raises(ConfigError, match="open to other users"):
        Config(search_key_file=key_file(tmp_path, mode=0o640)).search_tool(env={})


def test_key_file_path_expands_home(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, '[provider]\napi_key_file = "~/k"\n'), env={})
    assert config.api_key_file == Path.home() / "k"


# ---- provider types from packages ----


@pytest.fixture
def plugins(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Entry points served from a fake module; returns the module's namespace."""
    seen: dict[str, Any] = {}

    def make(settings: ProviderSettings) -> FakeProvider:
        if settings.options.get("bad"):
            raise ValueError("options.bad is not allowed")
        seen["settings"] = settings
        return FakeProvider([])

    def broken(settings: ProviderSettings) -> FakeProvider:
        raise RuntimeError("sdk exploded")

    module = types.ModuleType("fake_timu_plugin")
    module.__dict__.update(make=make, broken=broken, notcallable=3)
    monkeypatch.setitem(sys.modules, "fake_timu_plugin", module)
    group = providers.ENTRY_POINTS
    eps = EntryPoints(
        [
            EntryPoint("sdk", "fake_timu_plugin:make", group),
            EntryPoint("broken", "fake_timu_plugin:broken", group),
            EntryPoint("notcallable", "fake_timu_plugin:notcallable", group),
            EntryPoint("missing", "fake_timu_plugin_absent:make", group),
            EntryPoint(providers.BUILTIN, "fake_timu_plugin:make", group),
        ]
    )

    def entry_points(*, group: str, name: str | None = None) -> EntryPoints:
        found = eps.select(group=group)
        return found.select(name=name) if name else found

    monkeypatch.setattr(providers, "entry_points", entry_points)
    providers.factory.cache_clear()
    yield seen
    providers.factory.cache_clear()


def test_a_package_provider_gets_its_settings(
    tmp_path: Path, plugins: dict[str, Any]
) -> None:
    """A package's type defaults base_url and api_key_env to "", so its SDK's own
    defaults apply, and receives [provider.options]."""
    text = '[provider]\ntype = "sdk"\nmodel = "m"\noptions = { region = "eu" }\n'
    config = load_config(write(tmp_path, text), env={})
    assert isinstance(config.provider("coder", env={}), FakeProvider)
    assert plugins["settings"] == ProviderSettings(
        "m", "", "", 600, {}, {"region": "eu"}
    )


def test_a_package_provider_reads_a_key_file(
    tmp_path: Path, plugins: dict[str, Any]
) -> None:
    key = key_file(tmp_path)
    config = Config(provider_type="sdk", model="m", api_key_file=key)
    config.provider("coder", env={})
    assert plugins["settings"].api_key == "sk-file"


@pytest.mark.parametrize(
    ("kind", "options", "message"),
    [
        ("nope", {}, "no provider type 'nope'; installed: broken, chat-completions"),
        ("sdk", {"bad": True}, "provider type sdk: options.bad is not allowed"),
        ("broken", {}, "provider type broken: RuntimeError: sdk exploded"),
        ("notcallable", {}, "cannot load: TypeError: provider type 'notcallable'"),
        ("missing", {}, "cannot load: ModuleNotFoundError"),
    ],
)
def test_provider_type_errors(
    plugins: dict[str, Any], kind: str, options: dict[str, Any], message: str
) -> None:
    config = Config(
        provider_type=kind, model="m", api_key_env="", provider_options=options
    )
    with pytest.raises(ConfigError, match=message.replace("(", r"\(")):
        config.provider("coder", env={})


def test_a_package_cannot_replace_the_builtin(plugins: dict[str, Any]) -> None:
    config = Config(model="m", api_key_env="")
    assert isinstance(config.provider("coder", env={}), OpenAIProvider)
    assert "settings" not in plugins


def test_the_builtin_rejects_options() -> None:
    config = Config(model="m", api_key_env="", provider_options={"x": 1})
    with pytest.raises(ConfigError, match="only for provider types from packages"):
        config.provider("coder", env={})


# ---- named providers ----

NAMED = """
[provider]
api_key_env = ""
model = "default-model"

[providers.a]
type = "sdk"
model = "a-model"
options = { region = "eu" }

[providers.b]
type = "sdk"

[roles.reviewer]
provider = "a"

[roles.verifier]
provider = "a"
model = "a-big"

[roles.validator]
provider = "b"
"""


def test_a_role_uses_its_named_provider(
    tmp_path: Path, plugins: dict[str, Any]
) -> None:
    config = load_config(write(tmp_path, NAMED), env={"TIMU_MODEL": "env-model"})
    assert isinstance(config.provider("reviewer", env={}), FakeProvider)
    assert plugins["settings"] == ProviderSettings(
        "a-model", "", "", 600, {}, {"region": "eu"}
    )
    config.provider("verifier", env={})  # a role's own model wins
    assert plugins["settings"].model == "a-big"
    config.provider("verifier", env={}, model="node-model")  # a graph node's model
    assert plugins["settings"].model == "node-model"
    coder = builtin(config.provider("coder", env={}))  # the default, with TIMU_MODEL
    assert coder.model == "env-model"


def test_a_named_provider_does_not_borrow_the_default_model(
    tmp_path: Path, plugins: dict[str, Any]
) -> None:
    config = load_config(write(tmp_path, NAMED), env={})
    assert config.model_for("validator") == ""
    with pytest.raises(
        ConfigError, match="no model; set providers.b.model or roles.validator.model"
    ):
        config.provider("validator", env={})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            '[roles.coder]\nprovider = "x"\n',
            r"roles.coder.provider: no \[providers.x\]; defined: none",
        ),
        ('[providers.x]\nmodels = "m"\n', "providers.x: unknown keys: models"),
        ("[providers.x]\ntimeout = 0\n", "providers.x: timeout must be positive"),
        ("providers = 1\n", "providers must be a table"),
        ("[roles.coder]\nprovider = 1\n", "provider has the wrong type"),
    ],
)
def test_bad_named_providers(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text), env={})


def test_a_named_provider_names_its_table_in_errors(tmp_path: Path) -> None:
    text = '[providers.x]\nmodel = "m"\napi_key_env = "X_KEY"\n[roles.coder]\nprovider = "x"\n'
    config = load_config(write(tmp_path, text), env={})
    with pytest.raises(
        ConfigError, match="X_KEY is not set; .* providers.x.api_key_env"
    ):
        config.provider("coder", env={})


# ---- prices ----


def test_price_fills_a_missing_cost() -> None:
    from timu.provider import Price, Priced
    from timu.provider.base import Reply
    from timu.types import Usage

    unknown = Reply("a", usage=Usage(input_tokens=2_000_000, output_tokens=100_000))
    unknown = replace_reply(unknown, cost_known=False)
    known = Reply("b", usage=Usage(input_tokens=10, cost_usd=0.5))
    inner = FakeProvider([unknown, known])
    inner.model = "m"  # type: ignore[attr-defined]
    priced = Priced(inner, Price(3.0, 15.0))
    first = priced.complete([], [])
    assert first.cost_known and first.usage.cost_usd == pytest.approx(7.5)
    assert priced.complete([], []).usage.cost_usd == 0.5  # the server's cost wins
    assert priced.model == "m"


def replace_reply(reply: Any, **kw: Any) -> Any:
    from dataclasses import replace

    return replace(reply, **kw)


def test_a_price_in_config(tmp_path: Path) -> None:
    from timu.provider import Price, Priced

    text = (
        '[provider]\nmodel = "m"\napi_key_env = ""\nprice = { input = 3, output = 15 }\n'
        '[providers.x]\nmodel = "xm"\napi_key_env = ""\nprice = { input = 0.1, output = 0.4 }\n'
        '[roles.reviewer]\nprovider = "x"\n'
    )
    config = load_config(write(tmp_path, text), env={})
    coder, reviewer = (
        config.provider("coder", env={}),
        config.provider("reviewer", env={}),
    )
    assert isinstance(coder, Priced) and coder.price == Price(3, 15)
    assert isinstance(reviewer, Priced) and reviewer.price == Price(0.1, 0.4)
    assert isinstance(coder.inner, OpenAIProvider) and coder.model == "m"


@pytest.mark.parametrize(
    ("price", "message"),
    [
        ("{ input = 3 }", "price needs input and output"),
        ("{ input = 3, output = -1 }", "prices must be numbers >= 0"),
        ('{ input = "3", output = 1 }', "prices must be numbers >= 0"),
        ("{ input = nan, output = 1 }", "prices must be numbers >= 0"),
        ("{ input = inf, output = 1 }", "prices must be numbers >= 0"),
        ("3", "price must be a table"),
    ],
)
def test_bad_prices(tmp_path: Path, price: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(
            write(tmp_path, f'[provider]\nmodel = "m"\nprice = {price}\n'), env={}
        )


def test_a_cached_input_price() -> None:
    from timu.provider import Price
    from timu.types import Usage

    usage = Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=800_000)
    assert Price(3.0, 15.0, 0.3).cost(usage) == pytest.approx(0.6 + 0.24)
    assert Price(3.0, 15.0).cost(usage) == pytest.approx(3.0)  # no cached rate
    odd = Usage(input_tokens=10, cached_input_tokens=50)  # a server's bad count
    assert Price(1e6, 0, 0).cost(odd) == 0


def test_a_cached_input_price_in_config(tmp_path: Path) -> None:
    from timu.provider import Price, Priced

    text = '[provider]\nmodel = "m"\napi_key_env = ""\n'
    text += "price = { input = 3, output = 15, cached_input = 0.3 }\n"
    provider = load_config(write(tmp_path, text), env={}).provider("coder", env={})
    assert isinstance(provider, Priced) and provider.price == Price(3, 15, 0.3)
    bad = text.replace("cached_input", "cache_read")
    with pytest.raises(ConfigError, match="may set cached_input"):
        load_config(write(tmp_path, bad), env={})


def test_a_cache_write_price_in_config(tmp_path: Path) -> None:
    from timu.provider import Price, Priced

    text = '[provider]\nmodel = "m"\napi_key_env = ""\n'
    text += "price = { input = 4, output = 20, cached_input = 0.2, cache_write = 8 }\n"
    provider = load_config(write(tmp_path, text), env={}).provider("coder", env={})
    assert isinstance(provider, Priced) and provider.price == Price(4, 20, 0.2, 8)
