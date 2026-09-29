"""The GraphQL schema of lokate: a backup of a phone's location timeline.

The phone records and segments on its own; this service holds a copy it can restore from.
Every field acts as the token's user, and every write as the token's device
(``client_device``). There is no admin or cross-user API.

* ``AuthentikateExtension`` — authenticates the request from its bearer token and exposes
  the user and the token on ``info.context.request``.
"""

import strawberry
from authentikate.strawberry.extension import AuthentikateExtension

from lokate_server.logs import QuietErrorsSchema
from timeline import backup, graphql


@strawberry.type
class Query:
    """The root query type."""

    sync_state: graphql.SyncState = strawberry.field(resolver=graphql.sync_state, description="The calling device's watermarks.")
    changes: graphql.ChangeSet = strawberry.field(
        resolver=graphql.changes,
        description=f"Everything of the user's, from all their devices, changed after `cursor`, oldest first (limit at most {backup.MAX_BATCH}). Page until hasMore is false; used to restore.",
    )
    access_log: list[graphql.AccessLogEntry] = strawberry.field(resolver=graphql.access_log, description="Every read of your data, newest first.")
    retention: graphql.Retention = strawberry.field(resolver=graphql.retention, description="How long the server keeps your points and segments.")


@strawberry.type
class Mutation:
    """The root mutation type."""

    upload_points: graphql.UploadResult = strawberry.mutation(
        resolver=graphql.upload_points,
        description=f"Store points (at most {backup.MAX_BATCH} per call). Safe to repeat: a point already stored counts as a duplicate.",
    )
    replace_segments: graphql.ReplaceResult = strawberry.mutation(
        resolver=graphql.replace_segments,
        description="In one transaction, make this device's visits and trips starting at or after `from` exactly the ones sent.",
    )
    sync_places: graphql.PlaceSyncResult = strawberry.mutation(
        resolver=graphql.sync_places,
        description="Merge places and tombstones, last write wins on updatedAt; newer server copies come back in `stale`.",
    )
    delete_server_copy: int = strawberry.mutation(
        resolver=graphql.delete_server_copy,
        description=f'Delete all of your data on this server (confirm = "{backup.CONFIRM_DELETE}"). Returns how many points, visits, trips and places were deleted.',
    )
    set_retention: graphql.Retention = strawberry.mutation(
        resolver=graphql.set_retention,
        description="Keep your points and segments this many days (null: forever). Older ones are deleted now and on every later upload.",
    )


# A federation schema: the authentikate types are federated entities carrying ``@key``.
class Schema(QuietErrorsSchema, strawberry.federation.Schema):
    """strawberry.federation.Schema, logging expected resolver errors as one line and bugs with a traceback (see logs.py)."""


schema = Schema(
    query=Query,
    mutation=Mutation,
    extensions=[AuthentikateExtension],
)
