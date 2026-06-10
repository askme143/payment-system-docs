# FastAPI Payment Webserver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `payment-fastapi-style` first, then use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first production-shaped FastAPI payment webserver from the documentation source of truth.

**Architecture:** Follow the project-local `payment-fastapi-style` skill: `domain / application / adapters / http`, function-based use cases, explicit ports, `HttpDependencies` composition, and Mongo adapters that preserve payment invariants. Implement the server foundation plus the first vertical slice: `GET /plans`, `GET /plans/{planId}`, `POST /payments/orders`, and `GET /payments/{paymentId}`.

**Tech Stack:** Python 3.14+, uv, FastAPI CLI, Motor/PyMongo, Pydantic v2, pytest, FastAPI TestClient/httpx.

---

## Scope

The documentation currently defines 34 APIs. This plan intentionally implements only the server foundation and four representative APIs so the architecture, tests, and payment invariants are proven before larger flows are added.

Implemented in this plan:

- `GET /health`
- `GET /plans`
- `GET /plans/{planId}`
- `POST /payments/orders`
- `GET /payments/{paymentId}`

Follow-up plans should cover subscription checkout/confirm/cancel/resume/change, billing methods, payment confirm/cancel, Toss webhooks, invoices, internal batch, and admin APIs.

Scheduler reflection:

- Do not implement scheduled work inside FastAPI route modules or app startup hooks.
- Put scheduled business logic in `payments/src/payments/application/jobs/`.
- Put cron/queue/one-shot runner mechanics in `payments/src/payments/scheduler/`.
- Let internal HTTP APIs such as `POST /internal/subscription-billing/run` call the same application job functions used by the scheduler runner.
- Require operation locks, deterministic idempotency scopes, bounded batch sizes, and run summaries for scheduler work.

## Style Decisions

Use the newly created skill at `.agents/skills/payment-fastapi-style/SKILL.md`.

Important decisions:

- Use `payments/src/payments/http/composition.py` for app creation and runtime dependency wiring.
- Use `payments/src/payments/http/router.py` only as the include-only root router.
- Use `payments/src/payments/http/dependencies.py` for `HttpDependencies` and auth/header parsing.
- Use `payments/src/payments/http/errors.py` for application error to HTTP response mapping.
- Use `payments/src/payments/http/schemas/*` for Pydantic request/response schemas.
- Use `payments/src/payments/http/routes/*` for domain route factories.
- Use `payments/src/payments/application/jobs/*` for scheduled job use cases when internal billing work is added.
- Use `payments/src/payments/scheduler/*` for cron/queue/runner adapters when scheduled execution is added.
- Use `payments/src/payments/application/*.py` for use-case functions, errors, and context.
- Use `payments/src/payments/application/ports/*` for domain-specific Protocols.
- Use `payments/src/payments/adapters/mongo/` for Mongo repositories and indexes.
- Use Python 3.14 or newer. Keep `payments/.python-version` on a stable 3.14+ patch release and `requires-python = ">=3.14"` unless the user explicitly raises the floor.
- Use `uv` for dependency locking and execution. Commit `payments/uv.lock` when dependency changes create or update it.
- Do not put repositories on `request.app.state` for route handlers to fetch directly.
- Do not introduce `pydantic-settings` unless env parsing becomes too large for explicit config functions.
- Use `Clock` port in application logic. Direct `datetime.now()` belongs in adapters only.

## Target File Structure

- Modify: `payments/pyproject.toml`
- Create: `payments/uv.lock`
- Modify: `payments/main.py`
- Create: `payments/src/payments/__init__.py`
- Create: `payments/src/payments/application/__init__.py`
- Create: `payments/src/payments/application/context.py`
- Create: `payments/src/payments/application/errors.py`
- Create: `payments/src/payments/application/ports/__init__.py`
- Create: `payments/src/payments/application/ports/clock.py`
- Create: `payments/src/payments/application/ports/catalog.py`
- Create: `payments/src/payments/application/ports/payments.py`
- Create: `payments/src/payments/application/catalog.py`
- Create: `payments/src/payments/application/payment_orders.py`
- Create: `payments/src/payments/adapters/__init__.py`
- Create: `payments/src/payments/adapters/time.py`
- Create: `payments/src/payments/adapters/mongo/__init__.py`
- Create: `payments/src/payments/adapters/mongo/documents.py`
- Create: `payments/src/payments/adapters/mongo/indexes.py`
- Create: `payments/src/payments/adapters/mongo/catalog.py`
- Create: `payments/src/payments/adapters/mongo/payments.py`
- Create: `payments/src/payments/http/__init__.py`
- Create: `payments/src/payments/http/config.py`
- Create: `payments/src/payments/http/composition.py`
- Create: `payments/src/payments/http/router.py`
- Create: `payments/src/payments/http/dependencies.py`
- Create: `payments/src/payments/http/errors.py`
- Create: `payments/src/payments/http/schemas/__init__.py`
- Create: `payments/src/payments/http/schemas/catalog.py`
- Create: `payments/src/payments/http/schemas/payments.py`
- Create: `payments/src/payments/http/routes/__init__.py`
- Create: `payments/src/payments/http/routes/catalog.py`
- Create: `payments/src/payments/http/routes/payments.py`
- Create: `payments/tests/conftest.py`
- Create: `payments/tests/test_http_composition.py`
- Create: `payments/tests/http/test_catalog_routes.py`
- Create: `payments/tests/http/test_payment_routes.py`
- Create: `payments/tests/test_application_contract.py`
- Create: `payments/tests/test_application_catalog.py`
- Create: `payments/tests/test_application_payment_orders.py`
- Create: `payments/tests/test_api_documentation_contract.py`
- Create: `payments/tests/test_mongo_adapters.py`

## Task 1: Package And Dependencies

**Files:**

- Modify: `payments/pyproject.toml`
- Create: `payments/uv.lock`
- Modify: `payments/main.py`
- Create: `payments/src/payments/__init__.py`

- [ ] **Step 1: Update `payments/pyproject.toml`**

Use `uv` dependency management and Python 3.14+. Keep `payments/.python-version` on a stable 3.14+ patch release and `requires-python = ">=3.14"` unless the user explicitly raises the floor.

Expected shape:

```toml
[project]
name = "payments"
version = "0.1.0"
description = "Payment system FastAPI service"
readme = "README.md"
requires-python = ">=3.14"
dependencies = [
    "fastapi[standard]>=0.115.0",
    "motor>=3.6.0",
    "pydantic>=2.8.0",
    "pymongo>=4.9.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = [
    "httpx2>=2.2.0",
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "pyright>=1.1.409",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]

[tool.fastapi]
entrypoint = "main:app"
```

- [ ] **Step 2: Sync and lock dependencies with uv**

Run:

```bash
cd payments
uv sync --dev
```

Expected: dependencies are installed in the uv-managed environment and `uv.lock` is created or updated.

- [ ] **Step 3: Create package marker**

Create `payments/src/payments/__init__.py`:

```python
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
```

- [ ] **Step 4: Replace `payments/main.py` with composition entrypoint**

Follow the `shortner/main.py` style:

```python
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
```

- [ ] **Step 5: Run import check through uv**

Run:

```bash
cd payments
uv run python -c "from payments import __version__; print(__version__)"
```

Expected: `0.1.0`.

- [ ] **Step 6: Commit**

```bash
git add payments/pyproject.toml payments/uv.lock payments/main.py payments/src/payments/__init__.py
git commit -m "chore: prepare payment FastAPI package"
```

## Task 2: HTTP Config And Composition

**Files:**

- Create: `payments/src/payments/http/__init__.py`
- Create: `payments/src/payments/http/config.py`
- Create: `payments/src/payments/http/composition.py`
- Create: `payments/src/payments/adapters/__init__.py`
- Create: `payments/src/payments/adapters/time.py`
- Create: `payments/tests/test_http_composition.py`

- [ ] **Step 1: Write failing composition tests**

Create `payments/tests/test_http_composition.py` using `unittest.TestCase`. Cover:

- `payment_config_from_env()` loads required env values.
- missing env raises `ValueError`.
- `build_http_dependencies()` wires Mongo repositories and `SystemClock`.
- `create_app()` returns `GET /health` with `{"ok": True}`.

- [ ] **Step 2: Implement `http/config.py`**

Use frozen dataclass plus explicit env parsing:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PaymentHttpConfig:
    database_url: str
    database_name: str
    internal_service_token: str


def payment_config_from_env(
    environ: Mapping[str, str] = os.environ,
) -> PaymentHttpConfig:
    return PaymentHttpConfig(
        database_url=_required_env(environ, "PAYMENTS_DATABASE_URL"),
        database_name=_required_env(environ, "PAYMENTS_DATABASE_NAME"),
        internal_service_token=_required_env(environ, "PAYMENTS_INTERNAL_SERVICE_TOKEN"),
    )


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value
```

- [ ] **Step 3: Implement `adapters/time.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)
```

- [ ] **Step 4: Implement `http/dependencies.py` and `http/composition.py`**

Create `HttpDependencies` in `http/dependencies.py`. Create `create_mongo_database(config)`, `build_http_dependencies(database, config)`, and `create_app(dependencies)` in `http/composition.py` following the `payment-fastapi-style` reference.

- [ ] **Step 5: Run tests**

Run:

```bash
cd payments
uv run pytest tests/test_http_composition.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add payments/src/payments/http payments/src/payments/adapters payments/tests/test_http_composition.py
git commit -m "feat: add HTTP composition for payment service"
```

## Task 3: Application Context, Errors, Ports, And Contract Tests

**Files:**

- Create: `payments/src/payments/application/__init__.py`
- Create: `payments/src/payments/application/context.py`
- Create: `payments/src/payments/application/errors.py`
- Create: `payments/src/payments/application/ports/__init__.py`
- Create: `payments/src/payments/application/ports/clock.py`
- Create: `payments/src/payments/application/ports/catalog.py`
- Create: `payments/src/payments/application/ports/payments.py`
- Create: `payments/tests/test_application_contract.py`

- [ ] **Step 1: Write contract tests**

Create tests inspired by `shortner/tests/test_api_contract.py`:

- all application errors subclass `PaymentApplicationError`.
- public application functions are coroutine functions after Tasks 4 and 5.
- public application function signatures name dependencies explicitly.
- public application docstrings include `Args:`, `Returns:`, and `Raises:`.

- [ ] **Step 2: Implement context**

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    user_id: str | None = None
```

- [ ] **Step 3: Implement application errors**

Include at least:

- `PaymentApplicationError`
- `AuthenticationError`
- `AuthorizationError`
- `ResourceNotFoundError`
- `InvalidStateTransitionError`
- `IdempotencyConflictError`

- [ ] **Step 4: Implement split ports**

Define `Clock`, `CatalogRepository`, and `PaymentRepository` Protocols in separate modules under `application/ports/`. Re-export the common names from `application/ports/__init__.py`. Use `raise NotImplementedError` in methods, matching `shortner`.

- [ ] **Step 5: Run contract tests**

Run:

```bash
cd payments
uv run pytest tests/test_application_contract.py -v
```

Expected: initial failures for missing public use-case functions are acceptable until Tasks 4 and 5. Error hierarchy and ports should pass after this task.

- [ ] **Step 6: Commit**

```bash
git add payments/src/payments/application payments/tests/test_application_contract.py
git commit -m "feat: define payment application contracts"
```

## Task 4: Catalog Use Cases And HTTP Routes

**Files:**

- Create: `payments/src/payments/application/catalog.py`
- Create: `payments/src/payments/http/dependencies.py`
- Create: `payments/src/payments/http/errors.py`
- Create: `payments/src/payments/http/schemas/catalog.py`
- Create: `payments/src/payments/http/routes/catalog.py`
- Modify: `payments/src/payments/http/router.py`
- Create: `payments/tests/conftest.py`
- Create: `payments/tests/test_application_catalog.py`
- Create: `payments/tests/http/test_catalog_routes.py`
- Create: `payments/tests/test_api_documentation_contract.py`

- [ ] **Step 1: Write application catalog tests**

Test:

- `list_subscription_plans()` returns active plan summaries from `CatalogRepository`.
- `get_subscription_plan()` returns one plan.
- missing plan raises `ResourceNotFoundError`.

- [ ] **Step 2: Implement catalog use cases**

Export functions from `application/catalog.py`:

- `list_subscription_plans(catalog_repository: CatalogRepository) -> list[SubscriptionPlanSummary]`
- `get_subscription_plan(plan_id: str, catalog_repository: CatalogRepository) -> SubscriptionPlanSummary`

Use frozen dataclasses for application result DTOs.

- [ ] **Step 3: Write catalog route tests**

Test:

- `GET /health` returns `{"ok": True}`.
- `GET /plans` requires internal `Authorization` and `X-Request-Id`.
- `GET /plans` allows optional `X-Request-User-Id`.
- `GET /plans/{planId}` returns 404 error payload for missing plan.

- [ ] **Step 4: Implement split HTTP catalog modules**

Create:

- `HttpDependencies` and auth helper in `http/dependencies.py`.
- application error translation in `http/errors.py`.
- Pydantic plan response schemas in `http/schemas/catalog.py`.
- catalog route factory in `http/routes/catalog.py`.
- include-only root router in `http/router.py`.

- [ ] **Step 5: Write documentation route contract test**

Compare implemented route methods/paths to `docs-data/documentation.json` for:

- `plans-list`
- `plans-detail`
- `payments-orders`
- `payments-detail`

This test should fail until Task 5 adds payment routes.

- [ ] **Step 6: Run focused tests**

Run:

```bash
cd payments
uv run pytest tests/test_application_catalog.py tests/http/test_catalog_routes.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add payments/src/payments/application/catalog.py payments/src/payments/http payments/tests/conftest.py payments/tests/test_application_catalog.py payments/tests/http/test_catalog_routes.py payments/tests/test_api_documentation_contract.py
git commit -m "feat: expose documented catalog APIs"
```

## Task 5: Payment Order Use Cases And HTTP Routes

**Files:**

- Create: `payments/src/payments/application/payment_orders.py`
- Create: `payments/src/payments/http/schemas/payments.py`
- Create: `payments/src/payments/http/routes/payments.py`
- Modify: `payments/src/payments/http/router.py`
- Modify: `payments/tests/conftest.py`
- Create: `payments/tests/test_application_payment_orders.py`
- Create: `payments/tests/http/test_payment_routes.py`
- Modify: `payments/tests/test_api_documentation_contract.py`

- [ ] **Step 1: Write application payment order tests**

Test:

- missing `RequestContext.user_id` raises `AuthorizationError`.
- new order creates checkout and ready payment.
- same idempotency key and same payload returns stored response.
- same idempotency key and changed payload raises `IdempotencyConflictError`.
- retry with another user's checkout raises `ResourceNotFoundError`.
- `get_payment_for_user()` returns payment only for owner.

- [ ] **Step 2: Implement payment order use cases**

Export functions:

- `create_payment_order(...)`
- `get_payment_detail(...)`

Use `PaymentRepository`, `Clock`, request payload hash, and idempotency response snapshot. Keep temporary price calculation isolated and mark it as replacement target in a test name or code comment only if truly necessary.

- [ ] **Step 3: Add payment HTTP schemas and routes**

In `http/schemas/payments.py` and `http/routes/payments.py`, add:

- `POST /payments/orders`
- `GET /payments/{paymentId}`
- request schemas with camelCase aliases matching docs.
- response schemas with camelCase aliases.
- `Idempotency-Key` header parsing.
- include the payment router from `http/router.py`.

- [ ] **Step 4: Extend HTTP tests**

Test:

- `POST /payments/orders` requires `X-Request-User-Id`.
- successful order returns `checkoutId`, `paymentId`, `orderId`, `amount`, `status`.
- idempotent replay returns identical response.
- idempotent conflict returns 409 with `idempotency_conflict`.
- `GET /payments/{paymentId}` enforces ownership.

- [ ] **Step 5: Run focused tests**

Run:

```bash
cd payments
uv run pytest tests/test_application_payment_orders.py tests/http/test_payment_routes.py tests/test_api_documentation_contract.py -v
```

Expected: PASS.

- [ ] **Step 6: Run application contract tests**

Run:

```bash
cd payments
uv run pytest tests/test_application_contract.py -v
```

Expected: PASS. Public use-case functions should be coroutine functions with Korean docstrings containing `Args:`, `Returns:`, and `Raises:`.

- [ ] **Step 7: Commit**

```bash
git add payments/src/payments/application/payment_orders.py payments/src/payments/http payments/tests
git commit -m "feat: expose documented payment order APIs"
```

## Task 6: Mongo Adapters And Indexes

**Files:**

- Create: `payments/src/payments/adapters/mongo/__init__.py`
- Create: `payments/src/payments/adapters/mongo/documents.py`
- Create: `payments/src/payments/adapters/mongo/indexes.py`
- Create: `payments/src/payments/adapters/mongo/catalog.py`
- Create: `payments/src/payments/adapters/mongo/payments.py`
- Create: `payments/tests/test_mongo_adapters.py`

- [ ] **Step 1: Write adapter tests**

Use fake async collection objects or a lightweight adapter fake. Test:

- documented first-slice indexes are requested.
- catalog adapter filters active products and active subscription plans.
- payment adapter stores and retrieves checkout/payment ownership safely.
- idempotency lookup uses `(scope, key_hash)`.

- [ ] **Step 2: Implement document mappers**

Convert between Mongo documents and existing domain entities. Keep datetime conversion at adapter boundary.

- [ ] **Step 3: Implement `ensure_mongo_indexes(database)`**

Create indexes for:

- `products`: `product_code`, `product_type`
- `subscription_plans`: `product_id`, `plan_code`; `product_id`, `status`
- `checkouts`: `user_id`, `created_at`
- `payments`: `order_id`; `checkout_id`
- `idempotency_keys`: `scope`, `key_hash`; TTL on `expires_at`

- [ ] **Step 4: Implement catalog and payment repositories**

Keep constructors collection-specific, not database-wide:

```python
MongoCatalogRepository(products, subscription_plans)
MongoPaymentRepository(checkouts, payments, idempotency_keys)
```

- [ ] **Step 5: Run adapter tests**

Run:

```bash
cd payments
uv run pytest tests/test_mongo_adapters.py -v
```

Expected: PASS.

- [ ] **Step 6: Run full payment tests**

Run:

```bash
cd payments
uv run pytest -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add payments/src/payments/adapters/mongo payments/tests/test_mongo_adapters.py
git commit -m "feat: add Mongo adapters for first payment slice"
```

## Task 7: Local Smoke Test And README

**Files:**

- Modify: `payments/README.md`

- [ ] **Step 1: Document local env**

Add:

```markdown
# Payments FastAPI Service

## Run Locally

```bash
cd payments
PAYMENTS_DATABASE_URL=mongodb://localhost:27017 \
PAYMENTS_DATABASE_NAME=payments \
PAYMENTS_INTERNAL_SERVICE_TOKEN=dev-internal-token \
uv run fastapi dev
```

## Smoke Test

```bash
curl http://127.0.0.1:8000/health

curl http://127.0.0.1:8000/plans \
  -H 'Authorization: Bearer dev-internal-token' \
  -H 'X-Request-Id: req_local'
```
```

- [ ] **Step 2: Run full tests**

Run:

```bash
cd payments
uv run pytest -v
```

Expected: PASS.

- [ ] **Step 3: Start Mongo**

Run:

```bash
mongod --dbpath /tmp/payments-mongo
```

Expected: Mongo listens on `mongodb://localhost:27017`.

- [ ] **Step 4: Start server**

Run:

```bash
cd payments
PAYMENTS_DATABASE_URL=mongodb://localhost:27017 \
PAYMENTS_DATABASE_NAME=payments \
PAYMENTS_INTERNAL_SERVICE_TOKEN=dev-internal-token \
uv run fastapi dev
```

Expected: FastAPI development server starts at `http://127.0.0.1:8000`.

- [ ] **Step 5: Smoke test**

Run:

```bash
curl http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/plans -H 'X-Request-Id: req_local'
curl -i http://127.0.0.1:8000/plans \
  -H 'Authorization: Bearer dev-internal-token' \
  -H 'X-Request-Id: req_local'
```

Expected:

- health returns `{"ok":true}`.
- unauthenticated plans call returns 401.
- authenticated plans call returns 200 with `plans`, possibly empty until seed data exists.

- [ ] **Step 6: Commit**

```bash
git add payments/README.md
git commit -m "docs: document payment service local smoke test"
```

## Self-Review Checklist

- The plan points workers to `payment-fastapi-style`.
- The architecture is no longer split between old `api/core/infrastructure` names and the preferred `http/application/adapters` style.
- HTTP routing, HTTP schemas, HTTP dependencies, HTTP errors, and application ports are split from the first slice.
- Documentation contract tests cover all four first-slice APIs.
- Payment invariants are present in tests: internal auth, request ID, user ownership, idempotent replay, idempotent conflict, and missing resources.
- Mongo adapter guidance accounts for growth by using `adapters/mongo/` modules instead of one large file.
- Scheduler work is reserved as a separate adapter surface using `application/jobs/` plus `scheduler/`, not FastAPI background tasks.
- Follow-up APIs are explicitly out of scope for this first slice.
