"""Single fetches and counts over the caller's rows (see types.py for the scoping)."""

import strawberry
import strawberry_django
from kante.types import Info

from timeline import filters, models, types
from timeline.identity import caller


def _get(model, type_, info: Info, id: strawberry.ID):  # noqa: ANN001, ANN202
    return type_.get_queryset(model.objects.all(), info).get(pk=id)


def _count(model, type_, info: Info, where) -> int:  # noqa: ANN001
    rows = type_.get_queryset(model.objects.all(), info)
    if where is not None:
        rows = strawberry_django.filters.apply(where, rows, info)
    return rows.count()


def device(info: Info, id: strawberry.ID) -> types.Device:
    caller(info)
    return _get(models.Device, types.Device, info, id)


def point(info: Info, id: strawberry.ID) -> types.Point:
    caller(info)
    return _get(models.Point, types.Point, info, id)


def visit(info: Info, id: strawberry.ID) -> types.Visit:
    caller(info)
    return _get(models.Visit, types.Visit, info, id)


def trip(info: Info, id: strawberry.ID) -> types.Trip:
    caller(info)
    return _get(models.Trip, types.Trip, info, id)


def place(info: Info, id: strawberry.ID) -> types.Place:
    caller(info)
    return _get(models.Place, types.Place, info, id)


def points_count(info: Info, filters: filters.PointFilter | None = None) -> int:
    caller(info)
    return _count(models.Point, types.Point, info, filters)


def visits_count(info: Info, filters: filters.VisitFilter | None = None) -> int:
    caller(info)
    return _count(models.Visit, types.Visit, info, filters)


def trips_count(info: Info, filters: filters.TripFilter | None = None) -> int:
    caller(info)
    return _count(models.Trip, types.Trip, info, filters)
