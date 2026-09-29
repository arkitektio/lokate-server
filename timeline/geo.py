"""PostGIS without GeoDjango, as bank does it (``finance/geo.py``).

GeoDjango needs GDAL and GEOS in every process that imports it. lokate only stores points,
so ``geom`` is a *generated* ``geometry(Point,4326)`` column that Postgres computes from the
plain ``lat``/``lon`` columns the API reads and writes, GiST-indexed. Python never parses
geometry.
"""

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
