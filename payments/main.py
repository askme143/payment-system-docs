from __future__ import annotations

from payments.adapters.mongo.indexes import ensure_mongo_indexes
from payments.adapters.mongo.notifications import MongoNotificationTemplateRepository
from payments.adapters.time import SystemClock
from payments.application.notifications import seed_notification_templates_if_empty
from payments.http.composition import (
    build_http_dependencies,
    create_app,
    create_mongo_database,
)
from payments.http.config import payment_config_from_env

config = payment_config_from_env()
database = create_mongo_database(config)
app = create_app(build_http_dependencies(database, config))


@app.on_event("startup")
async def startup() -> None:
    await ensure_mongo_indexes(database)
    await seed_notification_templates_if_empty(
        template_repository=MongoNotificationTemplateRepository(
            database.notification_templates
        ),
        clock=SystemClock(),
    )
