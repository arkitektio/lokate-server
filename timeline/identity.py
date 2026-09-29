"""Who is calling: the user and the device, both from the token.

The device is the token's ``client_device`` claim, read from the token itself — never from
``Client.device``, which authentikate keeps as "the device this OAuth client was last seen
on" and so can name another phone. Writes, and the per-device ``syncState``, need one.
"""

from dataclasses import dataclass

from authentikate.models import User
from graphql import GraphQLError
from kante.types import Info


@dataclass(frozen=True)
class Caller:
    """The identity a request acts as."""

    user: User
    device_id: str | None
    client_id: str | None

    def require_device(self) -> str:
        if not self.device_id:
            raise GraphQLError(
                "This token names no device (its client_device claim is empty); lokate keys every write by device.",
                extensions={"code": "NO_DEVICE"},
            )
        return self.device_id


def caller(info: Info) -> Caller:
    """The calling user and device. Anonymous calls are refused."""
    request = info.context.request
    user = request._user  # unset without a token; the property would raise a ValueError instead
    if user is None or getattr(user, "is_anonymous", True):
        raise GraphQLError("Authentication required.", extensions={"code": "UNAUTHENTICATED"})
    token = request._extensions.get("token")
    return Caller(
        user=user,  # type: ignore[arg-type]
        device_id=getattr(token, "client_device", None) or None,
        client_id=getattr(token, "client_id", None),
    )
