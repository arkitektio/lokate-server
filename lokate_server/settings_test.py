from .settings import *  # noqa
from .settings import AUTHENTIKATE, DATABASES
import logging
import os

# The test stack (tests/integration/docker-compose.yaml) publishes postgres on an *ephemeral*
# host port: the root conftest's `django_db_modify_db_settings` overwrites PORT with what
# docker picked, before pytest-django creates the test database. This value is only the
# fallback for running a `tmanage.py` command against a stack you started by hand -- set
# LOKATE_TEST_DB_PORT to whatever `docker compose port db 5432` reports for it.
DATABASES["default"] = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "testdb",
    "USER": "test",
    "PASSWORD": "test",
    "HOST": "localhost",
    "PORT": os.environ.get("LOKATE_TEST_DB_PORT", "5432"),
}

# Django forces DEBUG=False under the test runner, and authentikate refuses static tokens
# when DEBUG is False. These are deliberate test fixtures, so opt in explicitly.
AUTHENTIKATE = {
    **AUTHENTIKATE,
    "allow_static_tokens_in_production": True,
    "static_tokens": {
        # One user, two phones: same sub, different client_device.
        "phone-a": {"sub": "1", "client_device": "phone-a"},
        "phone-b": {"sub": "1", "client_device": "phone-b"},
        # Another user, on a phone that happens to report the same device id as phone-a.
        "other": {"sub": "9", "client_device": "phone-a"},
        # A token that names no device.
        "nodevice": {"sub": "1", "client_device": ""},
    },
}

# Disable logging during tests to reduce noise
logging.disable(logging.CRITICAL)

# Use in-memory channel layer for tests instead of Redis
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
