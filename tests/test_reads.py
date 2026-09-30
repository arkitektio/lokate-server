"""Reading the timeline back: lists with filters, single fetches, and the aggregates.

One user (phone-a, phone-b) walks a line through Vienna on 2026-08-30; another user
(``other``) has data at the same spots, which must never show up.
"""

import datetime
import json

import pytest

pytestmark = pytest.mark.django_db(transaction=True)

UPLOAD = "mutation U($points: [PointInput!]!) { uploadPoints(points: $points) { accepted } }"
REPLACE = """mutation R($from: DateTime!, $visits: [VisitInput!]!, $trips: [TripInput!]!) {
  replaceSegments(from: $from, visits: $visits, trips: $trips) { visits trips } }"""
PLACES = """mutation P($places: [PlaceInput!]!, $deleted: [DeletedInput!]!) { syncPlaces(places: $places, deleted: $deleted) { applied } }"""

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
HOME = (48.2000, 16.3700)
WORK = (48.2100, 16.3900)


def iso(minutes: float) -> str:
    return (T0 + datetime.timedelta(minutes=minutes)).isoformat()


def walk(prefix: str, n: int = 11, start: float = 30) -> list[dict]:
    """From HOME to WORK in ``n`` fixes, one a minute, from ``start`` minutes after T0."""
    return [
        {
            "clientId": f"{prefix}{i}",
            "ts": iso(start + i),
            "lat": HOME[0] + (WORK[0] - HOME[0]) * i / (n - 1),
            "lon": HOME[1] + (WORK[1] - HOME[1]) * i / (n - 1),
            "acc": 5.0 if i % 2 == 0 else 50.0,
        }
        for i in range(n)
    ]


def visit(cid: str, start: float, end: float, at: tuple[float, float], place: str | None) -> dict:
    return {"clientId": cid, "start": iso(start), "end": iso(end), "lat": at[0], "lon": at[1], "radius": 40.0, "pointCount": 10, "placeClientId": place}


@pytest.fixture
async def seeded(run):
    await run(UPLOAD, {"points": walk("a")})
    await run(UPLOAD, {"points": walk("b", n=3, start=200)}, token="phone-b")
    await run(UPLOAD, {"points": walk("x")}, token="other")
    await run(
        REPLACE,
        {
            "from": iso(0),
            "visits": [visit("v-home", 0, 30, HOME, "home"), visit("v-work", 40, 180, WORK, "work")],
            "trips": [{"clientId": "t1", "start": iso(30), "end": iso(40), "distance": 2000.0, "mode": "BIKE", "fromVisit": "v-home", "toVisit": "v-work"}],
        },
    )
    await run(
        REPLACE,
        {"from": iso(0), "visits": [], "trips": [{"clientId": "t2", "start": iso(200), "end": iso(202), "distance": 300.0, "mode": "WALK"}]},
        token="phone-b",
    )
    await run(REPLACE, {"from": iso(0), "visits": [visit("v-x", 0, 30, HOME, "home")], "trips": []}, token="other")
    await run(
        PLACES,
        {
            "places": [
                {"clientId": "home", "name": "Home", "lat": HOME[0], "lon": HOME[1], "radius": 50.0, "updatedAt": iso(0)},
                {"clientId": "work", "name": "Office", "lat": WORK[0], "lon": WORK[1], "radius": 80.0, "updatedAt": iso(0)},
            ],
            "deleted": [{"clientId": "gone", "deletedAt": iso(0)}],
        },
    )
    await run(PLACES, {"places": [{"clientId": "home", "name": "Their home", "lat": 1.0, "lon": 2.0, "radius": 5.0, "updatedAt": iso(0)}], "deleted": []}, token="other")


async def test_points_are_the_callers_from_all_devices(run, seeded):
    data = await run("query { points(ordering: [{ts: ASC}]) { clientId deviceId device { deviceId } } pointsCount }")
    assert data["pointsCount"] == 14
    assert [p["clientId"] for p in data["points"]] == [f"a{i}" for i in range(11)] + ["b0", "b1", "b2"]
    assert {p["deviceId"] for p in data["points"]} == {"phone-a", "phone-b"}
    assert data["points"][0]["device"]["deviceId"] == "phone-a"
    assert (await run("query { pointsCount }", token="other"))["pointsCount"] == 11


async def test_point_filters(run, seeded):
    q = "query Q($f: PointFilter, $p: OffsetPaginationInput) { points(filters: $f, pagination: $p, ordering: [{ts: DESC}]) { clientId } pointsCount(filters: $f) }"
    window = await run(q, {"f": {"since": iso(32), "until": iso(35)}})
    assert [p["clientId"] for p in window["points"]] == ["a4", "a3", "a2"] and window["pointsCount"] == 3

    devices = (await run("query { devices(ordering: [{firstSeenAt: ASC}]) { id deviceId pointCount } }"))["devices"]
    assert [(d["deviceId"], d["pointCount"]) for d in devices] == [("phone-a", 11), ("phone-b", 3)]
    only_b = await run(q, {"f": {"devices": [devices[1]["id"]]}})
    assert only_b["pointsCount"] == 3

    near_home = await run(q, {"f": {"near": {"lat": HOME[0], "lon": HOME[1], "radius": 300}}})
    assert {p["clientId"] for p in near_home["points"]} == {"a0", "a1", "b0"}  # 0 m and ~210 m away; a2 is ~420 m

    box = await run(q, {"f": {"inBox": {"south": 48.2045, "west": 16.30, "north": 48.2075, "east": 16.40}}})
    assert {p["clientId"] for p in box["points"]} == {"a5", "a6", "a7", "b1"}

    accurate = await run(q, {"f": {"maxAccuracy": 10, "until": iso(100)}})
    assert accurate["pointsCount"] == 6

    page = await run(q, {"p": {"offset": 2, "limit": 2}})
    assert [p["clientId"] for p in page["points"]] == ["b0", "a10"]


async def test_single_fetches_are_scoped(run, seeded):
    point_id = (await run("query { points(pagination: {limit: 1}) { id } }"))["points"][0]["id"]
    assert (await run("query Q($id: ID!) { point(id: $id) { clientId } }", {"id": point_id}))["point"]["clientId"]
    theirs = await run("query Q($id: ID!) { point(id: $id) { clientId } }", {"id": point_id}, token="other", errors=True)
    assert theirs.errors and theirs.data is None


async def test_visits_trips_and_places(run, seeded):
    visits = (
        await run(
            """query { visits(ordering: [{start: ASC}]) { clientId duration placeClientId place { name } }
                       matched: visitsCount(filters: {places: ["work"]})
                       long: visitsCount(filters: {minDuration: 3600}) }"""
        )
    )
    assert [(v["clientId"], v["duration"], v["place"]["name"]) for v in visits["visits"]] == [("v-home", 1800.0, "Home"), ("v-work", 8400.0, "Office")]
    assert visits["matched"] == 1 and visits["long"] == 1

    trips = await run('query { trips(filters: {modes: [BIKE]}) { clientId mode duration fromVisit toVisit distance } tripsCount }')
    assert trips["trips"] == [{"clientId": "t1", "mode": "BIKE", "duration": 600.0, "fromVisit": "v-home", "toVisit": "v-work", "distance": 2000.0}]
    assert trips["tripsCount"] == 2

    places = (await run("query { places(ordering: [{name: ASC}]) { clientId name visitCount lastVisitAt } }"))["places"]
    assert [(p["clientId"], p["name"], p["visitCount"]) for p in places] == [("home", "Home", 1), ("work", "Office", 1)]  # no tombstone, not theirs
    assert (await run('query { places(filters: {search: "off"}) { name } }'))["places"] == [{"name": "Office"}]
    near = await run("query Q($n: NearInput!) { places(filters: {near: $n}) { clientId } }", {"n": {"lat": WORK[0], "lon": WORK[1], "radius": 100}})
    assert near["places"] == [{"clientId": "work"}]


async def test_day_in_a_time_zone(run, seeded):
    day = (await run('query { day(date: "2026-08-30", timezone: "Europe/Vienna") { start end visits { clientId } trips { clientId deviceId } pointCount distance } }'))["day"]
    assert day["start"] == "2026-08-30T00:00:00+02:00"
    assert [v["clientId"] for v in day["visits"]] == ["v-home", "v-work"]
    assert [(t["clientId"], t["deviceId"]) for t in day["trips"]] == [("t1", "phone-a"), ("t2", "phone-b")]
    assert day["pointCount"] == 14 and day["distance"] == 2300.0

    empty = (await run('query { day(date: "2026-08-29") { visits { clientId } pointCount } }'))["day"]
    assert empty == {"visits": [], "pointCount": 0}
    assert (await run('query { day(date: "2026-08-30", timezone: "Mars/Olympus") { pointCount } }', errors=True)).errors[0].extensions["code"] == "INVALID_TIMEZONE"


async def test_route_is_a_line_per_device(run, seeded):
    q = "query Q($s: DateTime!, $u: DateTime!, $simplify: Float) { route(since: $s, until: $u, simplify: $simplify) { device { deviceId } pointCount distance geojson start end } }"
    tracks = (await run(q, {"s": iso(0), "u": iso(300)}))["route"]
    assert [(t["device"]["deviceId"], t["pointCount"]) for t in tracks] == [("phone-a", 11), ("phone-b", 3)]
    line = json.loads(tracks[0]["geojson"])
    assert line["type"] == "LineString" and len(line["coordinates"]) == 11
    assert line["coordinates"][0] == [HOME[1], HOME[0]]
    assert 1800 < tracks[0]["distance"] < 1900  # ~1.8 km from HOME to WORK

    # A straight walk simplifies to its two ends; the distance stays the real one.
    simple = (await run(q, {"s": iso(0), "u": iso(100), "simplify": 5}))["route"]
    assert len(json.loads(simple[0]["geojson"])["coordinates"]) == 2
    assert simple[0]["distance"] == tracks[0]["distance"]

    assert (await run(q, {"s": iso(10), "u": iso(0)}, errors=True)).errors


async def test_stats_per_bucket_and_per_place(run, seeded):
    buckets = (
        await run('query Q($s: DateTime!, $u: DateTime!) { stats(since: $s, until: $u, granularity: DAY) { start pointCount visitCount tripCount distance byMode { mode trips distance seconds } } }', {"s": iso(-600), "u": iso(1440)})
    )["stats"]
    assert len(buckets) == 1
    [b] = buckets
    assert (b["pointCount"], b["visitCount"], b["tripCount"], b["distance"]) == (14, 2, 2, 2300.0)
    assert sorted((m["mode"], m["trips"], m["seconds"]) for m in b["byMode"]) == [("BIKE", 1, 600.0), ("WALK", 1, 120.0)]

    places = (await run("query { placeStats { placeClientId place { name } visitCount seconds } }"))["placeStats"]
    assert [(p["placeClientId"], p["place"]["name"], p["seconds"]) for p in places] == [("work", "Office", 8400.0), ("home", "Home", 1800.0)]


async def test_reads_need_a_token(run, seeded):
    from lokate_server.schema import schema
    from tests.conftest import context

    ctx = context("nope")
    ctx.headers = {}
    result = await schema.execute("query { points { id } }", context_value=ctx)
    assert result.errors
