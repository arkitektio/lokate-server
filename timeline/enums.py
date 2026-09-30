"""GraphQL enums."""

from enum import Enum

import strawberry


@strawberry.enum(description="How a trip was travelled, as the phone classified it.")
class TripMode(Enum):
    WALK = "WALK"
    BIKE = "BIKE"
    VEHICLE = "VEHICLE"
    UNKNOWN = "UNKNOWN"


@strawberry.enum(description="The width of a stats bucket.")
class Granularity(Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
