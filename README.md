# lokate-server

An optional, self-hosted backup of a phone's location timeline, following the
[Arkitekt](https://arkitekt.live) server patterns. The phone records and segments on its
own; lokate holds a copy it can restore from. It is registered as `live.arkitekt.lokate`
and has a python client, [`lokate`](https://github.com/jhnnsrs/lokate).

## What it stores

| Table | Unique key | Notes |
| --- | --- | --- |
| `timeline_point` | `(device, client_id, ts)` | partitioned by month of `ts`, GiST on `geom` |
| `timeline_visit` | `(device, client_id)` | |
| `timeline_trip` | `(device, client_id)` | |
| `timeline_place` | `(user, client_id)` | shared by all of a user's phones; deleted places stay as tombstones |
| `timeline_accesslog` | | one row per read; the user can list their own |

The user and the device come from the token: the device is its `client_device` claim.
Every resolver filters by user, and every write is scoped to the device. There is no admin
or cross-user API. `geom` is a generated PostGIS column computed from `lat`/`lon`, so the
service needs no GDAL.

**The point key includes `ts`.** Postgres requires every unique key of a partitioned table
to contain the partition key, so a point is unique on `(device, client_id, ts)`, not
`(device, client_id)`. A phone must never send one `client_id` with two different
timestamps.

## API

| Operation | What it does |
| --- | --- |
| `uploadPoints(points)` | Stores up to 1000 points. Returns `accepted` and `duplicates`. |
| `replaceSegments(from, visits, trips)` | In one transaction, makes this device's visits and trips that start at or after `from` exactly the ones sent. |
| `syncPlaces(places, deleted)` | Last write wins on `updatedAt`, and a tombstone beats an update at the same instant or older. Server copies that win come back in `stale`. |
| `syncState` | The calling device's newest point, its point count, and the `from` of its last replace. |
| `changes(cursor, limit)` | Everything the user has, from all devices, in change order. Page until `hasMore` is false. |
| `accessLog`, `retention`, `setRetention`, `deleteServerCopy(confirm: "DELETE")` | Tools for your own data. |

Rules:

- **Every write is safe to repeat.** Writes upsert on the unique key. An upsert that changes nothing keeps the row's change stamp, so a retried batch changes nothing.
- **The changes cursor is exact.** `received_at` is a sequence stamp. All of one user's writes hold that user's advisory lock from before they draw a stamp until they commit, and `changes` reads the four tables in a single statement. A reader therefore never pages past a row that commits later. `tests/test_backup.py` proves this with two writers that commit out of order.
- **Batches over 1000 rows are refused** with `BATCH_TOO_LARGE`.
- **Row values are stored as sent.** Only a batch the phone's own algorithm could never produce is refused: one that is too large, repeats a clientId, names no device, or has a segment before `from`. The phone retries a failed batch forever, so refusing a single point with a skewed clock would stall its backup for good.
- **Retention is enforced on the user's own writes.** Nothing loops in this service. Monthly partitions are created on demand by the first upload that needs them.

## Development

```sh
uv sync
uv run pytest          # brings up jhnnsrs/daten (postgres + PostGIS) via dokker
docker compose up      # a standalone dev stack on :8888
```

See [CONFIG.md](CONFIG.md) for every configuration value.
