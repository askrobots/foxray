"""DSNs: ``scheme://public_key[:secret]@host[:port]/[path/]project_id``."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


class DsnError(ValueError):
    pass


@dataclass(frozen=True)
class Dsn:
    scheme: str
    public_key: str
    secret_key: str
    host: str
    port: int | None
    path: str          # prefix before the project id, without trailing slash ("" or "/foxray")
    project_id: str

    @property
    def netloc(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"

    def endpoint(self, kind: str = "envelope") -> str:
        return f"{self.scheme}://{self.netloc}{self.path}/api/{self.project_id}/{kind}/"

    def __str__(self) -> str:
        secret = f":{self.secret_key}" if self.secret_key else ""
        return f"{self.scheme}://{self.public_key}{secret}@{self.netloc}{self.path}/{self.project_id}"


def parse_dsn(value: str) -> Dsn:
    if not value or not isinstance(value, str):
        raise DsnError("empty DSN")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https"):
        raise DsnError("DSN scheme must be http or https")
    if not parts.username:
        raise DsnError("DSN has no public key")
    if not parts.hostname:
        raise DsnError("DSN has no host")
    path = parts.path.rstrip("/")
    prefix, _, project_id = path.rpartition("/")
    if not project_id:
        raise DsnError("DSN has no project id")
    try:
        port = parts.port
    except ValueError as exc:
        raise DsnError(str(exc)) from None
    return Dsn(parts.scheme, parts.username, parts.password or "", parts.hostname, port, prefix, project_id)


def make_dsn(base_url: str, public_key: str, project_id) -> str:
    """``base_url`` is where ingest is mounted, e.g. ``https://host`` for the
    standalone server or ``https://host/foxray`` for the Django app."""
    parts = urlsplit((base_url or "http://localhost").strip())
    scheme = parts.scheme or "http"
    netloc = parts.netloc or parts.path
    path = parts.path.rstrip("/") if parts.netloc else ""
    return f"{scheme}://{public_key}@{netloc}{path}/{project_id}"
