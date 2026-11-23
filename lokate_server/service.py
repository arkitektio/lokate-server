"""lokate as a service of the hub: the models behind what its contract says it hosts.

What exists here is declared once, as data, in ``lokate_server.contract`` (``hosts``); this module
only binds each structure to its model. They are there so that a person can share part of their
timeline with a guest of their organization, view only, through a collaboration (lok's, by key):
a visit (with the place it was matched to), a trip, a place. Nothing of a timeline is announced
(no signals: it is one person's, not the organization's), and nothing is described.

Never shared: location fixes (no structure: the densest of it, on the partitioned point table) and
devices. A visit, trip or place is a row of one person's device or of the person: neither is a
structure, so nothing ever brings a whole device or person into a collaboration.

No workflow creates anything here (a phone backs up, nothing records a task): sharing a workflow
reports every structure unknown and puts nothing in.
"""

from arkitekt_service.service import Service

from lokate_server.contract import contract
from timeline import models

service = Service(
    "lokate",
    hosts=contract.description.hosts,
    description="A backup of your location timeline.",
)


def _visit_needs(visit: models.Visit) -> list[models.Place]:
    """The place the visit was matched to (its owner's, not deleted), so it is shown with it."""
    if not visit.place_client_id:
        return []
    return list(
        models.Place.objects.filter(
            user=visit.user, client_id=visit.place_client_id, deleted_at=None
        )
    )


visit = service.structure(models.Visit, "@lokate/visit", depends=_visit_needs)
trip = service.structure(models.Trip, "@lokate/trip")
place = service.structure(models.Place, "@lokate/place")
