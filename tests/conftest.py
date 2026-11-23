"""Fixtures for the lokate suite: callers, and a way to run documents as them.

Callers are the static tokens of ``settings_test``: ``phone-a`` and ``phone-b`` are one user's
two phones, ``other`` is another user (on a phone reporting phone-a's device id), and
``nodevice`` names no device. The schema's AuthentikateExtension authenticates each from its
Authorization header, exactly as a real request.
"""

from collections.abc import Callable, Iterator, Mapping

import pytest
from authentikate import settings as authentikate_settings
from django.db import connection
from pytest_django.fixtures import SettingsWrapper

from tests.runner import Run

#: ``guest({key: roles})``: grant the static token ``guest`` those collaborations, by their key
#: (lok's), with the roles held in each (``[]``: view only); returns the token to run as.
GuestFactory = Callable[[Mapping[str, list[str]]], str]


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


@pytest.fixture
def guest(transactional_db: None, settings: SettingsWrapper) -> Iterator[GuestFactory]:
    """``guest({key: roles})``: the static token ``guest``, a guest of the organization granted
    those collaborations by their key, as lok lists them in the token's ``collabs``. Its user is
    ``other``'s (sub 9), who backs up a timeline of their own: a guest still reads only what was
    shared with them. Holds no role of the organization, through an app granted all of lokate."""

    def mint(collaborations: Mapping[str, list[str]]) -> str:
        settings.AUTHENTIKATE = {
            **settings.AUTHENTIKATE,
            "static_tokens": {
                **settings.AUTHENTIKATE["static_tokens"],
                "guest": {
                    "sub": "9",
                    "client_device": "guest-phone",
                    "roles": [],
                    "collabs": dict(collaborations),
                    "scope": "openid lokate_all",
                },
            },
        }
        authentikate_settings.cached_settings = None
        return "guest"

    yield mint
    authentikate_settings.cached_settings = None
