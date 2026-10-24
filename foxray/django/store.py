"""The SQL store on Django's own database connection."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime

from django.db import connections, transaction

from foxray.core.sql import SqlStore
from foxray.core.util import to_utc_text

_DIALECTS = {"postgresql": "postgres", "sqlite": "sqlite"}


class DjangoBackend:
    def __init__(self, using: str = "default"):
        self.using = using
        vendor = connections[using].vendor
        if vendor not in _DIALECTS:
            raise RuntimeError(f"Foxray supports PostgreSQL and SQLite, not {vendor}")
        self.dialect = _DIALECTS[vendor]

    def adapt(self, value):
        if isinstance(value, datetime):
            # SQLite: the same text Django writes; Postgres: an aware datetime.
            return to_utc_text(value) if self.dialect == "sqlite" else value
        if isinstance(value, date):
            return value.isoformat()
        return value

    @contextmanager
    def tx(self):
        with transaction.atomic(using=self.using):
            with connections[self.using].cursor() as c:
                yield c

    @contextmanager
    def cursor(self):
        with connections[self.using].cursor() as c:
            yield c

    def close(self) -> None:
        pass


def get_store(using: str = "default") -> SqlStore:
    return SqlStore(DjangoBackend(using))
