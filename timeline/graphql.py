"""The lokate GraphQL API: types, inputs and resolvers over :mod:`timeline.backup`.

Resolvers only convert: the protocol lives in ``backup``, which runs synchronously in a
thread (``sync_to_async``), one transaction per call.
"""

import datetime
from enum import Enum
from typing import Annotated, Any

import strawberry
from asgiref.sync import sync_to_async
from kante.types import Info

from timeline import backup, models
from timeline.identity import caller

# --------------------------------------------------------------------------- types


@strawberry.enum(description="How a trip was travelled, as the phone classified it.")
class TripMode(Enum):
    WALK = "WALK"
    BIKE = "BIKE"
    VEHICLE = "VEHICLE"
    UNKNOWN = "UNKNOWN"


@strawberry.type(description="One location fix.")
class Point:
    client_id: strawberry.ID
    device_id: strawberry.ID = strawberry.field(description="The device (token client_device) that recorded it.")
    ts: datetime.datetime
    lat: float
    lon: float
    acc: float | None
    speed: float | None
    heading: float | None
    alt: float | None


@strawberry.type(description="A stay at one spot.")
class Visit:
    client_id: strawberry.ID
    device_id: strawberry.ID
    start: datetime.datetime
    end: datetime.datetime
    lat: float
    lon: float
    radius: float
    point_count: int
    place_client_id: strawberry.ID | None


@strawberry.type(description="A movement between two visits.")
class Trip:
    client_id: strawberry.ID
    device_id: strawberry.ID
    start: datetime.datetime
    end: datetime.datetime
    from_visit: strawberry.ID | None
    to_visit: strawberry.ID | None
    distance: float
    mode: TripMode


@strawberry.type(description="A named place, shared by all of a user's phones. A tombstone has deletedAt set and nothing else but its key.")
class Place:
    client_id: strawberry.ID
    name: str | None
    lat: float | None
    lon: float | None
    radius: float | None
    updated_at: datetime.datetime
    deleted_at: datetime.datetime | None


@strawberry.type(description="The calling device's watermarks.")
class SyncState:
    last_point_ts: datetime.datetime | None
    point_count: int
    segments_from: datetime.datetime | None = strawberry.field(description="The `from` of this device's last replaceSegments.")


@strawberry.type
class UploadResult:
    accepted: int
    duplicates: int


@strawberry.type
class ReplaceResult:
    deleted_visits: int
    deleted_trips: int
    visits: int
    trips: int


@strawberry.type
class PlaceSyncResult:
    applied: int
    stale: list[Place] = strawberry.field(description="Places whose server copy is newer; the phone takes them (deletedAt set: delete it).")


@strawberry.type(description="One page of everything the user has, from all their devices, in change order.")
class ChangeSet:
    points: list[Point]
    visits: list[Visit]
    trips: list[Trip]
    places: list[Place]
    deleted_places: list[strawberry.ID]
    next_cursor: str | None
    has_more: bool


@strawberry.type(description="One read of your data.")
class AccessLogEntry:
    id: strawberry.ID
    operation: str
    range: str
    rows: int
    at: datetime.datetime
    device_id: str | None = strawberry.field(description="The reading token's client_device claim.")
    client_id: str | None = strawberry.field(description="The reading token's OAuth client.")


@strawberry.type(description="How long the server keeps your points and segments.")
class Retention:
    days: int | None = strawberry.field(description="Null: forever.")


# --------------------------------------------------------------------------- inputs


@strawberry.input
class PointInput:
    client_id: strawberry.ID
    ts: datetime.datetime
    lat: float
    lon: float
    acc: float | None = None
    speed: float | None = None
    heading: float | None = None
    alt: float | None = None


@strawberry.input
class VisitInput:
    client_id: strawberry.ID
    start: datetime.datetime
    end: datetime.datetime
    lat: float
    lon: float
    radius: float
    point_count: int
    place_client_id: strawberry.ID | None = None


@strawberry.input
class TripInput:
    client_id: strawberry.ID
    start: datetime.datetime
    end: datetime.datetime
    distance: float
    mode: TripMode
    from_visit: strawberry.ID | None = None
    to_visit: strawberry.ID | None = None


@strawberry.input
class PlaceInput:
    client_id: strawberry.ID
    name: str
    lat: float
    lon: float
    radius: float
    updated_at: datetime.datetime


@strawberry.input
class DeletedInput:
    client_id: strawberry.ID
    deleted_at: datetime.datetime


# --------------------------------------------------------------------------- conversion


def _place(row: models.Place | dict[str, Any]) -> Place:
    get = row.get if isinstance(row, dict) else lambda key: getattr(row, key)
    return Place(
        client_id=strawberry.ID(get("client_id")),
        name=get("name"),
        lat=get("lat"),
        lon=get("lon"),
        radius=get("radius"),
        updated_at=get("updated_at"),
        deleted_at=get("deleted_at"),
    )


def _point(row: dict[str, Any]) -> Point:
    return Point(**row)


def _visit(row: dict[str, Any]) -> Visit:
    return Visit(**row)


def _trip(row: dict[str, Any]) -> Trip:
    return Trip(**{**row, "mode": TripMode(row["mode"])})


# --------------------------------------------------------------------------- queries


async def sync_state(info: Info) -> SyncState:
    state = await sync_to_async(backup.sync_state)(caller(info))
    return SyncState(last_point_ts=state.last_point_ts, point_count=state.point_count, segments_from=state.segments_from)


async def changes(info: Info, cursor: str | None = None, limit: int | None = backup.MAX_BATCH) -> ChangeSet:
    page = await sync_to_async(backup.changes)(caller(info), cursor, backup.MAX_BATCH if limit is None else limit)
    return ChangeSet(
        points=[_point(r) for r in page.points],
        visits=[_visit(r) for r in page.visits],
        trips=[_trip(r) for r in page.trips],
        places=[_place(r) for r in page.places],
        deleted_places=[strawberry.ID(c) for c in page.deleted_places],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


async def access_log(info: Info, limit: int | None = 100, offset: int | None = 0) -> list[AccessLogEntry]:
    entries = await sync_to_async(backup.access_log)(caller(info), 100 if limit is None else limit, offset or 0)
    return [
        AccessLogEntry(id=strawberry.ID(str(e.pk)), operation=e.operation, range=e.range, rows=e.rows, at=e.at, device_id=e.device_id, client_id=e.client_id)
        for e in entries
    ]


async def retention(info: Info) -> Retention:
    return Retention(days=await sync_to_async(backup.retention)(caller(info)))


# --------------------------------------------------------------------------- mutations


async def upload_points(info: Info, points: list[PointInput]) -> UploadResult:
    rows = [backup.PointIn(client_id=str(p.client_id), ts=p.ts, lat=p.lat, lon=p.lon, acc=p.acc, speed=p.speed, heading=p.heading, alt=p.alt) for p in points]
    result = await sync_to_async(backup.upload_points)(caller(info), rows)
    return UploadResult(accepted=result.accepted, duplicates=result.duplicates)


async def replace_segments(
    info: Info,
    from_: Annotated[datetime.datetime, strawberry.argument(name="from")],
    visits: list[VisitInput],
    trips: list[TripInput],
) -> ReplaceResult:
    visit_rows = [
        backup.VisitIn(client_id=str(v.client_id), start=v.start, end=v.end, lat=v.lat, lon=v.lon, radius=v.radius, point_count=v.point_count, place_client_id=v.place_client_id)
        for v in visits
    ]
    trip_rows = [
        backup.TripIn(client_id=str(t.client_id), start=t.start, end=t.end, distance=t.distance, mode=t.mode.value, from_visit=t.from_visit, to_visit=t.to_visit)
        for t in trips
    ]
    result = await sync_to_async(backup.replace_segments)(caller(info), from_, visit_rows, trip_rows)
    return ReplaceResult(deleted_visits=result.deleted_visits, deleted_trips=result.deleted_trips, visits=result.visits, trips=result.trips)


async def sync_places(info: Info, places: list[PlaceInput], deleted: list[DeletedInput]) -> PlaceSyncResult:
    place_rows = [backup.PlaceIn(client_id=str(p.client_id), name=p.name, lat=p.lat, lon=p.lon, radius=p.radius, updated_at=p.updated_at) for p in places]
    deleted_rows = [backup.DeletedIn(client_id=str(d.client_id), deleted_at=d.deleted_at) for d in deleted]
    result = await sync_to_async(backup.sync_places)(caller(info), place_rows, deleted_rows)
    return PlaceSyncResult(applied=result.applied, stale=[_place(p) for p in result.stale])


async def delete_server_copy(info: Info, confirm: str) -> int:
    return await sync_to_async(backup.delete_server_copy)(caller(info), confirm)


async def set_retention(info: Info, days: int | None = None) -> Retention:
    return Retention(days=await sync_to_async(backup.set_retention)(caller(info), days))
