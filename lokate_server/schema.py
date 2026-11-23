"""The GraphQL schema of lokate: the server copy of your phones' location timeline.

The phone records and segments on its own; it backs up here (``uploadPoints``,
``replaceSegments``, ``syncPlaces``) and restores from here (``changes``). Everything can be
read back: as plain lists (``points``, ``visits``, …, filterable and paginated) and as
aggregates (``day``, ``route``, ``stats``, ``placeStats``).

Every field acts as the token's user and only ever sees that user's rows; every write is the
token's device (``client_device``). There is no admin or cross-user API.

A person shares visits, trips and places of their own with a guest of their organization through a
collaboration (lok's, by key). The guest reads what was put in their collaborations and nothing
else (``timeline.types.UserScoped``, and the ``CollaborationGate``, which drops whatever a resolver
hands out that is in none of them); aggregates (``day``, ``route``, ``stats``, counts, ``changes``)
are closed to them, and no mutation is open to them.
"""

from typing import Any

import strawberry
import strawberry_django
from arkitekt_service.collaborations import graphql as collaboration_types
from arkitekt_service.graphql import (
    CollaborationGate,
    TelemetryExtension,
    annotate_schema,
)
from authentikate.strawberry.directives import AuthExtension
from authentikate.strawberry.extension import AuthentikateExtension
from django.db.models import Model
from kante.types import Info
from strawberry_django.optimizer import DjangoOptimizerExtension

from lokate_server.contract import EDITOR
from lokate_server.logs import QuietErrorsSchema
from lokate_server.service import service as lokate_service
from timeline import backup, graphql, models, queries, types

#: What a person may put in a collaboration: their own visits, trips and (live) places. A location
#: fix and a device never are.
SHAREABLE: frozenset[type[Model]] = frozenset({models.Visit, models.Trip, models.Place})


def _shareable(model: type[Model], id: strawberry.ID, info: Info) -> Model:
    """The caller's own object a collaboration mutation names; refused unless it is shareable."""
    if model not in SHAREABLE:
        raise PermissionError(
            f"A {model.__name__} is never put in a collaboration: only visits, trips and places are"
        )
    rows = model._default_manager.filter(user=info.context.request.user)
    if model is models.Place:
        rows = rows.filter(deleted_at=None)
    found: Model = rows.get(pk=id)
    return found


#: The collaborations a timeline is shared in, which a guest of the organization is granted.
collaboration_api = collaboration_types.resolvers(fetch=_shareable)


def field(scope: str = "lokate_read", **kwargs: Any) -> Any:  # noqa: ANN401 - as strawberry_django.field: the value of `x: T = field()`
    """A query field an app needs ``scope`` for (``lokate_read``, unless it says otherwise)."""
    return strawberry_django.field(extensions=[AuthExtension(scopes=[scope])], **kwargs)


def mutation(
    scope: str = "lokate_write", roles: list[str] | None = None, **kwargs: Any
) -> Any:  # noqa: ANN401 - as strawberry_django.mutation: the value of `x: T = mutation()`
    """A write: an app needs ``scope`` for it, a member one of ``roles`` (``lokate_editor`` unless it
    says otherwise; ``lokate_admin`` holds every role)."""
    return strawberry_django.mutation(
        extensions=[AuthExtension(scopes=[scope], any_role_of=roles or [EDITOR])],
        **kwargs,
    )


@strawberry.type
class Query:
    """The root query type."""

    # Collaborations
    collaborations: list[collaboration_types.Collaboration] = field(
        resolver=collaboration_api.collaborations,
        description="The organization's collaborations lokate holds visits, trips or places in (a guest: the ones granted to them).",
    )
    collaboration: collaboration_types.Collaboration = field(
        resolver=collaboration_api.collaboration,
        description="What lokate holds in a collaboration, by its key (lok's).",
    )
    collaborations_stats: collaboration_types.CollaborationStats = field(
        resolver=collaboration_api.collaborations_stats,
        description="Count the collaborations `collaborations` lists, and the objects in them.",
    )

    # The stored rows, as lists (paginated, filterable, orderable) and one by id.
    devices: list[types.Device] = field(
        description="Your phones (installs) that have backed up here."
    )
    device: types.Device = field(resolver=queries.device, description="A device by id.")
    points: list[types.Point] = field(
        description="Location fixes (paginated, filterable by time, device, box or distance)."
    )
    points_count: int = field(
        resolver=queries.points_count,
        description="How many points match (the same filters as `points`).",
    )
    point: types.Point = field(resolver=queries.point, description="A point by id.")
    visits: list[types.Visit] = field(
        description="Stays (paginated, filterable by time, device, place, box or distance)."
    )
    visits_count: int = field(
        resolver=queries.visits_count,
        description="How many visits match (the same filters as `visits`).",
    )
    visit: types.Visit = field(resolver=queries.visit, description="A visit by id.")
    trips: list[types.Trip] = field(
        description="Movements between visits (paginated, filterable by time, device, mode or distance)."
    )
    trips_count: int = field(
        resolver=queries.trips_count,
        description="How many trips match (the same filters as `trips`).",
    )
    trip: types.Trip = field(resolver=queries.trip, description="A trip by id.")
    places: list[types.Place] = field(
        description="Your named places (paginated, filterable by name, box or distance)."
    )
    place: types.Place = field(resolver=queries.place, description="A place by id.")

    # Aggregates.
    day: graphql.Day = field(
        resolver=graphql.day,
        description="One calendar day (in `timezone`, IANA name): its visits and trips in order, and totals.",
    )
    route: list[graphql.Track] = field(
        resolver=graphql.route,
        description="Your path between `since` and `until`, one GeoJSON line per device.",
    )
    stats: list[graphql.StatsBucket] = field(
        resolver=graphql.stats,
        description="Points, visits, trips and distance per day, week or month.",
    )
    place_stats: list[graphql.PlaceStat] = field(
        resolver=graphql.place_stats, description="Time spent per place, most first."
    )

    # Sync.
    sync_state: graphql.SyncState = field(
        resolver=graphql.sync_state, description="The calling device's watermarks."
    )
    changes: graphql.ChangeSet = field(
        resolver=graphql.changes,
        description=f"Everything of yours, from all your devices, changed after `cursor`, oldest first (limit at most {backup.MAX_BATCH}). Page until hasMore is false; used to restore.",
    )


@strawberry.type
class Mutation:
    """The root mutation type."""

    # Collaborations (view only for the guests they are granted to)
    add_to_collaboration = mutation(
        scope="lokate_share",
        resolver=collaboration_api.add_to_collaboration,
        description="Put one of your visits (with the place it was matched to), trips or places in a collaboration (lok's, by its key) for its guests to read. Never a location fix or a device. The first thing put in makes lokate's row of the collaboration.",
    )
    add_many_to_collaboration = mutation(
        scope="lokate_share",
        resolver=collaboration_api.add_many_to_collaboration,
        description="Put several of your visits, trips or places in a collaboration at once; all or none.",
    )
    share_workflow = mutation(
        scope="lokate_share",
        resolver=collaboration_api.share_workflow,
        description="Put what a workflow (its root task) created in lokate in a collaboration. A timeline is backed up by phones, not made by workflows: every structure is reported unknown and nothing is put in.",
    )
    remove_from_collaboration = mutation(
        scope="lokate_share",
        resolver=collaboration_api.remove_from_collaboration,
        description="Take a visit, trip or place out of a collaboration, with what it brought that nothing else there needs.",
    )
    delete_collaboration = mutation(
        scope="lokate_share",
        resolver=collaboration_api.delete_collaboration,
        description="Take everything lokate holds in a collaboration (by its key) out of it: its guests read none of it here any more. lok's collaboration and its grants stay.",
    )

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
    # A guest of the organization is handed only what is in their collaborations (CollaborationGate).
    extensions=[
        TelemetryExtension,
        DjangoOptimizerExtension,
        AuthentikateExtension,
        CollaborationGate,
    ],
)

# Which structure each type is (`@identified`), the contract's catch-alls (`lokate_all` passes
# every scope check, `lokate_admin` every role check), and, behind the gate, that lokate lets
# guests in.
annotate_schema(schema, lokate_service)
