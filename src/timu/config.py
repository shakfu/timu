"""timu.toml: which model API to call, and which model each role uses.

    [provider]
    base_url = "https://openrouter.ai/api/v1"
    api_key_env = "OPENROUTER_API_KEY"   # "" for a local server without a key
    model = "<model id>"
    timeout = 600                        # seconds, optional
    extra_body = { cache_control = { type = "ephemeral" } }   # optional

    [roles.reviewer]
    model = "<cheaper model id>"
    skills = ["code-review"]             # optional

    [search]
    api_key_env = "BRAVE_API_KEY"        # Brave Search; the default (plan D3)

    [sandbox]
    extra_read = ["~/.local/share/uv"]   # toolchains under $HOME (interim, design 15.8)

The user file is $XDG_CONFIG_HOME/timu/timu.toml (default ~/.config/timu/timu.toml).
./timu.toml overlays it but may set only provider.model, provider.timeout,
provider.extra_body and [roles]: a cloned repo writes that file, so it must not choose
where requests and keys go or what the sandbox reads. TIMU_BASE_URL and TIMU_MODEL
override both. An explicit --config file is trusted in full.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from timu.provider.openai import OpenAIProvider
from timu.tool import Tool
from timu.tools.web import web_search_tool

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
LOCAL = Path("timu.toml")
LOCAL_PROVIDER_KEYS = frozenset({"model", "timeout", "extra_body"})


class ConfigError(ValueError):
    """An invalid or incomplete configuration. The message names the file and key."""


@dataclass(frozen=True)
class Config:
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_KEY_ENV
    model: str = ""
    timeout: float = 600
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    role_models: Mapping[str, str] = field(default_factory=dict)
    role_skills: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    extra_read: tuple[Path, ...] = ()
    search_key_env: str = "BRAVE_API_KEY"
    source: str = "defaults"  # the file read, for error messages

    def model_for(self, role: str) -> str:
        return self.role_models.get(role) or self.model

    def provider(
        self, role: str, env: Mapping[str, str] = os.environ
    ) -> OpenAIProvider:
        """A provider for role. Raises ConfigError if the model or key is missing."""
        model = self.model_for(role)
        if not model:
            raise ConfigError(
                f"{self.source}: no model; set provider.model or TIMU_MODEL"
            )
        key = ""
        if self.api_key_env:
            key = env.get(self.api_key_env, "")
            if not key:
                raise ConfigError(
                    f"{self.api_key_env} is not set; set it, or set provider.api_key_env "
                    f'to "" for a server without a key'
                )
        return OpenAIProvider(
            self.base_url, model, key, extra_body=self.extra_body, timeout=self.timeout
        )

    def search_tool(self, env: Mapping[str, str] = os.environ) -> Tool | None:
        """web_search if its key is set, else None."""
        key = env.get(self.search_key_env, "") if self.search_key_env else ""
        return web_search_tool(key) if key else None


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
    """Read path; else the user file overlaid by ./timu.toml. Then apply env
    overrides. Raises ConfigError, including for a key ./timu.toml may not set."""
    if path is not None:
        raw, src = _read(path), str(path)
    else:
        raw, found = {}, []
        if user := user_config_path(env):
            raw = _read(user)
            found.append(str(user))
        if LOCAL.is_file():
            local = _read(LOCAL)
            _refuse_local(local)
            raw = _merge(raw, local)
            found.append(str(LOCAL))
        src = " + ".join(found) or "defaults"
    prov = _table(raw, "provider", src)
    roles = _table(raw, "roles", src)
    config = Config(
        base_url=_get(prov, "base_url", str, DEFAULT_BASE_URL, src),
        api_key_env=_get(prov, "api_key_env", str, DEFAULT_KEY_ENV, src),
        model=_get(prov, "model", str, "", src),
        timeout=float(_get(prov, "timeout", (int, float), 600, src)),
        extra_body=_table(prov, "extra_body", src),
        role_models={
            name: _get(_table(roles, name, src), "model", str, "", src)
            for name in roles
        },
        role_skills={
            name: _strings(_table(roles, name, src), "skills", src) for name in roles
        },
        extra_read=tuple(
            Path(os.path.realpath(os.path.expanduser(p)))
            for p in _strings(_table(raw, "sandbox", src), "extra_read", src)
        ),
        search_key_env=_get(
            _table(raw, "search", src), "api_key_env", str, "BRAVE_API_KEY", src
        ),
        source=src,
    )
    if url := env.get("TIMU_BASE_URL"):
        config = replace(config, base_url=url)
    if model := env.get("TIMU_MODEL"):
        config = replace(config, model=model)
    return config


def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None


def _refuse_local(raw: Mapping[str, Any]) -> None:
    prov = raw.get("provider", {})
    keys = [k for k in raw if k not in ("provider", "roles")]
    if isinstance(prov, dict):
        keys += [f"provider.{k}" for k in prov if k not in LOCAL_PROVIDER_KEYS]
    if keys:
        raise ConfigError(
            f"{LOCAL}: {', '.join(keys)} may be set only in the user config, "
            "the environment, or a file passed with --config"
        )


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        both = isinstance(v, dict) and isinstance(out.get(k), dict)
        out[k] = _merge(out[k], v) if both else v
    return out


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


def _get(raw: Mapping[str, Any], key: str, kind: Any, default: Any, src: str) -> Any:
    v = raw.get(key, default)
    if not isinstance(v, kind) or isinstance(v, bool):
        raise ConfigError(f"{src}: {key} has the wrong type")
    return v
