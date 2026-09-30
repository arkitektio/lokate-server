"""The GraphQL schema of lokate: the server copy of your phones' location timeline.

The phone records and segments on its own; it backs up here (``uploadPoints``,
``replaceSegments``, ``syncPlaces``) and restores from here (``changes``). Everything can be
read back: as plain lists (``points``, ``visits``, …, filterable and paginated) and as
aggregates (``day``, ``route``, ``stats``, ``placeStats``).

Every field acts as the token's user and only ever sees that user's rows; every write is the
token's device (``client_device``). There is no admin or cross-user API.
"""

import strawberry
import strawberry_django
from authentikate.strawberry.directives import AuthExtension
from authentikate.strawberry.extension import AuthentikateExtension
from strawberry_django.optimizer import DjangoOptimizerExtension

from lokate_server.logs import QuietErrorsSchema
from timeline import backup, graphql, queries, types


def field(**kwargs):  # noqa: ANN201
    """A query field that requires authentication."""
    return strawberry_django.field(extensions=[AuthExtension()], **kwargs)


def mutation(**kwargs):  # noqa: ANN201
    """A mutation that requires authentication."""
    return strawberry_django.mutation(extensions=[AuthExtension()], **kwargs)


@strawberry.type
class Query:
    """The root query type."""

    # The stored rows, as lists (paginated, filterable, orderable) and one by id.
    devices: list[types.Device] = field(description="Your phones (installs) that have backed up here.")
    device: types.Device = field(resolver=queries.device, description="A device by id.")
    points: list[types.Point] = field(description="Location fixes (paginated, filterable by time, device, box or distance).")
    points_count: int = field(resolver=queries.points_count, description="How many points match (the same filters as `points`).")
    point: types.Point = field(resolver=queries.point, description="A point by id.")
    visits: list[types.Visit] = field(description="Stays (paginated, filterable by time, device, place, box or distance).")
    visits_count: int = field(resolver=queries.visits_count, description="How many visits match (the same filters as `visits`).")
    visit: types.Visit = field(resolver=queries.visit, description="A visit by id.")
    trips: list[types.Trip] = field(description="Movements between visits (paginated, filterable by time, device, mode or distance).")
    trips_count: int = field(resolver=queries.trips_count, description="How many trips match (the same filters as `trips`).")
    trip: types.Trip = field(resolver=queries.trip, description="A trip by id.")
    places: list[types.Place] = field(description="Your named places (paginated, filterable by name, box or distance).")
    place: types.Place = field(resolver=queries.place, description="A place by id.")

    # Aggregates.
    day: graphql.Day = field(resolver=graphql.day, description="One calendar day (in `timezone`, IANA name): its visits and trips in order, and totals.")
    route: list[graphql.Track] = field(resolver=graphql.route, description="Your path between `since` and `until`, one GeoJSON line per device.")
    stats: list[graphql.StatsBucket] = field(resolver=graphql.stats, description="Points, visits, trips and distance per day, week or month.")
    place_stats: list[graphql.PlaceStat] = field(resolver=graphql.place_stats, description="Time spent per place, most first.")

    # Sync.
    sync_state: graphql.SyncState = field(resolver=graphql.sync_state, description="The calling device's watermarks.")
    changes: graphql.ChangeSet = field(
        resolver=graphql.changes,
        description=f"Everything of yours, from all your devices, changed after `cursor`, oldest first (limit at most {backup.MAX_BATCH}). Page until hasMore is false; used to restore.",
    )


@strawberry.type
class Mutation:
    """The root mutation type."""

    upload_points: graphql.UploadResult = mutation(
        resolver=graphql.upload_points,
        description=f"Store points (at most {backup.MAX_BATCH} per call). Safe to repeat: a point already stored counts as a duplicate.",
    )
    replace_segments: graphql.ReplaceResult = mutation(
        resolver=graphql.replace_segments,
        description="In one transaction, make this device's visits and trips starting at or after `from` exactly the ones sent.",
    )
    sync_places: graphql.PlaceSyncResult = mutation(
        resolver=graphql.sync_places,
        description="Merge places and tombstones, last write wins on updatedAt; newer server copies come back in `stale`.",
    )
    delete_server_copy: int = mutation(
        resolver=graphql.delete_server_copy,
        description=f'Delete all of your data on this server (confirm = "{backup.CONFIRM_DELETE}"). Returns how many points, visits, trips and places were deleted.',
    )


# A federation schema: the authentikate types are federated entities carrying ``@key``.
class Schema(QuietErrorsSchema, strawberry.federation.Schema):
    """strawberry.federation.Schema, logging expected resolver errors as one line and bugs with a traceback (see logs.py)."""


schema = Schema(
    query=Query,
    mutation=Mutation,
    extensions=[DjangoOptimizerExtension, AuthentikateExtension],
)
