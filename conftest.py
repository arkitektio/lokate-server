"""The test stack: a real postgres with PostGIS (jhnnsrs/daten), brought up by dokker.

A *root* conftest, so every database test in the repository gets it, not only those under
``tests/`` (see mikro's conftest for the reasoning behind each step). Nothing is mocked.
"""

import time
from pathlib import Path

import psycopg
import pytest
from dokker import testing


def _wait(check, what: str, timeout: float = 90) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while True:
        try:
            check()
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{what} did not come up in {timeout}s")
            time.sleep(0.2)


@pytest.fixture(scope="session")
def backend_stack():
    """Bring up postgres and yield the host port docker gave it.

    No host port is pinned: dokker mints a unique project per run and docker picks a free
    port, resolved inside the wait loop because ``up()`` may return before it is published.
    """
    compose = Path(__file__).parent / "tests" / "integration" / "docker-compose.yaml"
    with testing(str(compose)) as e:
        e.up()
        ports: dict[str, int] = {}

        def db_ready() -> None:
            if "db" not in ports:
                ports["db"] = e.get_port("db", 5432)
            with psycopg.connect(dbname="testdb", user="test", password="test", host="localhost", port=ports["db"], connect_timeout=1) as connection:
                connection.execute("SELECT 1")

        _wait(db_ready, "postgres")
        yield ports["db"]


@pytest.fixture(scope="session")
def django_db_modify_db_settings(backend_stack):
    """Point Django at the stack's postgres before pytest-django creates the test database."""
    from django.conf import settings

    settings.DATABASES["default"]["PORT"] = str(backend_stack)
    yield


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Kill the connections of asgiref's executor threads before the test database is dropped."""
    yield
    from django.db import connections

    with django_db_blocker.unblock():
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
        connections.close_all()
