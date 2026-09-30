"""The backup protocol: what each lokate operation does to the database.

Plain synchronous functions over plain dataclasses, so they read (and test) without GraphQL;
:mod:`timeline.graphql` only converts and calls them in a thread.

Rules they keep:

* **Every write is safe to repeat.** Each one upserts on its unique key, and an upsert that
  changes nothing neither rewrites the row nor draws a new change stamp, so a retried batch
  leaves the database exactly as it was.
* **One user's writes are serialised** by that user's advisory lock (:func:`user_lock`), taken
  before any stamp is drawn and held to commit. That is what makes the ``changes`` cursor
  exact (see :mod:`timeline.models`). Different users never wait on each other.
* **Batches are at most** :data:`MAX_BATCH` **rows**; a larger one is refused, not truncated.
* **Only a batch the phone's own algorithm can never produce is refused** (too large, a
  clientId twice, no device, a segment before ``from``). Row *values* are stored as sent:
  the phone retries a failed batch forever and keeps unsynced points until they upload, so
  refusing one point from a skewed clock would stall its backup for good. (A batch names
  at most 1000 months, which bounds the partitions one call can create.)
"""

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Any

from authentikate.models import User
from django.db import connection, transaction
from django.utils import timezone
from graphql import GraphQLError

from timeline import models
from timeline.identity import Caller
from timeline.partitions import ensure_point_partitions

logger = logging.getLogger(__name__)

MAX_BATCH = 1000
"""The most rows one list of one call may carry."""

CONFIRM_DELETE = "DELETE"
"""What ``deleteServerCopy`` must be passed to go ahead."""

_USER_LOCK_BASE = 0x6C6F_6B61 << 32
"""The bigint advisory-lock space for per-user locks ("loka" in the high half)."""


def invalid(message: str, code: str = "INVALID_INPUT") -> GraphQLError:
    """An error the phone caused and can act on; ``extensions.code`` says which."""
    return GraphQLError(message, extensions={"code": code})


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class PointIn:
    client_id: str
    ts: datetime.datetime
    lat: float
    lon: float
    acc: float | None = None
    speed: float | None = None
    heading: float | None = None
    alt: float | None = None


@dataclass(frozen=True)
class VisitIn:
    client_id: str
    start: datetime.datetime
    end: datetime.datetime
    lat: float
    lon: float
    radius: float
    point_count: int
    place_client_id: str | None = None


@dataclass(frozen=True)
class TripIn:
    client_id: str
    start: datetime.datetime
    end: datetime.datetime
    distance: float
    mode: str
    from_visit: str | None = None
    to_visit: str | None = None


@dataclass(frozen=True)
class PlaceIn:
    client_id: str
    name: str
    lat: float
    lon: float
    radius: float
    updated_at: datetime.datetime


@dataclass(frozen=True)
class DeletedIn:
    client_id: str
    deleted_at: datetime.datetime


# --------------------------------------------------------------------------- results


@dataclass
class UploadResult:
    accepted: int
    duplicates: int


@dataclass
class ReplaceResult:
    deleted_visits: int
    deleted_trips: int
    visits: int
    trips: int


@dataclass
class PlaceSyncResult:
    applied: int
    stale: list[models.Place]


@dataclass
class SyncState:
    last_point_ts: datetime.datetime | None
    point_count: int
    segments_from: datetime.datetime | None


@dataclass
class ChangeSet:
    points: list[models.Point]
    visits: list[models.Visit]
    trips: list[models.Trip]
    places: list[models.Place]
    deleted_places: list[str]
    next_cursor: str | None
    has_more: bool


# --------------------------------------------------------------------------- checks


def _aware(value: datetime.datetime) -> datetime.datetime:
    """A timestamp without an offset is taken as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)


def check_batch(name: str, rows: list) -> None:
    if len(rows) > MAX_BATCH:
        raise invalid(f"{name}: {len(rows)} rows in one call; at most {MAX_BATCH} are accepted. Send them in batches.", "BATCH_TOO_LARGE")


def _check_client_ids(name: str, ids: list[str]) -> None:
    seen: set[str] = set()
    for client_id in ids:
        if not client_id or len(client_id) > 255:
            raise invalid(f"{name}: a clientId must be 1 to 255 characters.")
        if client_id in seen:
            raise invalid(f"{name}: clientId {client_id!r} appears twice in one call.")
        seen.add(client_id)


# --------------------------------------------------------------------------- locking & devices


def user_lock(user_id: int) -> None:
    """Serialise this user's writes until the transaction ends (see the module docstring)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_USER_LOCK_BASE + int(user_id)])


def _device(user: User, device_id: str) -> models.Device:
    device, _ = models.Device.objects.get_or_create(user=user, device_id=device_id)
    return device


def _touch(device: models.Device, **fields: Any) -> None:
    models.Device.objects.filter(pk=device.pk).update(last_upload_at=timezone.now(), **fields)


# --------------------------------------------------------------------------- points


def upload_points(caller: Caller, points: list[PointIn]) -> UploadResult:
    """Store points; ones already stored (same device, clientId and ts) count as duplicates."""
    device_id = caller.require_device()
    check_batch("points", points)
    _check_client_ids("points", [p.client_id for p in points])
    rows = [(p, _aware(p.ts)) for p in points]
    if not rows:
        return UploadResult(accepted=0, duplicates=0)

    ensure_point_partitions([ts for _, ts in rows])
    with transaction.atomic():
        user_lock(caller.user.pk)
        device = _device(caller.user, device_id)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO timeline_point (user_id, device_id, client_id, ts, lat, lon, acc, speed, heading, alt)
                SELECT %s, %s, p.client_id, p.ts, p.lat, p.lon, p.acc, p.speed, p.heading, p.alt
                FROM unnest(%s::text[], %s::timestamptz[], %s::float8[], %s::float8[], %s::float8[], %s::float8[], %s::float8[], %s::float8[])
                    AS p(client_id, ts, lat, lon, acc, speed, heading, alt)
                ON CONFLICT (device_id, client_id, ts) DO NOTHING
                """,
                [
                    caller.user.pk,
                    device.pk,
                    [p.client_id for p, _ in rows],
                    [ts for _, ts in rows],
                    [p.lat for p, _ in rows],
                    [p.lon for p, _ in rows],
                    [p.acc for p, _ in rows],
                    [p.speed for p, _ in rows],
                    [p.heading for p, _ in rows],
                    [p.alt for p, _ in rows],
                ],
            )
            accepted = cursor.rowcount
        _touch(device)
    return UploadResult(accepted=accepted, duplicates=len(rows) - accepted)


# --------------------------------------------------------------------------- segments


_VISIT_UPSERT = """
INSERT INTO timeline_visit (user_id, device_id, client_id, start, "end", lat, lon, radius, point_count, place_client_id)
SELECT %s, %s, v.client_id, v.start, v."end", v.lat, v.lon, v.radius, v.point_count, v.place_client_id
FROM unnest(%s::text[], %s::timestamptz[], %s::timestamptz[], %s::float8[], %s::float8[], %s::float8[], %s::int[], %s::text[])
    AS v(client_id, start, "end", lat, lon, radius, point_count, place_client_id)
ON CONFLICT (device_id, client_id) DO UPDATE SET
    start = EXCLUDED.start, "end" = EXCLUDED."end", lat = EXCLUDED.lat, lon = EXCLUDED.lon,
    radius = EXCLUDED.radius, point_count = EXCLUDED.point_count, place_client_id = EXCLUDED.place_client_id,
    received_at = nextval('timeline_change_seq')
WHERE (timeline_visit.start, timeline_visit."end", timeline_visit.lat, timeline_visit.lon, timeline_visit.radius, timeline_visit.point_count, timeline_visit.place_client_id)
    IS DISTINCT FROM (EXCLUDED.start, EXCLUDED."end", EXCLUDED.lat, EXCLUDED.lon, EXCLUDED.radius, EXCLUDED.point_count, EXCLUDED.place_client_id)
"""

_TRIP_UPSERT = """
INSERT INTO timeline_trip (user_id, device_id, client_id, start, "end", from_visit, to_visit, distance, mode)
SELECT %s, %s, t.client_id, t.start, t."end", t.from_visit, t.to_visit, t.distance, t.mode
FROM unnest(%s::text[], %s::timestamptz[], %s::timestamptz[], %s::text[], %s::text[], %s::float8[], %s::text[])
    AS t(client_id, start, "end", from_visit, to_visit, distance, mode)
ON CONFLICT (device_id, client_id) DO UPDATE SET
    start = EXCLUDED.start, "end" = EXCLUDED."end", from_visit = EXCLUDED.from_visit, to_visit = EXCLUDED.to_visit,
    distance = EXCLUDED.distance, mode = EXCLUDED.mode,
    received_at = nextval('timeline_change_seq')
WHERE (timeline_trip.start, timeline_trip."end", timeline_trip.from_visit, timeline_trip.to_visit, timeline_trip.distance, timeline_trip.mode)
    IS DISTINCT FROM (EXCLUDED.start, EXCLUDED."end", EXCLUDED.from_visit, EXCLUDED.to_visit, EXCLUDED.distance, EXCLUDED.mode)
"""


def replace_segments(caller: Caller, from_: datetime.datetime, visits: list[VisitIn], trips: list[TripIn]) -> ReplaceResult:
    """Make this device's visits and trips from ``from_`` on exactly the ones sent, in one transaction.

    Sent rows are upserted (an unchanged one is left alone, stamp and all); the device's rows
    starting at or after ``from_`` that were not sent are deleted. Nothing of another device,
    or before ``from_``, is touched — so a sent row starting before ``from_`` is refused.
    """
    device_id = caller.require_device()
    from_ = _aware(from_)
    check_batch("visits", visits)
    check_batch("trips", trips)
    _check_client_ids("visits", [v.client_id for v in visits])
    _check_client_ids("trips", [t.client_id for t in trips])
    valid_modes = set(models.TripMode.values)
    for v in visits:
        if _aware(v.start) < from_:
            raise invalid(f"visit {v.client_id} starts before `from`; replaceSegments only replaces from `from` on.")
    for t in trips:
        if _aware(t.start) < from_:
            raise invalid(f"trip {t.client_id} starts before `from`; replaceSegments only replaces from `from` on.")
        if t.mode not in valid_modes:
            raise invalid(f"trip {t.client_id}: unknown mode {t.mode!r}.")

    with transaction.atomic():
        user_lock(caller.user.pk)
        device = _device(caller.user, device_id)
        with connection.cursor() as cursor:
            if visits:
                cursor.execute(
                    _VISIT_UPSERT,
                    [
                        caller.user.pk,
                        device.pk,
                        [v.client_id for v in visits],
                        [_aware(v.start) for v in visits],
                        [_aware(v.end) for v in visits],
                        [v.lat for v in visits],
                        [v.lon for v in visits],
                        [v.radius for v in visits],
                        [v.point_count for v in visits],
                        [v.place_client_id for v in visits],
                    ],
                )
            if trips:
                cursor.execute(
                    _TRIP_UPSERT,
                    [
                        caller.user.pk,
                        device.pk,
                        [t.client_id for t in trips],
                        [_aware(t.start) for t in trips],
                        [_aware(t.end) for t in trips],
                        [t.from_visit for t in trips],
                        [t.to_visit for t in trips],
                        [t.distance for t in trips],
                        [t.mode for t in trips],
                    ],
                )
            cursor.execute(
                "DELETE FROM timeline_visit WHERE device_id = %s AND start >= %s AND NOT (client_id = ANY(%s::text[]))",
                [device.pk, from_, [v.client_id for v in visits]],
            )
            deleted_visits = cursor.rowcount
            cursor.execute(
                "DELETE FROM timeline_trip WHERE device_id = %s AND start >= %s AND NOT (client_id = ANY(%s::text[]))",
                [device.pk, from_, [t.client_id for t in trips]],
            )
            deleted_trips = cursor.rowcount
        _touch(device, segments_from=from_)
    return ReplaceResult(deleted_visits=deleted_visits, deleted_trips=deleted_trips, visits=len(visits), trips=len(trips))


# --------------------------------------------------------------------------- places


def _update_place(row: models.Place, **fields: Any) -> None:
    """Write ``fields`` with a fresh stamp, and keep ``row`` in step for the rest of the batch."""
    models.Place.objects.filter(pk=row.pk).update(received_at=models.NextStamp(), **fields)
    for name, value in fields.items():
        setattr(row, name, value)


def sync_places(caller: Caller, places: list[PlaceIn], deleted: list[DeletedIn]) -> PlaceSyncResult:
    """Merge the phone's places into the user's, last write wins on ``updatedAt``.

    * An update newer than the server's copy is applied; an equal one is a no-op (a retry).
    * A tombstone wins over an update at the same instant or older, and loses to a newer one
      (a genuinely newer edit on another phone brings the place back).
    * Where the server's copy wins, it is returned in ``stale`` and the phone takes it —
      including a tombstone (``deletedAt`` set), which tells the phone to delete.

    ``applied`` counts what the server now agrees with, retries included.
    """
    check_batch("places", places)
    check_batch("deleted", deleted)
    _check_client_ids("places", [p.client_id for p in places])
    _check_client_ids("deleted", [d.client_id for d in deleted])

    applied = 0
    stale: dict[int, models.Place] = {}
    with transaction.atomic():
        user_lock(caller.user.pk)
        existing = {p.client_id: p for p in models.Place.objects.filter(user=caller.user, client_id__in=[p.client_id for p in places] + [d.client_id for d in deleted])}

        for incoming in places:
            updated_at = _aware(incoming.updated_at)
            fields = {"name": incoming.name, "lat": incoming.lat, "lon": incoming.lon, "radius": incoming.radius, "updated_at": updated_at, "deleted_at": None}
            row = existing.get(incoming.client_id)
            if row is None:
                existing[incoming.client_id] = models.Place.objects.create(user=caller.user, client_id=incoming.client_id, **fields)
                applied += 1
            elif row.deleted_at is not None:
                if updated_at > row.deleted_at:
                    _update_place(row, **fields)
                    applied += 1
                else:
                    stale[row.pk] = row
            elif updated_at > row.updated_at:
                _update_place(row, **fields)
                applied += 1
            elif updated_at == row.updated_at:
                applied += 1
            else:
                stale[row.pk] = row

        for tombstone in deleted:
            deleted_at = _aware(tombstone.deleted_at)
            row = existing.get(tombstone.client_id)
            if row is None:
                existing[tombstone.client_id] = models.Place.objects.create(user=caller.user, client_id=tombstone.client_id, updated_at=deleted_at, deleted_at=deleted_at)
                applied += 1
            elif row.deleted_at is not None:
                applied += 1  # already gone: a retry, or another phone deleted it too
            elif deleted_at >= row.updated_at:
                _update_place(row, deleted_at=deleted_at)
                applied += 1
            else:
                stale[row.pk] = row

        fresh = list(models.Place.objects.filter(pk__in=list(stale)).order_by("client_id"))
    return PlaceSyncResult(applied=applied, stale=fresh)


# --------------------------------------------------------------------------- reads


def sync_state(caller: Caller) -> SyncState:
    """The calling device's watermarks: its newest point, how many, and its last segment window."""
    device_id = caller.require_device()
    device = models.Device.objects.filter(user=caller.user, device_id=device_id).first()
    if device is None:
        state = SyncState(last_point_ts=None, point_count=0, segments_from=None)
    else:
        with connection.cursor() as cursor:
            cursor.execute("SELECT max(ts), count(*) FROM timeline_point WHERE device_id = %s", [device.pk])
            last, count = cursor.fetchone()
        state = SyncState(last_point_ts=last, point_count=count, segments_from=device.segments_from)
    return state


_CHANGES = """
WITH p AS (
    SELECT 'point' AS kind, pt.received_at, jsonb_build_object(
        'id', pt.id, 'device_id', pt.device_id, 'client_id', pt.client_id, 'ts', pt.ts, 'lat', pt.lat, 'lon', pt.lon,
        'acc', pt.acc, 'speed', pt.speed, 'heading', pt.heading, 'alt', pt.alt) AS data
    FROM timeline_point pt
    WHERE pt.user_id = %(user)s AND pt.received_at > %(after)s ORDER BY pt.received_at LIMIT %(take)s
), v AS (
    SELECT 'visit', vi.received_at, jsonb_build_object(
        'id', vi.id, 'device_id', vi.device_id, 'client_id', vi.client_id, 'start', vi.start, 'end', vi."end", 'lat', vi.lat, 'lon', vi.lon,
        'radius', vi.radius, 'point_count', vi.point_count, 'place_client_id', vi.place_client_id)
    FROM timeline_visit vi
    WHERE vi.user_id = %(user)s AND vi.received_at > %(after)s ORDER BY vi.received_at LIMIT %(take)s
), t AS (
    SELECT 'trip', tr.received_at, jsonb_build_object(
        'id', tr.id, 'device_id', tr.device_id, 'client_id', tr.client_id, 'start', tr.start, 'end', tr."end", 'from_visit', tr.from_visit,
        'to_visit', tr.to_visit, 'distance', tr.distance, 'mode', tr.mode)
    FROM timeline_trip tr
    WHERE tr.user_id = %(user)s AND tr.received_at > %(after)s ORDER BY tr.received_at LIMIT %(take)s
), pl AS (
    SELECT 'place', pc.received_at, jsonb_build_object(
        'id', pc.id, 'client_id', pc.client_id, 'name', pc.name, 'lat', pc.lat, 'lon', pc.lon, 'radius', pc.radius,
        'updated_at', pc.updated_at, 'deleted_at', pc.deleted_at)
    FROM timeline_place pc
    WHERE pc.user_id = %(user)s AND pc.received_at > %(after)s ORDER BY pc.received_at LIMIT %(take)s
)
SELECT kind, received_at, data FROM (
    SELECT * FROM p UNION ALL SELECT * FROM v UNION ALL SELECT * FROM t UNION ALL SELECT * FROM pl
) AS changed ORDER BY received_at LIMIT %(take)s
"""

_TIME_KEYS = ("ts", "start", "end", "updated_at", "deleted_at")


def _row(data: Any) -> dict[str, Any]:
    row = json.loads(data) if isinstance(data, str) else dict(data)
    for key in _TIME_KEYS:
        if row.get(key) is not None:
            row[key] = datetime.datetime.fromisoformat(row[key])
    return row


def parse_cursor(cursor: str | None) -> int:
    if cursor in (None, ""):
        return 0
    try:
        value = int(cursor)
    except ValueError:
        raise invalid(f"{cursor!r} is not a cursor this server issued.", "INVALID_CURSOR") from None
    if value < 0:
        raise invalid(f"{cursor!r} is not a cursor this server issued.", "INVALID_CURSOR")
    return value


def changes(caller: Caller, cursor: str | None, limit: int = MAX_BATCH) -> ChangeSet:
    """Everything of the user's, from all their devices, changed after ``cursor``, oldest first.

    One statement reads all four tables, so one snapshot: a transaction committing between
    two separate reads could otherwise land rows below the cursor they then advance past.
    Rows carry ``deviceId``, so a reinstall under a new device id can still pull its history.
    """
    if not 1 <= limit <= MAX_BATCH:
        raise invalid(f"limit must be between 1 and {MAX_BATCH}.", "BATCH_TOO_LARGE")
    after = parse_cursor(cursor)
    with connection.cursor() as db:
        db.execute(_CHANGES, {"user": caller.user.pk, "after": after, "take": limit + 1})
        rows = db.fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    # Devices are read after the snapshot: a row's device committed with (or before) it.
    devices = {d.pk: d for d in models.Device.objects.filter(user=caller.user)}
    result = ChangeSet(points=[], visits=[], trips=[], places=[], deleted_places=[], next_cursor=cursor, has_more=has_more)
    for kind, _stamp, data in rows:
        row = _row(data)
        if kind == "place":
            if row["deleted_at"] is not None:
                result.deleted_places.append(row["client_id"])
            else:
                result.places.append(models.Place(user=caller.user, **row))
            continue
        model, bucket = {"point": (models.Point, result.points), "visit": (models.Visit, result.visits), "trip": (models.Trip, result.trips)}[kind]
        device = devices.get(row.pop("device_id"))
        if device is None:
            continue  # the server copy was deleted between the two reads
        bucket.append(model(user=caller.user, device=device, **row))
    if rows:
        result.next_cursor = str(rows[-1][1])
    return result


# --------------------------------------------------------------------------- your data


def delete_server_copy(caller: Caller, confirm: str) -> int:
    """Delete all of the user's data on this server; returns how many points, visits, trips and places went.

    Its devices go too. The phone keeps its own copy; with backup
    still on, its next sync uploads it again.
    """
    if confirm != CONFIRM_DELETE:
        raise invalid(f'Pass confirm: "{CONFIRM_DELETE}" to delete the server copy.', "CONFIRMATION_REQUIRED")
    with transaction.atomic():
        user_lock(caller.user.pk)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM timeline_point WHERE user_id = %s", [caller.user.pk])
            count = cursor.rowcount
        count += models.Visit.objects.filter(user=caller.user).delete()[0]
        count += models.Trip.objects.filter(user=caller.user).delete()[0]
        count += models.Place.objects.filter(user=caller.user).delete()[0]
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM timeline_device WHERE user_id = %s", [caller.user.pk])
    logger.info("Deleted the server copy of user %s (%d rows)", caller.user.pk, count)
    return count
