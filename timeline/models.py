"""The server's copy of a phone's timeline.

The phone records and segments on its own; lokate only holds a copy it can restore from.
Everything is keyed by the *user* and the *device* of the token that wrote it (see
:mod:`timeline.identity`): there is no organization scoping and no cross-user API.

``received_at`` is not a timestamp but a stamp from one Postgres sequence
(``timeline_change_seq``), taken on every insert and on every update that changes a row.
``changes`` pages through it. All writes of one user hold that user's advisory lock (see
:func:`timeline.backup.user_lock`) from before they draw a stamp until they commit, so a row
that commits later always carries a higher stamp than every row a reader can already see:
the cursor never skips a row.

``Point`` is partitioned by month of ``ts``, which Django cannot express; its table is
created by raw SQL in the migration and the model here only describes it. Postgres requires
every unique key of a partitioned table to contain the partition key, so a point is unique
on ``(device, client_id, ts)`` rather than ``(device, client_id)``, and its primary key is
``(id, ts)``. A phone must therefore never send one ``client_id`` with two different ``ts``.
"""

from authentikate.models import User
from django.contrib.postgres.indexes import GistIndex
from django.db import models
from django.db.models import F

from timeline.geo import GeometryPointField, MakePoint


class NextStamp(models.Func):
    """``nextval('timeline_change_seq')``: the next change stamp."""

    template = "nextval('timeline_change_seq')"
    arity = 0
    output_field = models.BigIntegerField()


def stamp_field() -> models.BigIntegerField:
    return models.BigIntegerField(db_default=NextStamp(), help_text="The change stamp (sequence, not time) the changes cursor pages through.")


class TripMode(models.TextChoices):
    """How a trip was travelled, as the phone classified it."""

    WALK = "WALK", "Walk"
    BIKE = "BIKE", "Bike"
    VEHICLE = "VEHICLE", "Vehicle"
    UNKNOWN = "UNKNOWN", "Unknown"


class Device(models.Model):
    """One phone (install) of a user: the ``client_device`` claim of its token.

    Keyed by user as well, so two accounts on one phone never share rows. A reinstall that
    is issued a new device id is a new ``Device``; its old one's history stays readable
    through ``changes``.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="lokate_devices")
    device_id = models.CharField(max_length=2000, help_text="The token's client_device claim.")
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_upload_at = models.DateTimeField(null=True, blank=True, help_text="When this device last wrote anything.")
    segments_from = models.DateTimeField(null=True, blank=True, help_text="The `from` of this device's last replaceSegments.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "device_id"], name="timeline_device_unique")]

    def __str__(self) -> str:
        return f"{self.device_id} of {self.user_id}"


class Point(models.Model):
    """One location fix. Partitioned by month of ``ts`` (see the module docstring)."""

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+", db_constraint=False)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="points", db_constraint=False)
    client_id = models.CharField(max_length=255)
    ts = models.DateTimeField()
    lat = models.FloatField()
    lon = models.FloatField()
    geom = models.GeneratedField(expression=MakePoint(F("lon"), F("lat")), output_field=GeometryPointField(), db_persist=True)
    acc = models.FloatField(null=True, blank=True)
    speed = models.FloatField(null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    alt = models.FloatField(null=True, blank=True)
    received_at = stamp_field()

    class Meta:
        managed = False
        db_table = "timeline_point"


class Visit(models.Model):
    """A stay at one spot, as the phone segmented it."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="visits")
    client_id = models.CharField(max_length=255)
    start = models.DateTimeField()
    end = models.DateTimeField()
    lat = models.FloatField()
    lon = models.FloatField()
    geom = models.GeneratedField(expression=MakePoint(F("lon"), F("lat")), output_field=GeometryPointField(), db_persist=True)
    radius = models.FloatField()
    point_count = models.IntegerField()
    place_client_id = models.CharField(max_length=255, null=True, blank=True, help_text="The client id of the place this visit was matched to.")
    received_at = stamp_field()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "client_id"], name="timeline_visit_unique")]
        indexes = [
            models.Index(fields=["user", "received_at"], name="tl_visit_user_stamp"),
            models.Index(fields=["device", "start"], name="tl_visit_device_start"),
            GistIndex(fields=["geom"], name="tl_visit_geom"),
        ]


class Trip(models.Model):
    """A movement between two visits, as the phone segmented it."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="trips")
    client_id = models.CharField(max_length=255)
    start = models.DateTimeField()
    end = models.DateTimeField()
    from_visit = models.CharField(max_length=255, null=True, blank=True, help_text="The client id of the visit it left.")
    to_visit = models.CharField(max_length=255, null=True, blank=True, help_text="The client id of the visit it arrived at.")
    distance = models.FloatField(help_text="Meters.")
    mode = models.CharField(max_length=16, choices=TripMode.choices, default=TripMode.UNKNOWN)
    received_at = stamp_field()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "client_id"], name="timeline_trip_unique")]
        indexes = [
            models.Index(fields=["user", "received_at"], name="tl_trip_user_stamp"),
            models.Index(fields=["device", "start"], name="tl_trip_device_start"),
        ]


class Place(models.Model):
    """A named place, shared by all of a user's phones (so keyed by user, not device).

    Last write wins on ``updated_at``. A deleted place stays as a tombstone (``deleted_at``
    set) so another phone's older copy is not uploaded again. A tombstone can arrive for a
    place the server never saw, so everything but the key and the stamps is nullable.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    client_id = models.CharField(max_length=255)
    name = models.CharField(max_length=2000, null=True, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)
    geom = models.GeneratedField(expression=MakePoint(F("lon"), F("lat")), output_field=GeometryPointField(), db_persist=True)
    radius = models.FloatField(null=True, blank=True)
    updated_at = models.DateTimeField(help_text="The phone's edit time; last write wins on it.")
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="Set on a tombstone.")
    received_at = stamp_field()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "client_id"], name="timeline_place_unique")]
        indexes = [
            models.Index(fields=["user", "received_at"], name="tl_place_user_stamp"),
            GistIndex(fields=["geom"], name="tl_place_geom"),
        ]
