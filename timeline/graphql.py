"""The lokate GraphQL API beyond the plain object fields: sync I/O and aggregate reads.

The object types (Device, Point, Visit, Trip, Place) live in :mod:`timeline.types` and are
shared by both. Resolvers only convert: the protocol lives in :mod:`timeline.backup` and the
aggregates in :mod:`timeline.reads`, which run synchronously in a thread (``sync_to_async``).
"""

import datetime
from typing import Annotated

import strawberry
from asgiref.sync import sync_to_async
from kante.types import Info

from timeline import backup, enums, reads, types
from timeline.identity import caller

# --------------------------------------------------------------------------- sync types


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
    stale: list[types.Place] = strawberry.field(description="Places whose server copy is newer; the phone takes them (deletedAt set: delete it).")


@strawberry.type(description="One page of everything the user has, from all their devices, in change order.")
class ChangeSet:
    points: list[types.Point]
    visits: list[types.Visit]
    trips: list[types.Trip]
    places: list[types.Place]
    deleted_places: list[strawberry.ID]
    next_cursor: str | None
    has_more: bool


# --------------------------------------------------------------------------- read types


@strawberry.type(description="One calendar day of the timeline, across all your devices.")
class Day:
    date: datetime.date
    start: datetime.datetime = strawberry.field(description="Midnight, in the requested time zone.")
    end: datetime.datetime
    visits: list[types.Visit] = strawberry.field(description="Visits overlapping the day, oldest first.")
    trips: list[types.Trip] = strawberry.field(description="Trips overlapping the day, oldest first.")
    point_count: int
    distance: float = strawberry.field(description="Meters travelled, summed over the day's trips.")


@strawberry.type(description="One device's points over a time range, joined into a line.")
class Track:
    device: types.Device
    start: datetime.datetime
    end: datetime.datetime
    point_count: int
    distance: float = strawberry.field(description="Geodesic length of the (unsimplified) line, meters.")
    geojson: str = strawberry.field(description="A GeoJSON LineString (coordinates are [lon, lat]).")


@strawberry.type(description="Trips of one mode within a stats bucket.")
class ModeStat:
    mode: enums.TripMode
    trips: int
    distance: float = strawberry.field(description="Meters.")
    seconds: float


@strawberry.type(description="Totals for one day, week or month.")
class StatsBucket:
    start: datetime.datetime
    point_count: int
    visit_count: int
    trip_count: int
    distance: float = strawberry.field(description="Meters, over all trips.")
    by_mode: list[ModeStat]


@strawberry.type(description="Time spent at one place.")
class PlaceStat:
    place_client_id: strawberry.ID
    place: types.Place | None = strawberry.field(description="None if the place was deleted.")
    visit_count: int
    seconds: float
    first_visit_at: datetime.datetime
    last_visit_at: datetime.datetime


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
    mode: enums.TripMode
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


# --------------------------------------------------------------------------- sync queries


async def sync_state(info: Info) -> SyncState:
    state = await sync_to_async(backup.sync_state)(caller(info))
    return SyncState(last_point_ts=state.last_point_ts, point_count=state.point_count, segments_from=state.segments_from)


async def changes(info: Info, cursor: str | None = None, limit: int | None = backup.MAX_BATCH) -> ChangeSet:
    page = await sync_to_async(backup.changes)(caller(info), cursor, backup.MAX_BATCH if limit is None else limit)
    return ChangeSet(
        points=page.points,  # type: ignore[arg-type]
        visits=page.visits,  # type: ignore[arg-type]
        trips=page.trips,  # type: ignore[arg-type]
        places=page.places,  # type: ignore[arg-type]
        deleted_places=[strawberry.ID(c) for c in page.deleted_places],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


# --------------------------------------------------------------------------- reads


async def day(info: Info, date: datetime.date, timezone: str | None = "UTC") -> Day:
    d = await sync_to_async(reads.day)(caller(info).user, date, timezone or "UTC")
    return Day(date=d.date, start=d.start, end=d.end, visits=d.visits, trips=d.trips, point_count=d.point_count, distance=d.distance)  # type: ignore[arg-type]


async def route(
    info: Info,
    since: datetime.datetime,
    until: datetime.datetime,
    devices: list[strawberry.ID] | None = None,
    simplify: Annotated[float | None, strawberry.argument(description="Tolerance in meters for thinning the line (none: every point).")] = None,
    max_accuracy: Annotated[float | None, strawberry.argument(description="Leave out fixes less accurate than this (meters).")] = None,
) -> list[Track]:
    tracks = await sync_to_async(reads.route)(caller(info).user, since, until, [str(d) for d in devices] if devices else None, simplify, max_accuracy)
    return [Track(device=t.device, start=t.start, end=t.end, point_count=t.point_count, distance=t.distance, geojson=t.geojson) for t in tracks]  # type: ignore[arg-type]


async def stats(
    info: Info,
    since: datetime.datetime,
    until: datetime.datetime,
    granularity: enums.Granularity | None = enums.Granularity.DAY,
    timezone: str | None = "UTC",
) -> list[StatsBucket]:
    buckets = await sync_to_async(reads.stats)(caller(info).user, since, until, (granularity or enums.Granularity.DAY).value, timezone or "UTC")
    return [
        StatsBucket(
            start=b.start,
            point_count=b.point_count,
            visit_count=b.visit_count,
            trip_count=b.trip_count,
            distance=b.distance,
            by_mode=[ModeStat(mode=enums.TripMode(m.mode), trips=m.trips, distance=m.distance, seconds=m.seconds) for m in b.by_mode.values()],
        )
        for b in buckets
    ]


async def place_stats(info: Info, since: datetime.datetime | None = None, until: datetime.datetime | None = None, limit: int | None = 100) -> list[PlaceStat]:
    rows = await sync_to_async(reads.place_stats)(caller(info).user, since, until, limit or 100)
    return [
        PlaceStat(place_client_id=strawberry.ID(r.place_client_id), place=r.place, visit_count=r.visit_count, seconds=r.seconds, first_visit_at=r.first_visit_at, last_visit_at=r.last_visit_at)  # type: ignore[arg-type]
        for r in rows
    ]


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
    return PlaceSyncResult(applied=result.applied, stale=result.stale)  # type: ignore[arg-type]


async def delete_server_copy(info: Info, confirm: str) -> int:
    return await sync_to_async(backup.delete_server_copy)(caller(info), confirm)
