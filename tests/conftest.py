import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

from foxray.core.sql import TABLES, PostgresBackend, SqliteBackend, SqlStore, init_schema

PG_URL = os.environ.get("FOXRAY_TEST_PG_URL", "")


@pytest.fixture
def sqlite_store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "foxray.db"))
    init_schema(backend)
    yield SqlStore(backend)
    backend.close()


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture(scope="session")
def pg_url():
    """A throwaway database, created and dropped by the test run."""
    if not PG_URL:
        pytest.skip("set FOXRAY_TEST_PG_URL to run the Postgres tests")
    import psycopg
    name = "foxray_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    try:
        yield _with_db(PG_URL, name)
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def pg_fresh_url():
    """A brand-new database for one test (dropped afterwards)."""
    if not PG_URL:
        pytest.skip("set FOXRAY_TEST_PG_URL to run the Postgres tests")
    import psycopg
    name = "foxray_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    try:
        yield _with_db(PG_URL, name)
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def pg_store(pg_url):
    backend = PostgresBackend(pg_url)
    init_schema(backend)
    with backend.tx() as c:
        c.execute("TRUNCATE " + ", ".join(TABLES) + ", foxray_user RESTART IDENTITY", ())
    yield SqlStore(backend)
    backend.close()


@pytest.fixture
def django_store(transactional_db):
    from foxray.django.store import get_store
    return get_store()


@pytest.fixture(params=["sqlite", "postgres", "django"])
def store(request):
    """The storage contract runs against every store."""
    return request.getfixturevalue({"sqlite": "sqlite_store", "postgres": "pg_store",
                                    "django": "django_store"}[request.param])


def pytest_collection_modifyitems(items):
    # pytest-django creates only the test databases that collected tests ask
    # for, so mark the Django runs of the contract up front.
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is not None and callspec.params.get("store") == "django":
            item.add_marker(pytest.mark.django_db(transaction=True))
