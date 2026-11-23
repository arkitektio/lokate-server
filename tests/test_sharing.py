"""A guest of the organization, granted a collaboration and nothing else: what they read of a
timeline someone shared with them, and that they change nothing.

phone-a's person backs up a day (points in August and September, so two monthly partitions of
the point table), visits matched to places, and a trip, and puts some of it in a collaboration
under a fresh key (``addToCollaboration``, which makes lokate's row of it). The guest's token
(conftest ``guest``) is that of ``other``'s user, who backs up a timeline of their own: a guest
still reads only what was shared with them. Location fixes and devices are never shared.
"""

import datetime
import uuid
from typing import Any

import pytest
from arkitekt_service.collaborations.models import Collaboration, CollaborationItem
from strawberry.types import ExecutionResult

from tests.conftest import GuestFactory
from tests.runner import Run

pytestmark = pytest.mark.django_db(transaction=True)

UPLOAD = (
    "mutation U($points: [PointInput!]!) { uploadPoints(points: $points) { accepted } }"
)
REPLACE = """mutation R($from: DateTime!, $visits: [VisitInput!]!, $trips: [TripInput!]!) {
  replaceSegments(from: $from, visits: $visits, trips: $trips) { visits trips } }"""
PLACES = """mutation P($places: [PlaceInput!]!, $deleted: [DeletedInput!]!) { syncPlaces(places: $places, deleted: $deleted) { applied } }"""
ADD = "mutation ($input: CollaborationObjectInput!) { addToCollaboration(input: $input) { key } }"

T0 = datetime.datetime(2026, 8, 31, 22, 0, tzinfo=datetime.UTC)
HOME = (48.2000, 16.3700)
WORK = (48.2100, 16.3900)


def iso(minutes: float) -> str:
    return (T0 + datetime.timedelta(minutes=minutes)).isoformat()


def _visit(
    cid: str, start: float, end: float, at: tuple[float, float], place: str
) -> dict[str, Any]:
    return {
        "clientId": cid,
        "start": iso(start),
        "end": iso(end),
        "lat": at[0],
        "lon": at[1],
        "radius": 40.0,
        "pointCount": 10,
        "placeClientId": place,
    }


def _place(cid: str, name: str, at: tuple[float, float]) -> dict[str, Any]:
    return {
        "clientId": cid,
        "name": name,
        "lat": at[0],
        "lon": at[1],
        "radius": 50.0,
        "updatedAt": iso(0),
    }


def _refused(result: ExecutionResult, reason: str) -> None:
    assert result.errors, "it was let through"
    extensions = result.errors[0].extensions or {}
    assert extensions.get("reason") == reason, result.errors


@pytest.fixture
async def timeline(run: Run) -> dict[str, str]:
    """phone-a's timeline across midnight into September (two point partitions), and other's own
    visit to their own "home"."""
    await run(
        UPLOAD,
        {
            "points": [
                {"clientId": f"p{i}", "ts": iso(i * 30), "lat": HOME[0], "lon": HOME[1]}
                for i in range(6)  # 22:00 to 00:30: August and September
            ]
        },
    )
    await run(
        REPLACE,
        {
            "from": iso(0),
            "visits": [
                _visit("v-home", 0, 30, HOME, "home"),
                _visit("v-work", 60, 120, WORK, "work"),
                _visit("v-work-again", 180, 240, WORK, "work"),
            ],
            "trips": [
                {
                    "clientId": "t1",
                    "start": iso(30),
                    "end": iso(60),
                    "distance": 2000.0,
                    "mode": "BIKE",
                    "fromVisit": "v-home",
                    "toVisit": "v-work",
                }
            ],
        },
    )
    await run(
        PLACES,
        {
            "places": [_place("home", "Home", HOME), _place("work", "Office", WORK)],
            "deleted": [],
        },
    )
    await run(
        REPLACE,
        {
            "from": iso(0),
            "visits": [_visit("v-theirs", 0, 30, HOME, "home")],
            "trips": [],
        },
        token="other",
    )
    await run(
        PLACES,
        {"places": [_place("home", "Their home", HOME)], "deleted": []},
        token="other",
    )
    mine = await run(
        "{ visits { id clientId } trips { id clientId } places { id clientId } points { id } }"
    )
    ids = {
        f"{kind}:{row['clientId']}": row["id"]
        for kind in ("visits", "trips", "places")
        for row in mine[kind]
    }
    assert len(mine["points"]) == 6
    ids["point"] = mine["points"][0]["id"]
    return ids


async def _share(run: Run, identifier: str, pk: str, key: str | None = None) -> str:
    key = key or str(uuid.uuid4())
    await run(
        ADD,
        {
            "input": {
                "collaboration": key,
                "name": "Where I was",
                "identifier": identifier,
                "object": pk,
            }
        },
    )
    return key


async def test_a_guest_reads_the_shared_visit_and_its_place_and_nothing_else(
    run: Run, timeline: dict[str, str], guest: GuestFactory
) -> None:
    key = await _share(run, "@lokate/visit", timeline["visits:v-work"])
    token = guest({key: []})

    seen = await run(
        "{ visits { clientId place { name visitCount } } trips { id } places { name visitCount lastVisitAt } points { id } devices { id } }",
        token=token,
    )
    assert seen["visits"] == [
        {"clientId": "v-work", "place": {"name": "Office", "visitCount": 1}}
    ]
    # The place came along; it counts the shared visit to it, not the owner's second one.
    assert seen["places"] == [
        {"name": "Office", "visitCount": 1, "lastVisitAt": iso(120)}
    ]
    assert seen["trips"] == [] and seen["points"] == [] and seen["devices"] == []

    owner = await run("{ places { clientId visitCount } }")
    assert {"clientId": "work", "visitCount": 2} in owner["places"]

    for document, pk in [
        ("query ($id: ID!) { visit(id: $id) { id } }", timeline["visits:v-home"]),
        ("query ($id: ID!) { point(id: $id) { id } }", timeline["point"]),
        ("query ($id: ID!) { place(id: $id) { id } }", timeline["places:home"]),
    ]:
        assert (await run(document, {"id": pk}, token=token, errors=True)).errors


async def test_a_shared_trip_and_never_a_point_on_either_partition(
    run: Run, timeline: dict[str, str], guest: GuestFactory
) -> None:
    key = await _share(run, "@lokate/trip", timeline["trips:t1"])
    token = guest({key: []})

    seen = await run("{ trips { clientId mode distance } points { id } }", token=token)
    assert seen == {
        "trips": [{"clientId": "t1", "mode": "BIKE", "distance": 2000.0}],
        "points": [],
    }
    # Points are no structure: a member cannot put one in.
    put = await run(
        ADD,
        {
            "input": {
                "collaboration": key,
                "identifier": "@lokate/point",
                "object": timeline["point"],
            }
        },
        errors=True,
    )
    assert put.errors, "a location fix went in"

    # What reads the points or the whole timeline is closed to guests.
    for document in (
        "{ pointsCount }",
        "{ visitsCount }",
        '{ day(date: "2026-08-31") { pointCount } }',
        '{ route(since: "2026-08-31T00:00:00Z", until: "2026-09-02T00:00:00Z") { pointCount } }',
        '{ stats(since: "2026-08-31T00:00:00Z", until: "2026-09-02T00:00:00Z") { pointCount } }',
        "{ placeStats { visits } }",
        "{ syncState { pointCount } }",
        "{ changes { hasMore } }",
    ):
        _refused(
            await run(document, token=token, errors=True),
            "COLLABORATION_DOES_NOT_REACH",
        )


async def test_no_guest_changes_anything_not_even_an_editor(
    run: Run, timeline: dict[str, str], guest: GuestFactory
) -> None:
    key = await _share(run, "@lokate/visit", timeline["visits:v-work"])

    for token in (guest({key: []}), guest({key: ["lokate_editor"]})):
        for document, variables in [
            (
                UPLOAD,
                {"points": [{"clientId": "g", "ts": iso(5), "lat": 1.0, "lon": 1.0}]},
            ),
            (REPLACE, {"from": iso(0), "visits": [], "trips": []}),
            (PLACES, {"places": [_place("work", "Mine now", WORK)], "deleted": []}),
            (
                'mutation { deleteServerCopy(confirm: "DELETE") }',
                {},
            ),
            (
                ADD,
                {
                    "input": {
                        "collaboration": key,
                        "identifier": "@lokate/visit",
                        "object": timeline["visits:v-home"],
                    }
                },
            ),
            (
                "mutation ($input: ShareWorkflowInput!) { shareWorkflow(input: $input) { dryRun } }",
                {"input": {"collaboration": key, "rootTask": "any", "dryRun": True}},
            ),
            ("mutation ($key: ID!) { deleteCollaboration(key: $key) }", {"key": key}),
        ]:
            _refused(
                await run(document, variables, token=token, errors=True),
                "SCOPED_READ_ONLY",
            )

    owner = await run("{ places { name } visits { id } }")
    assert {p["name"] for p in owner["places"]} == {"Home", "Office"}
    assert len(owner["visits"]) == 3
    theirs = await run("{ visits { clientId } }", token="other")
    assert theirs["visits"] == [{"clientId": "v-theirs"}]
    assert (
        await CollaborationItem.objects.filter(collaboration__key=key).acount() == 2
    )  # the visit and its place


async def test_only_the_person_whose_timeline_it_is_shares_it(
    run: Run, timeline: dict[str, str]
) -> None:
    key = str(uuid.uuid4())
    put = await run(
        ADD,
        {
            "input": {
                "collaboration": key,
                "identifier": "@lokate/visit",
                "object": timeline["visits:v-home"],
            }
        },
        token="other",
        errors=True,
    )
    assert put.errors, "someone else shared phone-a's visit"
    assert not await Collaboration.objects.filter(key=key).aexists()


async def test_a_member_lists_what_is_in_a_collaboration(
    run: Run, timeline: dict[str, str], guest: GuestFactory
) -> None:
    key = await _share(run, "@lokate/place", timeline["places:home"])
    await _share(run, "@lokate/trip", timeline["trips:t1"])  # another, not granted

    listed = await run(
        "query ($key: ID!, $f: PlaceFilter) { collaboration(key: $key) { key itemCount(brought: false) } places(filters: $f) { name } }",
        {"key": key, "f": {"inCollaboration": key}},
    )
    assert listed == {
        "collaboration": {"key": key, "itemCount": 1},
        "places": [{"name": "Home"}],
    }
    granted = await run("{ collaborations { key name } }", token=guest({key: []}))
    assert granted == {"collaborations": [{"key": key, "name": "Where I was"}]}


def test_the_schema_says_what_each_type_is_and_opens_no_write_to_guests() -> None:
    from lokate_server.schema import schema

    sdl = str(schema)
    visit_line = next(
        line for line in sdl.splitlines() if line.startswith("type Visit ")
    )
    assert '@identified(by: "@lokate/visit")' in visit_line
    assert "allow_scoped: true" not in sdl
