"""Monthly partitions of ``timeline_point``, created when a batch first needs them.

Nothing loops in this service (see the deployment's stateless-backend rule), so a month's
partition is created by the first upload that carries a point in it. Partitions are shared by
all users, which the per-user write lock does not cover: creation takes a *global* advisory
lock, in its own short transaction, before the upload's transaction starts. The existence
check is a plain catalog read, so the common case (the month exists) takes no lock at all.
"""

import datetime

from django.db import connection, transaction

#: The advisory lock key (two-int form) serialising partition DDL across replicas.
PARTITION_LOCK = (0x6C6F, 1)

#: Months this process has seen exist; partitions are never dropped while it runs.
_known: set[str] = set()


def month_start(ts: datetime.datetime) -> datetime.date:
    """The first day of ``ts``'s month, in UTC."""
    utc = ts.astimezone(datetime.timezone.utc)
    return datetime.date(utc.year, utc.month, 1)


def next_month(day: datetime.date) -> datetime.date:
    return datetime.date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def partition_name(day: datetime.date) -> str:
    return f"timeline_point_y{day.year:04d}m{day.month:02d}"


def _exists(cursor, name: str) -> bool:  # noqa: ANN001
    cursor.execute(
        "SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'timeline_point'::regclass AND c.relname = %s",
        [name],
    )
    return cursor.fetchone() is not None


def ensure_point_partitions(timestamps: list[datetime.datetime]) -> None:
    """Create the monthly partitions ``timestamps`` fall into, where missing.

    Must run outside any transaction the caller will insert in: the DDL commits on its own.
    """
    months = sorted({month_start(ts) for ts in timestamps})
    missing = [m for m in months if partition_name(m) not in _known]
    if not missing:
        return
    with connection.cursor() as cursor:
        for month in missing:
            name = partition_name(month)
            if not _exists(cursor, name):
                with transaction.atomic():
                    cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(PARTITION_LOCK))
                    if not _exists(cursor, name):
                        cursor.execute(
                            f"CREATE TABLE {name} PARTITION OF timeline_point FOR VALUES FROM (%s) TO (%s)",
                            [f"{month.isoformat()} 00:00:00+00", f"{next_month(month).isoformat()} 00:00:00+00"],
                        )
            _known.add(name)


def forget_known() -> None:
    """Drop the in-process cache (tests that recreate the database)."""
    _known.clear()
