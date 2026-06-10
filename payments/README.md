# Payments FastAPI Service

## Run Locally

```bash
cd payments
uv sync --dev
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

## Tests

```bash
cd payments
uv run pytest -v
uv run pyright
uv run ruff check .
```

The API contract source of truth is `../docs-data/documentation.json`.

Implemented first-slice APIs:

- `GET /plans`
- `GET /plans/{planId}`
- `POST /payments/orders`
- `GET /payments/{paymentId}`
