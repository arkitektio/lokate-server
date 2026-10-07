# lokate-server

An optional, self-hosted backup of a phone's location timeline, following the
[Arkitekt](https://arkitekt.live) server patterns. The phone records and segments on its
own; lokate holds a copy it can restore from. It is registered as `live.arkitekt.lokate`
and has a python client, [`lokate`](https://github.com/arkitektio/lokate).

## What it stores

| Table | Unique key | Notes |
| --- | --- | --- |
| `timeline_point` | `(device, client_id, ts)` | partitioned by month of `ts`, GiST on `geom` |
| `timeline_visit` | `(device, client_id)` | |
| `timeline_trip` | `(device, client_id)` | |
| `timeline_place` | `(user, client_id)` | shared by all of a user's phones; deleted places stay as tombstones |

The user and the device come from the token: the device is its `client_device` claim.
Every resolver filters by user, and every write is scoped to the device. There is no admin
or cross-user API. `geom` is a generated PostGIS column computed from `lat`/`lon`, so the
service needs no GDAL.

**The point key includes `ts`.** Postgres requires every unique key of a partitioned table
to contain the partition key, so a point is unique on `(device, client_id, ts)`, not
`(device, client_id)`. A phone must never send one `client_id` with two different
timestamps.

## API

GraphQL is served at `/graphql`, with the SDL at `/schema`.

| Operation | What it does |
| --- | --- |
| `uploadPoints(points)` | Stores up to 1000 points. Returns `accepted` and `duplicates`. |
| `replaceSegments(from, visits, trips)` | In one transaction, makes this device's visits and trips that start at or after `from` exactly the ones sent. |
| `syncPlaces(places, deleted)` | Last write wins on `updatedAt`, and a tombstone beats an update at the same instant or older. Server copies that win come back in `stale`. |
| `syncState` | The calling device's newest point, its point count, and the `from` of its last replace. |
| `changes(cursor, limit)` | Everything the user has, from all devices, in change order. Page until `hasMore` is false. |
| `deleteServerCopy(confirm: "DELETE")` | Deletes everything of yours on the server. |
| `devices`, `points`, `visits`, `trips`, `places` (+ `…Count`, and one by id) | The stored rows, paginated and orderable. Filters: time (`since`/`until`), `devices`, a map box (`inBox`), a radius (`near`), plus per-type ones (`maxAccuracy`, `places`, `minDuration`, `modes`, `search`, …). |
| `day(date, timezone)` | A calendar day's visits and trips in order, with totals. |
| `route(since, until, devices, simplify, maxAccuracy)` | One GeoJSON LineString per device, optionally simplified, with its geodesic length. |
| `stats(since, until, granularity, timezone)` | Points, visits, trips, and distance and time per mode, for each day, week or month. |
| `placeStats(since, until)` | Time spent per place. |

Every read returns only the caller's rows, from all of their devices. Tombstoned places are sync bookkeeping and are never listed.

Rules:

- **Every write is safe to repeat.** Writes upsert on the unique key. An upsert that changes nothing keeps the row's change stamp, so a retried batch changes nothing.
- **The changes cursor is exact.** `received_at` is a sequence stamp. All of one user's writes hold that user's advisory lock from before they draw a stamp until they commit, and `changes` reads the four tables in a single statement. A reader therefore never pages past a row that commits later. `tests/test_backup.py` proves this with two writers that commit out of order.
- **Batches over 1000 rows are refused** with `BATCH_TOO_LARGE`.
- **Row values are stored as sent.** Only a batch the phone's own algorithm could never produce is refused: one that is too large, repeats a clientId, names no device, or has a segment before `from`. The phone retries a failed batch forever, so refusing a single point with a skewed clock would stall its backup for good.
- **Nothing loops in this service.** Monthly partitions are created on demand by the first upload that needs them.

## Hub integration

Declared in [`lokate_server/contract.py`](lokate_server/contract.py):

- **Scopes**: `lokate_read`, `lokate_write`.
- **Needs**: tokens issued by lok, and nothing else of the hub. lokate has no instance key,
  is not a rekuest service and offers no actions.

## Running

The image is `jhnnsrs/lokate`. It has no default command, and starting it takes two steps:

```sh
arkitekt-service run migrate   # wait for the database, migrate, ensureadmin
arkitekt-service serve                          # serve on :80 (daphne), and nothing else
```

`arkitekt-service debug` does both in one go with Django's autoreloading server, for development.

It needs Postgres with PostGIS ([`jhnnsrs/daten`](https://github.com/arkitektio/daten-server))
and Redis.

## Development

```sh
uv sync
uv run pytest          # brings up jhnnsrs/daten (postgres + PostGIS) via dokker
docker compose up      # a standalone dev stack on :8888
```

See [CONFIG.md](CONFIG.md) for every configuration value.

## Releases

Releases are tags: a push to `main` cuts a stable version, a push to `next` a release
candidate. Each one publishes `jhnnsrs/lokate` under its version (`X.Y.Z`, `X.Y`, `X`), plus
`latest` from `main` and `next` from `next`. The `version` in `pyproject.toml` is a
placeholder. Release notes are on
[GitHub Releases](https://github.com/arkitektio/lokate-server/releases).
