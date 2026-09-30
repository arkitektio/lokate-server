"""The object types: devices, points, visits, trips and places, as stored.

Every type is scoped to the caller in its ``get_queryset``, which strawberry_django runs for
list fields, single fetches and nested relations alike: a user only ever reads their own rows,
from any of their devices. Tombstoned places are sync bookkeeping and never listed.
"""

import datetime

import kante
import strawberry
import strawberry_django
from kante.types import Info

from timeline import enums, filters, models


class UserScoped:
    """Mixin: only the calling user's rows."""

    @classmethod
    def get_queryset(cls, queryset, info: Info, **kwargs):  # noqa: ANN001, ANN206
        return queryset.filter(user=info.context.request.user)


@kante.django_type(models.Device, pagination=True, filters=filters.DeviceFilter, ordering=filters.DeviceOrder, description="One of your phones (an install): the token's client_device.")
class Device(UserScoped):
    id: strawberry.ID
    device_id: str = strawberry_django.field(description="The client_device claim it uploads with.")
    first_seen_at: datetime.datetime
    last_upload_at: datetime.datetime | None
    segments_from: datetime.datetime | None = strawberry_django.field(description="The `from` of its last replaceSegments.")

    @strawberry_django.field(description="How many points it has uploaded.")
    def point_count(self) -> int:
        return models.Point.objects.filter(device_id=self.pk).count()  # type: ignore[attr-defined]


@kante.django_type(models.Point, pagination=True, filters=filters.PointFilter, ordering=filters.PointOrder, description="One location fix.")
class Point(UserScoped):
    id: strawberry.ID
    client_id: strawberry.ID
    device: Device
    ts: datetime.datetime
    lat: float
    lon: float
    acc: float | None = strawberry_django.field(description="Horizontal accuracy, meters.")
    speed: float | None = strawberry_django.field(description="Meters per second.")
    heading: float | None = strawberry_django.field(description="Degrees from north.")
    alt: float | None = strawberry_django.field(description="Altitude, meters.")

    @strawberry_django.field(description="The client_device of the device that recorded it.", only=["device__device_id"])
    def device_id(self) -> strawberry.ID:
        return strawberry.ID(self.device.device_id)  # type: ignore[attr-defined]


@kante.django_type(models.Place, pagination=True, filters=filters.PlaceFilter, ordering=filters.PlaceOrder, description="A named place, shared by all of your phones.")
class Place(UserScoped):
    id: strawberry.ID
    client_id: strawberry.ID
    name: str | None
    lat: float | None
    lon: float | None
    radius: float | None = strawberry_django.field(description="Meters.")
    updated_at: datetime.datetime
    deleted_at: datetime.datetime | None = strawberry_django.field(description="Set on a tombstone (only ever seen in syncPlaces' `stale`).")

    @classmethod
    def get_queryset(cls, queryset, info: Info, **kwargs):  # noqa: ANN001, ANN206
        return super().get_queryset(queryset, info, **kwargs).filter(deleted_at=None)

    @strawberry_django.field(description="How many visits were matched to it.")
    def visit_count(self) -> int:
        return models.Visit.objects.filter(user_id=self.user_id, place_client_id=self.client_id).count()  # type: ignore[attr-defined]

    @strawberry_django.field(description="When the latest visit to it ended.")
    def last_visit_at(self) -> datetime.datetime | None:
        visit = models.Visit.objects.filter(user_id=self.user_id, place_client_id=self.client_id).order_by("-end").first()  # type: ignore[attr-defined]
        return visit.end if visit else None


@kante.django_type(models.Visit, pagination=True, filters=filters.VisitFilter, ordering=filters.VisitOrder, description="A stay at one spot.")
class Visit(UserScoped):
    id: strawberry.ID
    client_id: strawberry.ID
    device: Device
    start: datetime.datetime
    end: datetime.datetime
    lat: float
    lon: float
    radius: float = strawberry_django.field(description="Meters.")
    point_count: int
    place_client_id: strawberry.ID | None = strawberry_django.field(description="The clientId of the place it was matched to.")

    @strawberry_django.field(description="The client_device of the device that recorded it.", only=["device__device_id"])
    def device_id(self) -> strawberry.ID:
        return strawberry.ID(self.device.device_id)  # type: ignore[attr-defined]

    @strawberry_django.field(description="How long it lasted, seconds.")
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()  # type: ignore[attr-defined]

    @strawberry_django.field(description="The place it was matched to (none if deleted or unmatched).")
    def place(self) -> Place | None:
        if not self.place_client_id:  # type: ignore[attr-defined]
            return None
        return models.Place.objects.filter(user_id=self.user_id, client_id=self.place_client_id, deleted_at=None).first()  # type: ignore[attr-defined,return-value]


@kante.django_type(models.Trip, pagination=True, filters=filters.TripFilter, ordering=filters.TripOrder, description="A movement between two visits.")
class Trip(UserScoped):
    id: strawberry.ID
    client_id: strawberry.ID
    device: Device
    start: datetime.datetime
    end: datetime.datetime
    from_visit: strawberry.ID | None = strawberry_django.field(description="The clientId of the visit it left.")
    to_visit: strawberry.ID | None = strawberry_django.field(description="The clientId of the visit it arrived at.")
    distance: float = strawberry_django.field(description="Meters.")

    @strawberry_django.field(description="The client_device of the device that recorded it.", only=["device__device_id"])
    def device_id(self) -> strawberry.ID:
        return strawberry.ID(self.device.device_id)  # type: ignore[attr-defined]

    @strawberry_django.field(description="How it was travelled.")
    def mode(self) -> enums.TripMode:
        return enums.TripMode(self.mode)  # type: ignore[attr-defined]

    @strawberry_django.field(description="How long it took, seconds.")
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()  # type: ignore[attr-defined]
