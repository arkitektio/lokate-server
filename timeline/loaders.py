"""Batch loaders: what a list asks once per row, asked once per answer.

A list of places wants each place's visit count and latest visit; a list of visits wants each
visit's place. Asked row by row that is a query per row (a 500-place list made a thousand).
These loaders collect the rows of one answer and ask once. They are kept on the request's
context and never cache: they batch, and every answer reads the database as it is.
"""

import datetime
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.db.models import Count, Max
from kante.types import Info
from strawberry.dataloader import DataLoader

from timeline import models

PlaceKey = tuple[int, str]
"""A place by its owner and its client id: ``(user id, place client id)``."""


@dataclass(frozen=True)
class VisitStats:
    """The visits matched to one place: how many, and when the latest ended."""

    count: int
    last_end: datetime.datetime | None


NO_VISITS = VisitStats(count=0, last_end=None)


def _by_user(keys: list[PlaceKey]) -> dict[int, set[str]]:
    grouped: dict[int, set[str]] = {}
    for user_id, client_id in keys:
        grouped.setdefault(user_id, set()).add(client_id)
    return grouped


def visit_stats_of(keys: list[PlaceKey]) -> list[VisitStats]:
    """The visit count and latest visit end of each place, one grouped query per user."""
    found: dict[PlaceKey, VisitStats] = {}
    for user_id, client_ids in _by_user(keys).items():
        rows = (
            models.Visit.objects.filter(user_id=user_id, place_client_id__in=client_ids)
            .values("place_client_id")
            .annotate(n=Count("id"), last=Max("end"))
            .order_by()
        )
        for row in rows:
            found[(user_id, row["place_client_id"])] = VisitStats(
                count=row["n"], last_end=row["last"]
            )
    return [found.get(key, NO_VISITS) for key in keys]


def places_of(keys: list[PlaceKey]) -> list[models.Place | None]:
    """The live (not tombstoned) place of each key, or None, one query per user."""
    found: dict[PlaceKey, models.Place] = {}
    for user_id, client_ids in _by_user(keys).items():
        for place in models.Place.objects.filter(
            user_id=user_id, client_id__in=client_ids, deleted_at=None
        ):
            found[(user_id, place.client_id)] = place
    return [found.get(key) for key in keys]


async def _load_visit_stats(keys: list[PlaceKey]) -> list[VisitStats]:
    return await sync_to_async(visit_stats_of)(keys)


async def _load_places(keys: list[PlaceKey]) -> list[models.Place | None]:
    return await sync_to_async(places_of)(keys)


def visit_stats(info: Info) -> DataLoader[PlaceKey, VisitStats]:
    """This request's loader of per-place visit stats."""
    loader: DataLoader[PlaceKey, VisitStats] | None = getattr(
        info.context, "_lokate_visit_stats", None
    )
    if loader is None:
        loader = DataLoader(load_fn=_load_visit_stats, cache=False)
        info.context._lokate_visit_stats = loader
    return loader


def places(info: Info) -> DataLoader[PlaceKey, models.Place | None]:
    """This request's loader of places by ``(user, client id)``."""
    loader: DataLoader[PlaceKey, models.Place | None] | None = getattr(
        info.context, "_lokate_places", None
    )
    if loader is None:
        loader = DataLoader(load_fn=_load_places, cache=False)
        info.context._lokate_places = loader
    return loader
