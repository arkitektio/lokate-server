"""PostGIS without GeoDjango, as bank does it (``finance/geo.py``).

GeoDjango needs GDAL and GEOS in every process that imports it. lokate only stores points,
so ``geom`` is a *generated* ``geometry(Point,4326)`` column that Postgres computes from the
plain ``lat``/``lon`` columns the API reads and writes, GiST-indexed. Python never parses
geometry.
"""

import math
from typing import Any

from django.db import models
from django.db.models import Func


class GeometryPointField(models.Field):
    """A ``geometry(Point,4326)`` column. Values stay opaque (hex WKB); read lat/lon instead."""

    description = "A WGS84 point (PostGIS geometry)"

    def db_type(self, connection: Any) -> str:
        return "geometry(Point,4326)"

    def rel_db_type(self, connection: Any) -> str:
        return self.db_type(connection)


class MakePoint(Func):
    """``ST_MakePoint(lon, lat)`` in WGS84, NULL if either is NULL. Immutable, so usable in a generated column."""

    template = "ST_SetSRID(ST_MakePoint(%(expressions)s), 4326)"
    arity = 2
    output_field = GeometryPointField()


@GeometryPointField.register_lookup
class InBox(models.Lookup):
    """``geom__in_box=(south, west, north, east)``: inside a lat/lon rectangle (a map viewport).

    ``&&`` against an envelope, so the GiST index answers it.
    """

    lookup_name = "in_box"
    prepare_rhs = False

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, list]:
        lhs, params = self.process_lhs(compiler, connection)
        south, west, north, east = (float(v) for v in self.rhs)
        return f"{lhs} && ST_MakeEnvelope(%s, %s, %s, %s, 4326)", [*params, west, south, east, north]


@GeometryPointField.register_lookup
class Near(models.Lookup):
    """``geom__near=(lat, lon, meters)``: within ``meters`` of a point, measured on the spheroid.

    An envelope test first (index-backed, generous at any latitude), then the exact geodesic
    distance on what is left.
    """

    lookup_name = "near"
    prepare_rhs = False

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, list]:
        lhs, params = self.process_lhs(compiler, connection)
        lat, lon, meters = (float(v) for v in self.rhs)
        degrees = meters / (111_320 * max(math.cos(math.radians(lat)), 0.01))
        here = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
        sql = f"({lhs} && ST_Expand({here}, %s) AND ST_DWithin({lhs}::geography, {here}::geography, %s))"
        return sql, [*params, lon, lat, degrees, *params, lon, lat, meters]
