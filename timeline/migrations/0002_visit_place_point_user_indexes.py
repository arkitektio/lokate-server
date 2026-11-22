"""Indexes the reads lean on.

``(user, place_client_id)`` on visits: a place's visit count and latest visit, asked for a
whole list of places at once. ``(user_id, ts)`` on points: a day, a route and the stats all
read one user's points in a time range, which the device-keyed index cannot answer.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('timeline', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddIndex(
            model_name='visit',
            index=models.Index(fields=['user', 'place_client_id'], name='tl_visit_user_place'),
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS tl_point_user_ts ON timeline_point (user_id, ts);",
            "DROP INDEX IF EXISTS tl_point_user_ts;",
        ),
    ]
