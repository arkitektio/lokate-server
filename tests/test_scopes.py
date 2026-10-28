"""What an app may do here is what its token's scopes say: every root field asks for one of lokate's
own (the ``@auth`` directive publishes it, a generated client gathers it), and `lokate_all` stands
for all of them."""

import pytest

from arkitekt_service.graphql import scope_gaps
from lokate_server.schema import schema

pytestmark = pytest.mark.django_db(transaction=True)

SYNC_PLACES = """
mutation { syncPlaces(places: [{clientId: "home", name: "Home", lat: 52.5, lon: 13.4, radius: 50, updatedAt: "2026-10-28T12:00:00Z"}], deleted: []) { applied } }
"""
PLACES = "query { places { clientId } }"


def _reason(result) -> str | None:
    return result.errors[0].extensions.get("reason") if result.errors else None


def test_every_root_field_asks_for_a_scope_lokate_declares():
    assert scope_gaps(schema) == []


async def test_an_app_granted_reads_reads_and_writes_nothing(run):
    refused = await run(SYNC_PLACES, token="readertest", errors=True)
    assert _reason(refused) == "INSUFFICIENT_SCOPE"
    assert (await run(PLACES, token="readertest"))["places"] == []


async def test_the_all_scope_writes(run):
    assert (await run(SYNC_PLACES))["syncPlaces"]["applied"] == 1
    assert (await run(PLACES, token="readertest"))["places"] == [{"clientId": "home"}]


async def test_the_default_role_writes(run):
    assert (await run(SYNC_PLACES, token="editortest"))["syncPlaces"]["applied"] == 1
