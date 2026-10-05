"""What this image answers a hub's installer: ``python -m arkitekt_service <verb>`` (see ``arkitekt_service.contract``).

The installer knows the hub; how this release spells its config is written here, with the
settings it is read by. A key renamed in ``configuration.py`` is renamed in :func:`render` in
the same commit, and no installer has to learn of it.
"""

from __future__ import annotations

from arkitekt_service.contract import JSON, Contract, Description, Facts, Needs, Offers, Scope, blocks

from lokate_server.configuration import Settings

#: What a token may be allowed to do here: defined at the coordination server when the hub enrols.
SCOPES = [
    Scope(key="lokate_read", description="Read your backed-up location timeline"),
    Scope(key="lokate_write", description="Back up your location timeline"),
]


def render(facts: Facts) -> dict[str, JSON]:
    """This release's config for the hub ``facts`` describes."""
    document: dict[str, JSON] = blocks.server(facts)
    return document


contract = Contract(
    description=Description(
        name="lokate",
        summary="A backup of your location timeline.",
        needs=Needs(scopes=SCOPES, storage=["media"]),
        offers=Offers(),
    ),
    settings=Settings,
    render=render,
    setup=(("ensureadmin",),),
)
