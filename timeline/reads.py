"""The aggregate reads: a day, a route, stats. Synchronous, over the caller's rows only.

Everything spans all of the user's devices unless a device is named, since the timeline is the
person's, not the phone's.
"""

import datetime
import zoneinfo
from dataclasses import dataclass, field

from authentikate.models import User
from django.db import connection
from django.db.models import Q

from timeline import models
from timeline.backup import invalid

MAX_ROUTE_DAYS = 366


def zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise invalid(f"{name!r} is not an IANA time zone (e.g. Europe/Vienna).", "INVALID_TIMEZONE") from None


def _span(since: datetime.datetime, until: datetime.datetime) -> None:
    if until <= since:
        raise invalid("`until` must be after `since`.")


@dataclass
class Day:
    date: datetime.date
    start: datetime.datetime
    end: datetime.datetime
    visits: list[models.Visit]
    trips: list[models.Trip]
    point_count: int
    distance: float


def day(user: User, date: datetime.date, timezone: str = "UTC") -> Day:
    """The visits and trips overlapping ``date`` (in ``timezone``), oldest first, and the day's totals."""
    tz = zone(timezone)
    start = datetime.datetime.combine(date, datetime.time(), tzinfo=tz)
    end = datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(), tzinfo=tz)
    overlap = Q(user=user, end__gte=start, start__lt=end)
    visits = list(models.Visit.objects.filter(overlap).select_related("device").order_by("start"))
    trips = list(models.Trip.objects.filter(overlap).select_related("device").order_by("start"))
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM timeline_point WHERE user_id = %s AND ts >= %s AND ts < %s", [user.pk, start, end])
        (points,) = cursor.fetchone()
    return Day(date=date, start=start, end=end, visits=visits, trips=trips, point_count=points, distance=sum(t.distance for t in trips))


@dataclass
class Track:
    device: models.Device
    start: datetime.datetime
    end: datetime.datetime
    point_count: int
    distance: float
    geojson: str


def route(user: User, since: datetime.datetime, until: datetime.datetime, devices: list[str] | None = None, simplify: float | None = None, max_accuracy: float | None = None) -> list[Track]:
    """One track per device: its points in ``[since, until)`` joined in time order, as GeoJSON.

    ``simplify`` (meters) thins the line with Douglas-Peucker for drawing; ``distance`` is the
    geodesic length of the unsimplified line.
    """
    _span(since, until)
    if until - since > datetime.timedelta(days=MAX_ROUTE_DAYS):
        raise invalid(f"A route spans at most {MAX_ROUTE_DAYS} days; use stats for longer ranges.")
    tolerance = (simplify or 0) / 111_320  # meters to degrees, near enough for drawing
    where, params = ["user_id = %s", "ts >= %s", "ts < %s"], [user.pk, since, until]
    if devices:
        where.append("device_id = ANY(%s::bigint[])")
        params.append([int(d) for d in devices])
    if max_accuracy is not None:
        where.append("(acc IS NULL OR acc <= %s)")
        params.append(max_accuracy)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT device_id, a, b, n, ST_Length(line::geography),
                   ST_AsGeoJSON(CASE WHEN %s > 0 THEN ST_Simplify(line, %s) ELSE line END, 6)
            FROM (
                SELECT device_id, min(ts) AS a, max(ts) AS b, count(*) AS n, ST_MakeLine(geom ORDER BY ts) AS line
                FROM timeline_point WHERE {' AND '.join(where)} GROUP BY device_id
            ) AS tracks ORDER BY a
            """,
            [tolerance, tolerance, *params],
        )
        rows = cursor.fetchall()
    found = {d.pk: d for d in models.Device.objects.filter(user=user, pk__in=[r[0] for r in rows])}
    return [Track(device=found[r[0]], start=r[1], end=r[2], point_count=r[3], distance=r[4] or 0.0, geojson=r[5]) for r in rows]


@dataclass
class ModeStat:
    mode: str
    trips: int = 0
    distance: float = 0.0
    seconds: float = 0.0


@dataclass
class Bucket:
    start: datetime.datetime
    point_count: int = 0
    visit_count: int = 0
    trip_count: int = 0
    distance: float = 0.0
    by_mode: dict[str, ModeStat] = field(default_factory=dict)


def stats(user: User, since: datetime.datetime, until: datetime.datetime, granularity: str = "day", timezone: str = "UTC") -> list[Bucket]:
    """Counts and distances per day, week or month (in ``timezone``); empty buckets left out.

    Visits and trips count in the bucket they start in.
    """
    _span(since, until)
    zone(timezone)
    if granularity not in ("day", "week", "month"):
        raise invalid("granularity must be day, week or month.")
    buckets: dict[datetime.datetime, Bucket] = {}

    def bucket(start: datetime.datetime) -> Bucket:
        return buckets.setdefault(start, Bucket(start=start))

    trunc = "date_trunc(%s, {col} AT TIME ZONE %s) AT TIME ZONE %s"
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {trunc.format(col='ts')} AS b, count(*) FROM timeline_point WHERE user_id = %s AND ts >= %s AND ts < %s GROUP BY b",
            [granularity, timezone, timezone, user.pk, since, until],
        )
        for start, n in cursor.fetchall():
            bucket(start).point_count = n
        cursor.execute(
            f"SELECT {trunc.format(col='start')} AS b, count(*) FROM timeline_visit WHERE user_id = %s AND start >= %s AND start < %s GROUP BY b",
            [granularity, timezone, timezone, user.pk, since, until],
        )
        for start, n in cursor.fetchall():
            bucket(start).visit_count = n
        cursor.execute(
            f"""SELECT {trunc.format(col='start')} AS b, mode, count(*), sum(distance), sum(extract(epoch FROM "end" - start))
            FROM timeline_trip WHERE user_id = %s AND start >= %s AND start < %s GROUP BY b, mode""",
            [granularity, timezone, timezone, user.pk, since, until],
        )
        for start, mode, n, distance, seconds in cursor.fetchall():
            b = bucket(start)
            b.trip_count += n
            b.distance += distance or 0.0
            b.by_mode[mode] = ModeStat(mode=mode, trips=n, distance=distance or 0.0, seconds=float(seconds or 0))
    return [buckets[k] for k in sorted(buckets)]


@dataclass
class PlaceStat:
    place_client_id: str
    place: models.Place | None
    visit_count: int
    seconds: float
    first_visit_at: datetime.datetime
    last_visit_at: datetime.datetime


def place_stats(user: User, since: datetime.datetime | None = None, until: datetime.datetime | None = None, limit: int = 100) -> list[PlaceStat]:
    """Time spent per place (by the visits matched to it), most time first."""
    q = Q(user=user, place_client_id__isnull=False)
    if since is not None:
        q &= Q(end__gte=since)
    if until is not None:
        q &= Q(start__lt=until)
    if since is not None and until is not None:
        _span(since, until)
    rows: dict[str, dict] = {}
    for v in models.Visit.objects.filter(q).only("place_client_id", "start", "end"):
        row = rows.setdefault(v.place_client_id, {"n": 0, "s": 0.0, "first": v.start, "last": v.end})
        row["n"] += 1
        row["s"] += (v.end - v.start).total_seconds()
        row["first"] = min(row["first"], v.start)
        row["last"] = max(row["last"], v.end)
    places = {p.client_id: p for p in models.Place.objects.filter(user=user, client_id__in=list(rows), deleted_at=None)}
    ranked = sorted(rows.items(), key=lambda kv: kv[1]["s"], reverse=True)[:limit]
    return [PlaceStat(place_client_id=k, place=places.get(k), visit_count=r["n"], seconds=r["s"], first_visit_at=r["first"], last_visit_at=r["last"]) for k, r in ranked]

