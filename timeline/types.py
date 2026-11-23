"""The object types: devices, points, visits, trips and places, as stored.

Every type is scoped to the caller in its ``get_queryset``, which strawberry_django runs for
list fields, single fetches and nested relations alike: a user only ever reads their own rows,
from any of their devices. Tombstoned places are sync bookkeeping and never listed.

A guest of the organization (``arkitekt_service.collaborations``) reads the visits, trips and
places someone put in their collaborations, and nothing else: not their own rows, not anyone's
points or devices. What a place counts of its visits is counted over the shared ones.
"""

import datetime
from typing import TypeVar

import kante
import strawberry
import strawberry_django
from arkitekt_service.collaborations.guests import guest_of, object_scope
from asgiref.sync import sync_to_async
from django.db.models import Count, Max, Model, QuerySet
from kante.types import Info

from timeline import enums, filters, loaders, models

_M = TypeVar("_M", bound=Model)


def held_to_guest(queryset: QuerySet[_M]) -> QuerySet[_M]:
    """For a guest: the rows in their collaborations, whoever's they are, wherever they are read
    (not only at a root field: what a nested field counts is counted over them)."""
    held: QuerySet[_M] = object_scope(queryset)
    return held


class UserScoped:
    """Mixin: only the calling user's rows (a guest: what is in their collaborations)."""

    @classmethod
    def get_queryset(
        cls, queryset: QuerySet[_M], info: Info, **kwargs: object
    ) -> QuerySet[_M]:
        if guest_of(info) is not None:
            return held_to_guest(queryset)
        return queryset.filter(user=info.context.request.user)


def _shared_visit_stats(user_id: int, client_id: str) -> "loaders.VisitStats":
    """The visits matched to a place that are in the guest's collaborations: how many, and the latest end."""
    found = held_to_guest(
        models.Visit.objects.filter(user_id=user_id, place_client_id=client_id)
    ).aggregate(n=Count("id"), last=Max("end"))
    return loaders.VisitStats(count=found["n"], last_end=found["last"])


async def _place_visit_stats(place: object, info: Info) -> "loaders.VisitStats":
    key = (place.user_id, place.client_id)  # type: ignore[attr-defined]
    if guest_of(info) is not None:
        return await sync_to_async(_shared_visit_stats)(*key)
    return await loaders.visit_stats(info).load(key)


@kante.django_type(
    models.Device,
    pagination=True,
    filters=filters.DeviceFilter,
    ordering=filters.DeviceOrder,
    description="One of your phones (an install): the token's client_device.",
)
class Device(UserScoped):
    id: strawberry.ID
    device_id: str = strawberry_django.field(
        description="The client_device claim it uploads with."
    )
    first_seen_at: datetime.datetime
    last_upload_at: datetime.datetime | None
    segments_from: datetime.datetime | None = strawberry_django.field(
        description="The `from` of its last replaceSegments."
    )

    @strawberry_django.field(description="How many points it has uploaded.")
    def point_count(self) -> int:
        return models.Point.objects.filter(device_id=self.pk).count()  # type: ignore[attr-defined]


@kante.django_type(
    models.Point,
    pagination=True,
    filters=filters.PointFilter,
    ordering=filters.PointOrder,
    description="One location fix.",
)
class Point(UserScoped):
    id: strawberry.ID
    client_id: strawberry.ID
    device: Device
    ts: datetime.datetime
    lat: float
    lon: float
    acc: float | None = strawberry_django.field(
        description="Horizontal accuracy, meters."
    )
    speed: float | None = strawberry_django.field(description="Meters per second.")
    heading: float | None = strawberry_django.field(description="Degrees from north.")
    alt: float | None = strawberry_django.field(description="Altitude, meters.")

    @strawberry_django.field(
        description="The client_device of the device that recorded it.",
        only=["device__device_id"],
    )
    def device_id(self) -> strawberry.ID:
        return strawberry.ID(self.device.device_id)  # type: ignore[attr-defined]


@kante.django_type(
    models.Place,
    pagination=True,
    filters=filters.PlaceFilter,
    ordering=filters.PlaceOrder,
    description="A named place, shared by all of your phones.",
)
class Place(UserScoped):
    id: strawberry.ID
    client_id: strawberry.ID
    name: str | None
    lat: float | None
    lon: float | None
    radius: float | None = strawberry_django.field(description="Meters.")
    updated_at: datetime.datetime
    deleted_at: datetime.datetime | None = strawberry_django.field(
        description="Set on a tombstone (only ever seen in syncPlaces' `stale`)."
    )

    @classmethod
    def get_queryset(
        cls, queryset: QuerySet[_M], info: Info, **kwargs: object
    ) -> QuerySet[_M]:
        return super().get_queryset(queryset, info, **kwargs).filter(deleted_at=None)

    @strawberry_django.field(
        description="How many visits were matched to it.", only=["user_id", "client_id"]
    )
    async def visit_count(self, info: Info) -> int:
        return (await _place_visit_stats(self, info)).count

    @strawberry_django.field(
        description="When the latest visit to it ended.", only=["user_id", "client_id"]
    )
    async def last_visit_at(self, info: Info) -> datetime.datetime | None:
        return (await _place_visit_stats(self, info)).last_end


@kante.django_type(
    models.Visit,
    pagination=True,
    filters=filters.VisitFilter,
    ordering=filters.VisitOrder,
    description="A stay at one spot.",
)
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
    place_client_id: strawberry.ID | None = strawberry_django.field(
        description="The clientId of the place it was matched to."
    )

    @strawberry_django.field(
        description="The client_device of the device that recorded it.",
        only=["device__device_id"],
    )
    def device_id(self) -> strawberry.ID:
        return strawberry.ID(self.device.device_id)  # type: ignore[attr-defined]

    @strawberry_django.field(description="How long it lasted, seconds.")
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()  # type: ignore[attr-defined]

    @strawberry_django.field(
        description="The place it was matched to (none if deleted or unmatched).",
        only=["user_id", "place_client_id"],
    )
    async def place(self, info: Info) -> Place | None:
        if not self.place_client_id:  # type: ignore[attr-defined]
            return None
        return await loaders.places(info).load((self.user_id, self.place_client_id))  # type: ignore[attr-defined,return-value]


@kante.django_type(
    models.Trip,
    pagination=True,
    filters=filters.TripFilter,
    ordering=filters.TripOrder,
    description="A movement between two visits.",
)
class Trip(UserScoped):
    id: strawberry.ID
    client_id: strawberry.ID
    device: Device
    start: datetime.datetime
    end: datetime.datetime
    from_visit: strawberry.ID | None = strawberry_django.field(
        description="The clientId of the visit it left."
    )
    to_visit: strawberry.ID | None = strawberry_django.field(
        description="The clientId of the visit it arrived at."
    )
    distance: float = strawberry_django.field(description="Meters.")

    @strawberry_django.field(
        description="The client_device of the device that recorded it.",
        only=["device__device_id"],
    )
    def device_id(self) -> strawberry.ID:
        return strawberry.ID(self.device.device_id)  # type: ignore[attr-defined]

    @strawberry_django.field(description="How it was travelled.")
    def mode(self) -> enums.TripMode:
        return enums.TripMode(self.mode)  # type: ignore[attr-defined]

    @strawberry_django.field(description="How long it took, seconds.")
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()  # type: ignore[attr-defined]
