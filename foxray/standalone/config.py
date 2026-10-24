"""Configuration from environment variables (``FOXRAY_<NAME>``, plus
``DATABASE_URL``) and/or a small INI file (``[foxray]`` section; a ``.toml``
file also works on Python 3.11+). Environment variables win."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, fields

from foxray.core.settings import Settings, coerce


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    database_url: str = "foxray.db"
    secret_key: str = ""            # signs UI sessions; generated and kept in the DB if empty
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_from: str = "foxray@localhost"
    trust_proxy: bool = False       # honour X-Forwarded-Proto/For from a reverse proxy
    tick_seconds: int = 60


def _read_file(path: str) -> dict:
    if path.endswith(".toml"):
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            raise SystemExit("TOML config needs Python 3.11+; use an INI file ([foxray] section)") from None
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return {k.lower(): v for k, v in data.get("foxray", data).items()}
    parser = configparser.ConfigParser(interpolation=None)
    with open(path, encoding="utf-8") as f:
        parser.read_file(f)
    if not parser.has_section("foxray"):
        return {}
    return {k.lower(): v for k, v in parser.items("foxray")}


def load(path: str | None = None, env=None) -> tuple[ServerConfig, Settings]:
    env = os.environ if env is None else env
    values: dict = {}
    path = path or env.get("FOXRAY_CONFIG")
    if path:
        values.update(_read_file(path))
    if env.get("DATABASE_URL"):
        values["database_url"] = env["DATABASE_URL"]
    for key, value in env.items():
        if key.startswith("FOXRAY_") and key != "FOXRAY_CONFIG":
            values[key[7:].lower()] = value
    if "db" in values and "database_url" not in values:
        values["database_url"] = values.pop("db")
    cfg = ServerConfig()
    for f in fields(ServerConfig):
        if f.name in values:
            setattr(cfg, f.name, coerce(values[f.name], getattr(cfg, f.name)))
    settings = Settings.from_mapping(values)
    return cfg, settings
