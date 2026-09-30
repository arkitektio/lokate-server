"""The backup protocol end to end over GraphQL, against a real postgres + PostGIS."""

import datetime
import threading

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import User
from django.db import connection, connections

from timeline import backup
from timeline.identity import Caller

pytestmark = pytest.mark.django_db(transaction=True)

UPLOAD = """
mutation Upload($points: [PointInput!]!) { uploadPoints(points: $points) { accepted duplicates } }
"""
REPLACE = """
mutation Replace($from: DateTime!, $visits: [VisitInput!]!, $trips: [TripInput!]!) {
  replaceSegments(from: $from, visits: $visits, trips: $trips) { deletedVisits deletedTrips visits trips }
}
"""
SYNC_PLACES = """
mutation Places($places: [PlaceInput!]!, $deleted: [DeletedInput!]!) {
  syncPlaces(places: $places, deleted: $deleted) { applied stale { clientId name updatedAt deletedAt } }
}
"""
CHANGES = """
query Changes($cursor: String, $limit: Int) {
  changes(cursor: $cursor, limit: $limit) {
    points { clientId deviceId ts lat lon acc }
    visits { clientId deviceId start end placeClientId }
    trips { clientId deviceId fromVisit toVisit mode }
    places { clientId name updatedAt }
    deletedPlaces
    nextCursor
    hasMore
  }
}
"""
STATE = "query { syncState { lastPointTs pointCount segmentsFrom } }"

T0 = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.timezone.utc)


def iso(minutes: float) -> str:
    return (T0 + datetime.timedelta(minutes=minutes)).isoformat()


def points(n: int, start: int = 0, prefix: str = "p") -> list[dict]:
    return [{"clientId": f"{prefix}{i}", "ts": iso(i), "lat": 48.2 + i * 1e-4, "lon": 16.37, "acc": 5.0} for i in range(start, start + n)]


def visit(client_id: str, start: float, end: float, place: str | None = None) -> dict:
    return {"clientId": client_id, "start": iso(start), "end": iso(end), "lat": 48.2, "lon": 16.37, "radius": 50.0, "pointCount": 10, "placeClientId": place}


def trip(client_id: str, start: float, end: float, from_visit: str | None = None, to_visit: str | None = None) -> dict:
    return {"clientId": client_id, "start": iso(start), "end": iso(end), "distance": 1200.0, "mode": "WALK", "fromVisit": from_visit, "toVisit": to_visit}


def place(client_id: str, name: str, updated: float) -> dict:
    return {"clientId": client_id, "name": name, "lat": 48.2, "lon": 16.37, "radius": 80.0, "updatedAt": iso(updated)}


def _sql(query: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchall()


sql = sync_to_async(_sql)


async def stamps(table: str) -> list[tuple]:
    return await sql(f"SELECT client_id, received_at FROM {table} ORDER BY client_id")


async def page_all(run, token: str = "phone-a", limit: int = 1000) -> list[dict]:
    pages, cursor = [], None
    while True:
        page = (await run(CHANGES, {"cursor": cursor, "limit": limit}, token=token))["changes"]
        pages.append(page)
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            return pages


# --------------------------------------------------------------------------- points


async def test_a_retried_point_batch_changes_nothing(run):
    batch = points(50)
    assert (await run(UPLOAD, {"points": batch}))["uploadPoints"] == {"accepted": 50, "duplicates": 0}
    before = await stamps("timeline_point")

    assert (await run(UPLOAD, {"points": batch}))["uploadPoints"] == {"accepted": 0, "duplicates": 50}
    assert await stamps("timeline_point") == before

    # Half old, half new: only the new half is stored.
    assert (await run(UPLOAD, {"points": points(50, start=25)}))["uploadPoints"] == {"accepted": 25, "duplicates": 25}


async def test_batches_over_1000_are_refused(run):
    result = await run(UPLOAD, {"points": points(1001)}, errors=True)
    assert result.errors[0].extensions["code"] == "BATCH_TOO_LARGE"
    assert "1000" in result.errors[0].message
    assert await stamps("timeline_point") == []

    ok = await run(UPLOAD, {"points": points(1000)})
    assert ok["uploadPoints"]["accepted"] == 1000


async def test_points_land_in_monthly_partitions(run):
    batch = [
        {"clientId": "aug", "ts": "2026-08-31T23:59:59+00:00", "lat": 1.0, "lon": 2.0},
        {"clientId": "sep", "ts": "2026-09-01T00:00:00+00:00", "lat": 1.0, "lon": 2.0},
        {"clientId": "old", "ts": "2025-01-15T10:00:00+00:00", "lat": 1.0, "lon": 2.0},
    ]
    assert (await run(UPLOAD, {"points": batch}))["uploadPoints"]["accepted"] == 3
    rows = await sql("SELECT tableoid::regclass::text, client_id, ST_AsText(geom) FROM timeline_point ORDER BY ts")
    assert rows == [
        ("timeline_point_y2025m01", "old", "POINT(2 1)"),
        ("timeline_point_y2026m08", "aug", "POINT(2 1)"),
        ("timeline_point_y2026m09", "sep", "POINT(2 1)"),
    ]


async def test_a_skewed_clock_does_not_stall_the_batch(run):
    """The phone retries a failed batch forever, so one odd point must not fail the rest."""
    batch = points(5) + [
        {"clientId": "epoch", "ts": "1970-01-01T00:00:00+00:00", "lat": 1.0, "lon": 2.0},
        {"clientId": "future", "ts": "2999-01-01T00:00:00+00:00", "lat": 1.0, "lon": 2.0},
    ]
    assert (await run(UPLOAD, {"points": batch}))["uploadPoints"] == {"accepted": 7, "duplicates": 0}
    assert (await run(UPLOAD, {"points": batch}))["uploadPoints"] == {"accepted": 0, "duplicates": 7}
    # A segment ending before it starts is stored as sent too.
    await run(REPLACE, {"from": "1970-01-01T00:00:00+00:00", "visits": [visit("odd", 10, 5)], "trips": []})


async def test_writes_need_a_device(run):
    result = await run(UPLOAD, {"points": points(1)}, token="nodevice", errors=True)
    assert result.errors[0].extensions["code"] == "NO_DEVICE"


async def test_sync_state_is_the_calling_devices(run):
    await run(UPLOAD, {"points": points(3)})
    await run(UPLOAD, {"points": points(7, prefix="b")}, token="phone-b")
    await run(REPLACE, {"from": iso(0), "visits": [visit("v1", 1, 2)], "trips": []})

    state = (await run(STATE))["syncState"]
    assert state["pointCount"] == 3
    assert datetime.datetime.fromisoformat(state["lastPointTs"]) == T0 + datetime.timedelta(minutes=2)
    assert datetime.datetime.fromisoformat(state["segmentsFrom"]) == T0

    assert (await run(STATE, token="phone-b"))["syncState"]["pointCount"] == 7
    assert (await run(STATE, token="other"))["syncState"] == {"lastPointTs": None, "pointCount": 0, "segmentsFrom": None}


# --------------------------------------------------------------------------- segments


async def test_replace_segments_only_touches_the_calling_devices_rows(run):
    await run(REPLACE, {"from": iso(0), "visits": [visit("v1", 0, 10), visit("v2", 20, 30)], "trips": [trip("t1", 10, 20, "v1", "v2")]})
    await run(REPLACE, {"from": iso(0), "visits": [visit("v1", 0, 10), visit("v9", 40, 50)], "trips": [trip("t1", 10, 20)]}, token="phone-b")
    # Another user whose phone reports the very same device id.
    await run(REPLACE, {"from": iso(0), "visits": [visit("v1", 0, 10)], "trips": []}, token="other")

    # Phone A resegments from minute 15: v2 is recomputed as v3, t1 (before `from`) stays.
    result = (await run(REPLACE, {"from": iso(15), "visits": [visit("v3", 22, 30)], "trips": []}))["replaceSegments"]
    assert result == {"deletedVisits": 1, "deletedTrips": 0, "visits": 1, "trips": 0}

    assert await sql(
        "SELECT d.device_id, u.sub, v.client_id FROM timeline_visit v JOIN timeline_device d ON d.id = v.device_id JOIN authentikate_user u ON u.id = v.user_id ORDER BY 2, 1, 3"
    ) == [
        ("phone-a", "1", "v1"),
        ("phone-a", "1", "v3"),
        ("phone-b", "1", "v1"),
        ("phone-b", "1", "v9"),
        ("phone-a", "9", "v1"),
    ]
    assert await sql("SELECT count(*) FROM timeline_trip") == [(2,)]


async def test_a_retried_replace_changes_nothing(run):
    variables = {"from": iso(0), "visits": [visit("v1", 0, 10, "home"), visit("v2", 20, 30)], "trips": [trip("t1", 10, 20, "v1", "v2")]}
    await run(REPLACE, variables)
    before = await stamps("timeline_visit"), await stamps("timeline_trip")

    result = (await run(REPLACE, variables))["replaceSegments"]
    assert result == {"deletedVisits": 0, "deletedTrips": 0, "visits": 2, "trips": 1}
    assert (await stamps("timeline_visit"), await stamps("timeline_trip")) == before

    # A changed visit gets a new stamp; the unchanged one keeps its own.
    variables["visits"][1]["end"] = iso(35)
    await run(REPLACE, variables)
    after = dict(await stamps("timeline_visit"))
    assert after["v1"] == dict(before[0])["v1"]
    assert after["v2"] > dict(before[0])["v2"]


async def test_replace_refuses_segments_before_from(run):
    result = await run(REPLACE, {"from": iso(15), "visits": [visit("v1", 0, 10)], "trips": []}, errors=True)
    assert "before `from`" in result.errors[0].message
    assert await stamps("timeline_visit") == []


# --------------------------------------------------------------------------- places


async def test_places_last_write_wins(run):
    await run(SYNC_PLACES, {"places": [place("home", "Home", 10)], "deleted": []})

    # Phone B holds an older edit: it loses and is handed the server's copy.
    older = (await run(SYNC_PLACES, {"places": [place("home", "Old home", 5)], "deleted": []}, token="phone-b"))["syncPlaces"]
    assert older["applied"] == 0
    assert [(p["clientId"], p["name"]) for p in older["stale"]] == [("home", "Home")]

    # A newer edit wins.
    newer = (await run(SYNC_PLACES, {"places": [place("home", "New home", 20)], "deleted": []}, token="phone-b"))["syncPlaces"]
    assert newer == {"applied": 1, "stale": []}

    # The same update again is a no-op: applied, not stale, and no new stamp.
    before = await stamps("timeline_place")
    again = (await run(SYNC_PLACES, {"places": [place("home", "New home", 20)], "deleted": []}, token="phone-b"))["syncPlaces"]
    assert again == {"applied": 1, "stale": []}
    assert await stamps("timeline_place") == before


async def test_a_tombstone_beats_an_older_update(run):
    await run(SYNC_PLACES, {"places": [place("gym", "Gym", 10)], "deleted": []})
    deleted = (await run(SYNC_PLACES, {"places": [], "deleted": [{"clientId": "gym", "deletedAt": iso(20)}]}))["syncPlaces"]
    assert deleted == {"applied": 1, "stale": []}

    # Phone B still has the place, edited before the deletion: it must not come back.
    resurrect = (await run(SYNC_PLACES, {"places": [place("gym", "Gym (B)", 15)], "deleted": []}, token="phone-b"))["syncPlaces"]
    assert resurrect["applied"] == 0
    assert [(p["clientId"], p["deletedAt"] is not None) for p in resurrect["stale"]] == [("gym", True)]

    # On a tie the tombstone wins too.
    tie = (await run(SYNC_PLACES, {"places": [place("gym", "Gym (tie)", 20)], "deleted": []}, token="phone-b"))["syncPlaces"]
    assert tie["applied"] == 0 and tie["stale"][0]["deletedAt"] is not None

    # A tombstone retried is a no-op.
    before = await stamps("timeline_place")
    retry = (await run(SYNC_PLACES, {"places": [], "deleted": [{"clientId": "gym", "deletedAt": iso(20)}]}))["syncPlaces"]
    assert retry == {"applied": 1, "stale": []}
    assert await stamps("timeline_place") == before


async def test_a_tombstone_loses_to_a_newer_update(run):
    await run(SYNC_PLACES, {"places": [place("cafe", "Cafe", 30)], "deleted": []})
    result = (await run(SYNC_PLACES, {"places": [], "deleted": [{"clientId": "cafe", "deletedAt": iso(20)}]}, token="phone-b"))["syncPlaces"]
    assert result["applied"] == 0
    assert result["stale"] == [{"clientId": "cafe", "name": "Cafe", "updatedAt": iso(30), "deletedAt": None}]


async def test_a_tombstone_for_an_unknown_place_is_kept(run):
    result = (await run(SYNC_PLACES, {"places": [], "deleted": [{"clientId": "never-seen", "deletedAt": iso(20)}]}))["syncPlaces"]
    assert result == {"applied": 1, "stale": []}
    # ...so an older copy uploaded later from another phone stays deleted.
    late = (await run(SYNC_PLACES, {"places": [place("never-seen", "Ghost", 10)], "deleted": []}, token="phone-b"))["syncPlaces"]
    assert late["applied"] == 0
    page = (await run(CHANGES))["changes"]
    assert page["deletedPlaces"] == ["never-seen"] and page["places"] == []


# --------------------------------------------------------------------------- changes


async def test_changes_pages_through_everything_exactly_once(run):
    await run(UPLOAD, {"points": points(23)})
    await run(UPLOAD, {"points": points(11, prefix="b")}, token="phone-b")
    await run(REPLACE, {"from": iso(0), "visits": [visit(f"v{i}", i * 10, i * 10 + 5) for i in range(4)], "trips": [trip("t1", 5, 10)]})
    await run(SYNC_PLACES, {"places": [place("home", "Home", 1), place("work", "Work", 1)], "deleted": [{"clientId": "gone", "deletedAt": iso(3)}]})
    await run(UPLOAD, {"points": points(5, prefix="other")}, token="other")  # never in phone-a's pages

    pages = await page_all(run, limit=7)
    assert all(not p["hasMore"] for p in pages[-1:]) and all(p["hasMore"] for p in pages[:-1])
    assert len(pages) == 6  # 23 + 11 + 4 + 1 + 3 = 42 rows, 7 per page

    seen = [("point", p["deviceId"], p["clientId"]) for page in pages for p in page["points"]]
    seen += [("visit", v["deviceId"], v["clientId"]) for page in pages for v in page["visits"]]
    seen += [("trip", t["deviceId"], t["clientId"]) for page in pages for t in page["trips"]]
    seen += [("place", None, p["clientId"]) for page in pages for p in page["places"]]
    seen += [("deleted", None, c) for page in pages for c in page["deletedPlaces"]]
    assert len(seen) == len(set(seen)) == 42
    assert {s for s in seen if s[0] == "point" and s[1] == "phone-b"} == {("point", "phone-b", f"b{i}") for i in range(11)}
    assert not any(s[2].startswith("other") for s in seen)

    # Past the end: an empty page that keeps the cursor.
    tail = (await run(CHANGES, {"cursor": pages[-1]["nextCursor"]}))["changes"]
    assert tail["points"] == [] and tail["hasMore"] is False and tail["nextCursor"] == pages[-1]["nextCursor"]

    # A later change shows up after the cursor, once.
    await run(UPLOAD, {"points": points(1, start=100)})
    later = (await run(CHANGES, {"cursor": pages[-1]["nextCursor"]}))["changes"]
    assert [p["clientId"] for p in later["points"]] == ["p100"]


async def test_changes_limit_and_cursor_are_checked(run):
    assert (await run(CHANGES, {"limit": 1001}, errors=True)).errors[0].extensions["code"] == "BATCH_TOO_LARGE"
    assert (await run(CHANGES, {"cursor": "nope"}, errors=True)).errors[0].extensions["code"] == "INVALID_CURSOR"


def _in_thread(fn):  # noqa: ANN001, ANN202
    """Run ``fn`` on its own thread (so its own connection); returns (thread, results, errors)."""
    results: list = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            results.append(fn())
        except BaseException as error:  # noqa: BLE001 -- surfaced by the caller
            errors.append(error)
        finally:
            connections.close_all()

    thread = threading.Thread(target=target)
    thread.start()
    return thread, results, errors


def _waiting_on_advisory_lock() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        return cursor.fetchone()[0] > 0


def test_changes_skips_nothing_when_writers_commit_out_of_order(transactional_db):
    """A stamp drawn first must never commit after a reader has paged past a later one.

    Phone A draws its stamps and stays uncommitted; phone B (same user) writes meanwhile; a
    reader pages. Without the per-user lock B commits higher stamps first, the reader's cursor
    passes A's, and A's rows are skipped for good. With it, B waits for A.
    """
    user, _ = User.objects.get_or_create(sub="1", iss="static_issuer", defaults={"username": "static_issuer_1"})
    phone_a = Caller(user=user, device_id="phone-a", client_id="test")
    phone_b = Caller(user=user, device_id="phone-b", client_id="test")
    batch = lambda prefix: [backup.PointIn(client_id=f"{prefix}{i}", ts=T0 + datetime.timedelta(minutes=i), lat=1.0, lon=2.0) for i in range(5)]  # noqa: E731

    def page_from(cursor: str | None) -> tuple[list[str], str | None]:
        seen: list[str] = []
        while True:
            page = backup.changes(phone_a, cursor, limit=3)
            seen += [p.client_id for p in page.points]
            cursor = page.next_cursor
            if not page.has_more:
                return seen, cursor

    backup.upload_points(phone_a, batch("warm"))  # creates the partition and both devices' user
    first, cursor = page_from(None)
    assert len(first) == 5

    from django.db import transaction

    with transaction.atomic():
        backup.upload_points(phone_a, batch("a"))  # stamps drawn, not committed; the lock is held
        writer, _, writer_errors = _in_thread(lambda: backup.upload_points(phone_b, batch("b")))
        while writer.is_alive() and not _waiting_on_advisory_lock():
            writer.join(0.01)
        reader, pages, reader_errors = _in_thread(lambda: page_from(cursor))
        reader.join()
    writer.join()

    assert not writer_errors and not reader_errors, (writer_errors, reader_errors)
    during, cursor = pages[0]
    tail, results, errors = _in_thread(lambda: page_from(cursor))
    tail.join()
    assert not errors, errors
    later = results[0][0]

    seen = during + later
    assert sorted(seen) == sorted([f"a{i}" for i in range(5)] + [f"b{i}" for i in range(5)]), f"during={during} later={later}"


# --------------------------------------------------------------------------- your data

DELETE = 'mutation Delete($confirm: String!) { deleteServerCopy(confirm: $confirm) }'


async def test_delete_server_copy_needs_confirmation_and_spares_other_users(run):
    await run(UPLOAD, {"points": points(4)})
    await run(UPLOAD, {"points": points(2, prefix="b")}, token="phone-b")
    await run(REPLACE, {"from": iso(0), "visits": [visit("v1", 0, 5)], "trips": [trip("t1", 5, 9)]})
    await run(SYNC_PLACES, {"places": [place("home", "Home", 1)], "deleted": []})
    await run(UPLOAD, {"points": points(3)}, token="other")

    refused = await run(DELETE, {"confirm": "yes"}, errors=True)
    assert refused.errors[0].extensions["code"] == "CONFIRMATION_REQUIRED"

    assert (await run(DELETE, {"confirm": "DELETE"}))["deleteServerCopy"] == 4 + 2 + 1 + 1 + 1
    page = (await run(CHANGES))["changes"]
    assert page["points"] == page["visits"] == page["trips"] == page["places"] == []
    assert (await run(STATE, token="other"))["syncState"]["pointCount"] == 3
