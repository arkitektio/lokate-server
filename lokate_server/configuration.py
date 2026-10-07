"""Typed, fully-documented configuration schema for the **lokate** service.

Owned by this service. Values resolve (highest precedence first) from init
kwargs, environment variables (nested via ``__`` — e.g. ``POSTGRES__PASSWORD``),
then the YAML file (``config.yaml`` where the service runs by default; override with
``ARKITEKT_CONFIG_FILE``). Secret fields have **no default**: loading fails fast
with a ``ValidationError`` if they are not supplied via config or environment.
"""


from arkitekt_service.server.settings import DjangoSettings, PostgresSettings, RedisSettings, ServiceSettings
from authentikate.base_models import AuthentikateSettings
from pydantic import Field

class Settings(ServiceSettings):
    """Top-level, validated configuration for the lokate service."""

    django: DjangoSettings = Field(description="Core Django settings.")
    postgres: PostgresSettings = Field(description="PostgreSQL connection.")
    redis: RedisSettings = Field(description="Redis connection.")
    authentikate: AuthentikateSettings = Field(description="Token-verification config (authentikate).")
