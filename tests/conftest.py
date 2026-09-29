"""Fixtures for the lokate suite: callers, and a way to run documents as them.

Callers are the static tokens of ``settings_test``: ``phone-a`` and ``phone-b`` are one user's
two phones, ``other`` is another user (on a phone reporting phone-a's device id), and
``nodevice`` names no device. The schema's AuthentikateExtension authenticates each from its
Authorization header, exactly as a real request.
"""

import pytest
from django.db import connection
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse

from lokate_server.schema import schema


def context(token: str) -> HttpContext:
    return HttpContext(
        request=UniversalRequest(_extensions={}),
        response=TemporalResponse(),
        headers={"Authorization": f"Bearer {token}"},
        type="http",
    )


@pytest.fixture(autouse=True)
def clean_points(request):
    """``timeline_point`` is unmanaged, so the test flush leaves it alone; empty it after each test."""
    yield
    if "transactional_db" in request.fixturenames or request.node.get_closest_marker("django_db"):
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE timeline_point")


@pytest.fixture
def run(transactional_db):
    """Run a document as ``token`` (phone-a by default); fails the test on errors unless ``errors=True``."""

    async def _run(document: str, variables: dict | None = None, token: str = "phone-a", errors: bool = False):
        result = await schema.execute(document, variable_values=variables or {}, context_value=context(token))
        if not errors:
            assert not result.errors, result.errors
            return result.data
        return result

    return _run
