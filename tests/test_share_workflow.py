"""Sharing a workflow: nothing of a timeline is made by one.

A timeline is backed up by phones; no lokate row records the task that made it (no history, no
``created_through``). Sharing a workflow reports every structure unknown and puts nothing in.
"""

import uuid

import pytest
from arkitekt_service.collaborations.models import Collaboration, CollaborationItem

from tests.runner import Run

pytestmark = pytest.mark.django_db(transaction=True)

SHARE = """
mutation ($input: ShareWorkflowInput!) {
  shareWorkflow(input: $input) { dryRun added { identifier object } covered skipped { identifier } unknown }
}
"""


async def test_every_structure_is_unknown_and_nothing_goes_in(run: Run) -> None:
    await run(
        """mutation { replaceSegments(from: "2026-08-30T00:00:00Z", visits: [{clientId: "v", start: "2026-08-30T08:00:00Z", end: "2026-08-30T09:00:00Z", lat: 48.2, lon: 16.37, radius: 40, pointCount: 3}], trips: []) { visits } }"""
    )
    key = str(uuid.uuid4())
    for dry_run in (True, False):
        report = (
            await run(
                SHARE,
                {
                    "input": {
                        "collaboration": key,
                        "rootTask": "root-1",
                        "dryRun": dry_run,
                    }
                },
            )
        )["shareWorkflow"]
        assert report["added"] == [] and report["skipped"] == []
        assert report["covered"] == 0
        assert sorted(report["unknown"]) == [
            "@lokate/place",
            "@lokate/trip",
            "@lokate/visit",
        ]
    # Sharing it for real made only lokate's (empty) row of the collaboration.
    collaboration = await Collaboration.objects.aget(key=key)
    assert not await CollaborationItem.objects.filter(
        collaboration=collaboration
    ).aexists()
