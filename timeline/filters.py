"""Filtering and ordering for the list fields.

``strawberry_django`` turns these into GraphQL inputs the paginated list fields accept, e.g.
``points(filters: {since: "…", until: "…", inBox: {south: 48, west: 16, north: 49, east: 17}}, ordering: [{ts: ASC}])``.
Scoping to the caller is not a filter: every type's ``get_queryset`` does it (see types.py).
"""

import datetime

import strawberry
import strawberry_django
from django.db.models import DateTimeField, ExpressionWrapper, F, Q
from strawberry import auto

from timeline import enums, models


@strawberry.input(description="A lat/lon rectangle, e.g. a map viewport.")
class BoxInput:
    south: float
    west: float
    north: float
    east: float


@strawberry.input(description="Within `radius` meters of a point.")
class NearInput:
    lat: float
    lon: float
    radius: float


def _ids(prefix: str, field: str, value: list[strawberry.ID]) -> Q:
    return Q(**{f"{prefix}{field}__in": value})


def _in_box(prefix: str, value: BoxInput) -> Q:
    return Q(**{f"{prefix}geom__in_box": (value.south, value.west, value.north, value.east)})


def _near(prefix: str, value: NearInput) -> Q:
    return Q(**{f"{prefix}geom__near": (value.lat, value.lon, value.radius)})


@strawberry_django.filter_type(models.Device)
class DeviceFilter:
    """Filtering options for devices."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these devices."""
        return _ids(prefix, "id", value)


@strawberry_django.filter_type(models.Point)
class PointFilter:
    """Filtering options for points."""

    @strawberry_django.filter_field
    def devices(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only points recorded by these devices."""
        return _ids(prefix, "device_id", value)

    @strawberry_django.filter_field
    def since(self, value: datetime.datetime, prefix: str) -> Q:
        """Recorded at or after this time."""
        return Q(**{f"{prefix}ts__gte": value})

    @strawberry_django.filter_field
    def until(self, value: datetime.datetime, prefix: str) -> Q:
        """Recorded before this time."""
        return Q(**{f"{prefix}ts__lt": value})

    @strawberry_django.filter_field
    def in_box(self, value: BoxInput, prefix: str) -> Q:
        """Inside this rectangle."""
        return _in_box(prefix, value)

    @strawberry_django.filter_field
    def near(self, value: NearInput, prefix: str) -> Q:
        """Within `radius` meters of a point."""
        return _near(prefix, value)

    @strawberry_django.filter_field
    def max_accuracy(self, value: float, prefix: str) -> Q:
        """Only fixes at least this accurate (acc ≤ value, meters)."""
        return Q(**{f"{prefix}acc__lte": value})


def _overlaps(prefix: str, since: datetime.datetime | None = None, until: datetime.datetime | None = None) -> Q:
    q = Q()
    if since is not None:
        q &= Q(**{f"{prefix}end__gte": since})
    if until is not None:
        q &= Q(**{f"{prefix}start__lt": until})
    return q


@strawberry_django.filter_type(models.Visit)
class VisitFilter:
    """Filtering options for visits."""

    @strawberry_django.filter_field
    def devices(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only visits segmented by these devices."""
        return _ids(prefix, "device_id", value)

    @strawberry_django.filter_field
    def since(self, value: datetime.datetime, prefix: str) -> Q:
        """Still going on at or after this time (ends at or after it)."""
        return _overlaps(prefix, since=value)

    @strawberry_django.filter_field
    def until(self, value: datetime.datetime, prefix: str) -> Q:
        """Started before this time."""
        return _overlaps(prefix, until=value)

    @strawberry_django.filter_field
    def places(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only visits matched to these places (by their clientId)."""
        return Q(**{f"{prefix}place_client_id__in": value})

    @strawberry_django.filter_field
    def has_place(self, value: bool, prefix: str) -> Q:
        """Only visits matched to a place (true), or only unmatched ones (false)."""
        return Q(**{f"{prefix}place_client_id__isnull": not value})

    @strawberry_django.filter_field
    def min_duration(self, value: float, prefix: str) -> Q:
        """Only visits at least this long (seconds)."""
        later = ExpressionWrapper(F(f"{prefix}start") + datetime.timedelta(seconds=value), output_field=DateTimeField())
        return Q(**{f"{prefix}end__gte": later})

    @strawberry_django.filter_field
    def in_box(self, value: BoxInput, prefix: str) -> Q:
        """Inside this rectangle."""
        return _in_box(prefix, value)

    @strawberry_django.filter_field
    def near(self, value: NearInput, prefix: str) -> Q:
        """Within `radius` meters of a point."""
        return _near(prefix, value)


@strawberry_django.filter_type(models.Trip)
class TripFilter:
    """Filtering options for trips."""

    @strawberry_django.filter_field
    def devices(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only trips segmented by these devices."""
        return _ids(prefix, "device_id", value)

    @strawberry_django.filter_field
    def since(self, value: datetime.datetime, prefix: str) -> Q:
        """Still going on at or after this time."""
        return _overlaps(prefix, since=value)

    @strawberry_django.filter_field
    def until(self, value: datetime.datetime, prefix: str) -> Q:
        """Started before this time."""
        return _overlaps(prefix, until=value)

    @strawberry_django.filter_field
    def modes(self, value: list[enums.TripMode], prefix: str) -> Q:
        """Only trips travelled these ways."""
        return Q(**{f"{prefix}mode__in": [m.value for m in value]})

    @strawberry_django.filter_field
    def min_distance(self, value: float, prefix: str) -> Q:
        """Only trips at least this long (meters)."""
        return Q(**{f"{prefix}distance__gte": value})


@strawberry_django.filter_type(models.Place)
class PlaceFilter:
    """Filtering options for places."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these places."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def search(self, value: str, prefix: str) -> Q:
        """Name contains this (case-insensitive)."""
        return Q(**{f"{prefix}name__icontains": value})

    @strawberry_django.filter_field
    def in_box(self, value: BoxInput, prefix: str) -> Q:
        """Inside this rectangle."""
        return _in_box(prefix, value)

    @strawberry_django.filter_field
    def near(self, value: NearInput, prefix: str) -> Q:
        """Within `radius` meters of a point."""
        return _near(prefix, value)


@strawberry_django.order_type(models.Device)
class DeviceOrder:
    """Ordering options for devices."""

    first_seen_at: auto
    last_upload_at: auto


@strawberry_django.order_type(models.Point)
class PointOrder:
    """Ordering options for points."""

    ts: auto


@strawberry_django.order_type(models.Visit)
class VisitOrder:
    """Ordering options for visits."""

    start: auto
    end: auto


@strawberry_django.order_type(models.Trip)
class TripOrder:
    """Ordering options for trips."""

    start: auto
    distance: auto


@strawberry_django.order_type(models.Place)
class PlaceOrder:
    """Ordering options for places."""

    name: auto
    updated_at: auto
