"""Fixtures for the lokate suite: callers, and a way to run documents as them.

Callers are the static tokens of ``settings_test``: ``phone-a`` and ``phone-b`` are one user's
two phones, ``other`` is another user (on a phone reporting phone-a's device id), and
``nodevice`` names no device. The schema's AuthentikateExtension authenticates each from its
Authorization header, exactly as a real request.
"""

from collections.abc import Iterator

import pytest
from django.db import connection

from tests.runner import Run


@pytest.fixture(autouse=True)
def clean_points(request: pytest.FixtureRequest) -> Iterator[None]:
    """``timeline_point`` is unmanaged, so the test flush leaves it alone; empty it after each test."""
    yield
    if "transactional_db" in request.fixturenames or request.node.get_closest_marker(
        "django_db"
    ):
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE timeline_point")


@pytest.fixture
def run(transactional_db: None) -> Run:
    """Run a document as ``token`` (phone-a by default); fails the test on errors unless ``errors=True``."""
    return Run()
