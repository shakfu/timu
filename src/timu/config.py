"""timu.toml: which model API to call, and which model each role uses.

    [provider]
    type = "chat-completions"            # optional; or a type an installed package
                                         # registers (timu.provider)
    base_url = "https://openrouter.ai/api/v1"
    api_key_env = "OPENROUTER_API_KEY"   # "" for a local server without a key
    api_key_file = "~/.config/timu/openrouter.key"   # optional; wins over api_key_env
    model = "<model id>"
    timeout = 600                        # seconds, optional
    extra_body = { cache_control = { type = "ephemeral" } }   # optional
    options = { }                        # optional; for a package's provider type
    price = { input = 3.0, output = 15.0, cached_input = 0.3, cache_write = 3.75 }
                                         # optional; USD per million tokens, for a
                                         # server that reports no cost; the cache
                                         # rates are optional

For a type from a package, base_url and api_key_env default to "": the package's
own defaults apply.

    [providers.anthropic]                # optional; named, with [provider]'s keys
    type = "anthropic"
    model = "<model id>"                 # the model of the roles that use it

    [roles.reviewer]                     # also coder, researcher, lead,
    model = "<cheaper model id>"         # validator and verifier
    provider = "anthropic"               # optional; default [provider]
    skills = ["code-review"]             # optional

A role's model is its own, else its provider's. A role on a named provider never
falls back to [provider].model: that is another API's model id.

    [search]
    api_key_env = "BRAVE_API_KEY"        # Brave Search; the default (plan D3)
    api_key_file = "~/.config/timu/brave.key"        # optional; wins over api_key_env

    [sandbox]
    extra_read = ["~/.local/share/uv"]   # toolchains under $HOME (interim, design 15.8)

    [projects]
    roots = ["~/projects"]               # where graph nodes find projects (graph.md 7.1)

The user file is $XDG_CONFIG_HOME/timu/timu.toml (default ~/.config/timu/timu.toml),
or the file passed with --config. A workspace timu.toml is never read: models, roles
and request settings are the user's choice, not the repo's. TIMU_BASE_URL and
TIMU_MODEL override the file.

A sandboxed command can read the environment of the user's processes on macOS
(sandbox.py), but not files under $HOME. A key file there, mode 600, keeps the key
from commands; a key in the environment does not.
"""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from timu.provider import BUILTIN, Price, Priced, Provider, ProviderSettings, factory
from timu.roles import ROLE_NAMES
from timu.tool import Tool
from timu.tools.web import web_search_tool

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"


class ConfigError(ValueError):
    """An invalid or incomplete configuration. The message names the file and key."""


@dataclass(frozen=True)
class ProviderConfig:
    """One provider table: [provider] or a [providers.NAME]."""

    type: str = BUILTIN
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_KEY_ENV
    api_key_file: Path | None = None
    model: str = ""
    timeout: float = 600
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    price: Price | None = None

    def build(
        self, model: str, env: Mapping[str, str], source: str, name: str
    ) -> Provider:
        """A provider at model. Errors name source and the table name. Raises
        ConfigError."""
        key = ""
        if self.api_key_file:
            key = read_key(self.api_key_file)
        elif self.api_key_env:
            key = env.get(self.api_key_env, "")
            if not key:
                raise ConfigError(
                    f"{self.api_key_env} is not set; set it, or set {name}.api_key_env "
                    f'to "" for a server without a key'
                )
        settings = ProviderSettings(
            model, self.base_url, key, self.timeout, self.extra_body, self.options
        )
        where = f"{source}: {name} type {self.type}"
        try:
            make = factory(self.type)
        except LookupError as e:
            raise ConfigError(f"{where}: {e}") from None
        except Exception as e:  # noqa: BLE001 - a package's import may raise anything
            raise ConfigError(
                f"{where}: cannot load: {type(e).__name__}: {e}"
            ) from None
        try:
            provider = make(settings)
        except ValueError as e:
            raise ConfigError(f"{where}: {e}") from None
        except Exception as e:  # noqa: BLE001 - a package's factory may raise anything
            raise ConfigError(f"{where}: {type(e).__name__}: {e}") from None
        return Priced(provider, self.price) if self.price else provider


@dataclass(frozen=True)
class Config:
    """The flat provider fields are the [provider] table, the default."""

    provider_type: str = BUILTIN
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_KEY_ENV
    api_key_file: Path | None = None
    model: str = ""
    timeout: float = 600
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    provider_options: Mapping[str, Any] = field(default_factory=dict)
    price: Price | None = None
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)  # named
    role_models: Mapping[str, str] = field(default_factory=dict)
    role_providers: Mapping[str, str] = field(default_factory=dict)  # role -> name
    role_skills: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    extra_read: tuple[Path, ...] = ()
    project_roots: tuple[Path, ...] = ()
    search_key_env: str = "BRAVE_API_KEY"
    search_key_file: Path | None = None
    source: str = "defaults"  # the file and env overrides read

    def model_for(self, role: str) -> str:
        if name := self.role_providers.get(role):
            return self.role_models.get(role) or self.providers[name].model
        return self.role_models.get(role) or self.model

    def provider_table(self, role: str) -> tuple[str, ProviderConfig]:
        """The name and table of role's provider; "provider" for the default."""
        if name := self.role_providers.get(role):
            return f"providers.{name}", self.providers[name]
        default = ProviderConfig(
            self.provider_type,
            self.base_url,
            self.api_key_env,
            self.api_key_file,
            self.model,
            self.timeout,
            self.extra_body,
            self.provider_options,
            self.price,
        )
        return "provider", default

    def provider(
        self, role: str, env: Mapping[str, str] = os.environ, model: str = ""
    ) -> Provider:
        """A provider for role, at model if given. Raises ConfigError if the model or
        key is missing, or the provider type cannot be loaded or rejects the settings."""
        name, table = self.provider_table(role)
        model = model or self.model_for(role)
        if not model:
            hint = "TIMU_MODEL" if name == "provider" else f"roles.{role}.model"
            raise ConfigError(f"{self.source}: no model; set {name}.model or {hint}")
        return table.build(model, env, self.source, name)

    def search_tool(self, env: Mapping[str, str] = os.environ) -> Tool | None:
        """web_search if its key is set, else None. Raises ConfigError for a bad key
        file."""
        if self.search_key_file:
            return web_search_tool(read_key(self.search_key_file))
        key = env.get(self.search_key_env, "") if self.search_key_env else ""
        return web_search_tool(key) if key else None


def read_key(path: Path) -> str:
    """The key in path. Raises ConfigError if path is unreadable, empty, or open to
    other users."""
    try:
        mode = path.stat().st_mode
        key = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ConfigError(f"cannot read key file: {e}") from None
    if mode & 0o077:
        raise ConfigError(f"{path} is open to other users; run chmod 600 {path}")
    if not key:
        raise ConfigError(f"{path} is empty")
    return key


def user_config_path(env: Mapping[str, str] = os.environ) -> Path | None:
    """The user config file if it exists, else None."""
    base = env.get("XDG_CONFIG_HOME") or (
        str(Path(env["HOME"]) / ".config") if env.get("HOME") else ""
    )
    user = Path(base) / "timu" / "timu.toml" if base else None
    return user if user and user.is_file() else None


def load_config(
    path: Path | None = None, env: Mapping[str, str] = os.environ
) -> Config:
    """Read path, else the user file. Then apply env overrides. Raises ConfigError."""
    path = path or user_config_path(env)
    raw, src = (_read(path), str(path)) if path else ({}, "defaults")
    _only(raw, KEYS, src)
    roles = _table(raw, "roles", src)
    named = _table(raw, "providers", src)
    for table, keys in KEYS.items():
        if table not in ("roles", "providers"):
            _only(_table(raw, table, src), keys, f"{src}: {table}")
    providers = {
        name: provider_config(_table(named, name, src), f"{src}: providers.{name}")
        for name in named
    }
    for name in roles:
        if name not in ROLE_NAMES:
            raise ConfigError(
                f"{src}: unknown role {name}; roles: {', '.join(ROLE_NAMES)}"
            )
        _only(_table(roles, name, src), KEYS["roles"], f"{src}: roles.{name}")
    role_providers = {
        name: _get(_table(roles, name, src), "provider", str, "", src) for name in roles
    }
    for role, name in role_providers.items():
        if name and name not in providers:
            known = ", ".join(sorted(providers)) or "none"
            raise ConfigError(
                f"{src}: roles.{role}.provider: no [providers.{name}]; defined: {known}"
            )
    default = provider_config(_table(raw, "provider", src), src)
    config = Config(
        provider_type=default.type,
        base_url=default.base_url,
        api_key_env=default.api_key_env,
        api_key_file=default.api_key_file,
        model=default.model,
        timeout=default.timeout,
        extra_body=default.extra_body,
        provider_options=default.options,
        price=default.price,
        providers=providers,
        role_models={
            name: _get(_table(roles, name, src), "model", str, "", src)
            for name in roles
        },
        role_providers={r: n for r, n in role_providers.items() if n},
        role_skills={
            name: _strings(_table(roles, name, src), "skills", src) for name in roles
        },
        extra_read=tuple(
            Path(os.path.realpath(os.path.expanduser(p)))
            for p in _strings(_table(raw, "sandbox", src), "extra_read", src)
        ),
        project_roots=tuple(
            Path(os.path.realpath(os.path.expanduser(p)))
            for p in _strings(_table(raw, "projects", src), "roots", src)
        ),
        search_key_env=_get(
            _table(raw, "search", src), "api_key_env", str, "BRAVE_API_KEY", src
        ),
        search_key_file=_path(_table(raw, "search", src), "api_key_file", src),
        source=src,
    )
    if url := env.get("TIMU_BASE_URL"):
        config = replace(
            config, base_url=url, source=f"{config.source} + TIMU_BASE_URL"
        )
    if model := env.get("TIMU_MODEL"):
        config = replace(config, model=model, source=f"{config.source} + TIMU_MODEL")
    return config


def provider_config(prov: Mapping[str, Any], src: str) -> ProviderConfig:
    """A provider table, checked. Raises ConfigError naming src."""
    _only(prov, KEYS["provider"], src)
    kind = _get(prov, "type", str, BUILTIN, src)
    builtin = kind == BUILTIN
    timeout = float(_get(prov, "timeout", (int, float), 600, src))
    if not 0 < timeout < math.inf:  # also rejects nan
        raise ConfigError(f"{src}: timeout must be positive and finite")
    return ProviderConfig(
        type=kind,
        base_url=_get(prov, "base_url", str, DEFAULT_BASE_URL if builtin else "", src),
        api_key_env=_get(
            prov, "api_key_env", str, DEFAULT_KEY_ENV if builtin else "", src
        ),
        api_key_file=_path(prov, "api_key_file", src),
        model=_get(prov, "model", str, "", src),
        timeout=timeout,
        extra_body=_table(prov, "extra_body", src),
        options=_table(prov, "options", src),
        price=_price(prov, src),
    )


def _price(prov: Mapping[str, Any], src: str) -> Price | None:
    t = _table(prov, "price", src)
    if not t:
        return None
    allowed = {"input", "output", "cached_input", "cache_write"}
    if not {"input", "output"} <= set(t) <= allowed:
        raise ConfigError(
            f"{src}: price needs input and output, and may set cached_input and "
            "cache_write; USD per million tokens"
        )
    try:
        return Price(
            t["input"], t["output"], t.get("cached_input"), t.get("cache_write")
        )
    except ValueError as e:
        raise ConfigError(f"{src}: {e}") from None


# The keys of each table; for roles and providers, the keys of each [<table>.<name>].
KEYS = {
    "provider": {
        "type",
        "options",
        "price",
        "base_url",
        "api_key_env",
        "api_key_file",
        "model",
        "timeout",
        "extra_body",
    },
    "providers": set(),  # each [providers.NAME] has the keys of provider
    "roles": {"model", "provider", "skills"},
    "search": {"api_key_env", "api_key_file"},
    "sandbox": {"extra_read"},
    "projects": {"roots"},
}


def _only(t: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    if extra := sorted(set(t) - set(allowed)):
        raise ConfigError(f"{where}: unknown keys: {', '.join(extra)}")


def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None


def _table(raw: Mapping[str, Any], key: str, src: str) -> dict[str, Any]:
    v = raw.get(key, {})
    if not isinstance(v, dict):
        raise ConfigError(f"{src}: {key} must be a table")
    return v


def _strings(raw: Mapping[str, Any], key: str, src: str) -> tuple[str, ...]:
    v = raw.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{src}: {key} must be a list of strings")
    return tuple(v)


def _path(raw: Mapping[str, Any], key: str, src: str) -> Path | None:
    v = _get(raw, key, str, "", src)
    return Path(os.path.expanduser(v)) if v else None


def _get(raw: Mapping[str, Any], key: str, kind: Any, default: Any, src: str) -> Any:
    v = raw.get(key, default)
    if not isinstance(v, kind) or isinstance(v, bool):
        raise ConfigError(f"{src}: {key} has the wrong type")
    return v
