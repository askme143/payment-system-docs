from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIRoute

from conftest import FakeCatalogRepository, FakePaymentRepository, FixedClock
from payments.http.composition import create_app
from payments.http.dependencies import HttpDependencies

IMPLEMENTED_API_IDS = {
    "plans-list",
    "plans-detail",
    "payments-orders",
    "payments-detail",
}


def test_first_slice_routes_match_documentation() -> None:
    docs_path = Path(__file__).resolve().parents[2] / "docs-data" / "documentation.json"
    data = json.loads(docs_path.read_text())
    documented_routes = {
        (api["method"], api["path"])
        for api in data["apis"]
        if api["id"] in IMPLEMENTED_API_IDS
    }
    app = create_app(
        HttpDependencies(
            catalog_repository=FakeCatalogRepository(),
            payment_repository=FakePaymentRepository(),
            clock=FixedClock(),
            internal_service_token="secret",
        )
    )
    app_routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST", "PATCH", "DELETE"}
    }

    assert documented_routes <= app_routes
