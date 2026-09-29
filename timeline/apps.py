from django.apps import AppConfig


class TimelineConfig(AppConfig):
    """The backed-up copy of a phone's location timeline."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "timeline"
