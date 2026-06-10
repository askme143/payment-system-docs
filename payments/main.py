from __future__ import annotations

from payments.adapters.mongo.indexes import ensure_mongo_indexes
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
