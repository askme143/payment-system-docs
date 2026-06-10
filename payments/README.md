# Payments FastAPI Service

## Run Locally

```bash
cd payments
uv sync --dev
PAYMENTS_DATABASE_URL='mongodb://localhost:27117/?replicaSet=rs0' \
PAYMENTS_DATABASE_NAME=payments \
PAYMENTS_INTERNAL_SERVICE_TOKEN=dev-internal-token \
uv run fastapi dev
```

## Local MongoDB

Local transactions require MongoDB to run as a replica set. Start a single-node
development replica set from this `payments` directory:

```bash
docker compose up -d payments-mongo payments-mongo-rs-init
```

The default host port is `27117` to avoid common conflicts with local MongoDB
instances on `27017`. Override it when needed:

```bash
PAYMENTS_MONGO_PORT=27118 docker compose up -d payments-mongo payments-mongo-rs-init
```

If you change `PAYMENTS_MONGO_PORT` after the replica set has already been
initialized, recreate the local Mongo volume so the advertised replica-set host
matches the new port:

```bash
docker compose down -v
PAYMENTS_MONGO_PORT=27118 docker compose up -d payments-mongo payments-mongo-rs-init
```

Use the same port in `PAYMENTS_DATABASE_URL`:

```bash
PAYMENTS_DATABASE_URL='mongodb://localhost:27117/?replicaSet=rs0'
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
