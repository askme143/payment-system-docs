# Payment DB Safety Invariants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make payment safety requirements executable by documenting MongoDB uniqueness, partial indexes, TTL indexes, idempotency storage, operation locks, and operator audit collections in the source JSON, schema, generated HTML, and tests.

**Architecture:** Keep `docs-data/documentation.json` as the source of truth. Extend the JSON schema only where the current model cannot express MongoDB index behavior, then update generated database docs and tests so future agents cannot silently remove these safeguards.

**Tech Stack:** Python static generator, JSON source data, JSON Schema, MongoDB index semantics, unittest.

---

## File Structure

- Modify: `docs-data/schema/documentation.schema.json`
  - Add MongoDB index metadata fields: `name`, `sparse`, `partialFilterExpression`, `expireAfterSeconds`.
- Modify: `docs-data/documentation.json`
  - Add explicit safety indexes to `payments`, `subscriptions`, `billing-methods`, `invoices`, `payment-instruments`, and `webhook-events`.
  - Add new collections: `idempotency-keys`, `operation-locks`, `operator-audits`.
  - Add `apiAccess` mappings and relationships for the new collections.
- Modify: `scripts/generate_docs.py`
  - Render index name, sparse flag, partial filter, and TTL seconds in `database-doc.html`.
  - Validate index fields and collection references used by API access.
- Modify: `tests/test_generate_docs.py`
  - Add regression tests for critical partial/unique/TTL indexes.
  - Add regression tests that generated HTML exposes partial filters and TTL metadata.
- Generated: root HTML files from `scripts/generate_docs.py`
  - Regenerate after JSON/schema/generator changes.

## Acceptance Criteria

- `payments.payment_key` is sparse unique.
- `payments.checkout_id + status=paid` prevents two paid attempts for one checkout.
- `subscriptions.user_id + product_code` prevents multiple service-holding subscriptions for the same product.
- `billing_methods.user_id + is_default=true + status=active` prevents two active defaults.
- `invoices.subscription_id + billing_cycle_key` prevents duplicate invoices for one subscription billing cycle.
- `idempotency_keys` has a unique request key and a TTL retention policy.
- `operation_locks` has a unique lock key, lock expiry, and fencing token.
- `operator_audits` has immutable operational trace fields for admin corrections and cancellations.
- `python3 scripts/generate_docs.py --data docs-data/documentation.json --out .` succeeds.
- `python3 -m unittest tests/test_generate_docs.py` succeeds.

---

### Task 1: Extend Schema For MongoDB Index Semantics

**Files:**
- Modify: `docs-data/schema/documentation.schema.json`
- Test: `tests/test_generate_docs.py`

- [ ] **Step 1: Write the failing schema coverage test**

Add this test to `GenerateDocsTest`:

```python
def test_schema_allows_mongodb_index_safety_metadata(self):
    schema = json.loads(Path("docs-data/schema/documentation.schema.json").read_text(encoding="utf-8"))
    index_properties = schema["$defs"]["dbIndex"]["properties"]

    self.assertIn("name", index_properties)
    self.assertIn("sparse", index_properties)
    self.assertIn("partialFilterExpression", index_properties)
    self.assertIn("expireAfterSeconds", index_properties)
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_schema_allows_mongodb_index_safety_metadata`

Expected: FAIL because `name`, `sparse`, `partialFilterExpression`, and `expireAfterSeconds` are not schema properties yet.

- [ ] **Step 3: Extend `dbIndex` schema**

Update `docs-data/schema/documentation.schema.json` in `$defs.dbIndex.properties`:

```json
"name": {
  "type": "string",
  "description": "MongoDB index name used by migrations and operational dashboards."
},
"sparse": {
  "type": "boolean",
  "description": "Whether MongoDB should omit documents that do not contain the indexed field."
},
"partialFilterExpression": {
  "$ref": "#/$defs/jsonValue",
  "description": "MongoDB partial index filter expression."
},
"expireAfterSeconds": {
  "type": "integer",
  "minimum": 0,
  "description": "TTL duration in seconds for date-based expiry indexes."
}
```

- [ ] **Step 4: Run the schema coverage test**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_schema_allows_mongodb_index_safety_metadata`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs-data/schema/documentation.schema.json tests/test_generate_docs.py
git commit -m "docs: allow mongodb safety index metadata"
```

---

### Task 2: Add Critical Payment And Subscription Indexes

**Files:**
- Modify: `docs-data/documentation.json`
- Test: `tests/test_generate_docs.py`

- [ ] **Step 1: Write failing tests for critical indexes**

Add helpers and tests to `GenerateDocsTest`:

```python
def _collection_by_id(self, data, collection_id):
    return next(collection for collection in data["database"]["collections"] if collection["id"] == collection_id)

def _index_by_name(self, collection, index_name):
    return next(index for index in collection["indexes"] if index.get("name") == index_name)

def test_payment_safety_indexes_are_documented(self):
    data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
    payments = self._collection_by_id(data, "payments")

    payment_key = self._index_by_name(payments, "uniq_payments_payment_key_sparse")
    self.assertEqual(payment_key["fields"], ["payment_key"])
    self.assertTrue(payment_key["unique"])
    self.assertTrue(payment_key["sparse"])

    paid_checkout = self._index_by_name(payments, "uniq_payments_paid_checkout")
    self.assertEqual(paid_checkout["fields"], ["checkout_id"])
    self.assertTrue(paid_checkout["unique"])
    self.assertEqual(paid_checkout["partialFilterExpression"], {"checkout_id": {"$exists": True}, "status": "paid"})

def test_subscription_and_billing_safety_indexes_are_documented(self):
    data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
    subscriptions = self._collection_by_id(data, "subscriptions")
    billing_methods = self._collection_by_id(data, "billing-methods")
    invoices = self._collection_by_id(data, "invoices")

    active_subscription = self._index_by_name(subscriptions, "uniq_subscriptions_user_product_service_holding")
    self.assertEqual(active_subscription["fields"], ["user_id", "product_code"])
    self.assertTrue(active_subscription["unique"])
    self.assertEqual(active_subscription["partialFilterExpression"], {"status": {"$in": ["pending", "active", "past_due", "cancel_scheduled"]}})

    default_method = self._index_by_name(billing_methods, "uniq_billing_methods_active_default")
    self.assertEqual(default_method["fields"], ["user_id", "is_default"])
    self.assertTrue(default_method["unique"])
    self.assertEqual(default_method["partialFilterExpression"], {"is_default": True, "status": "active"})

    billing_cycle = self._index_by_name(invoices, "uniq_invoices_subscription_billing_cycle")
    self.assertEqual(billing_cycle["fields"], ["subscription_id", "billing_cycle_key"])
    self.assertTrue(billing_cycle["unique"])
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_payment_safety_indexes_are_documented tests.test_generate_docs.GenerateDocsTest.test_subscription_and_billing_safety_indexes_are_documented`

Expected: FAIL because the named indexes do not exist yet.

- [ ] **Step 3: Update `payments` fields and indexes**

In `docs-data/documentation.json`, add these fields to `database.collections[id=payments].fields`:

```json
{
  "name": "billing_cycle_key",
  "type": "string",
  "required": false,
  "description": "구독 정기결제 또는 재시도에서 같은 청구 회차를 식별하는 키입니다. 예: sub_123:2026-07."
},
{
  "name": "cancelable_amount",
  "type": "number",
  "required": false,
  "description": "부분 취소 검증에 사용하는 남은 취소 가능 금액입니다."
}
```

Add these indexes to `database.collections[id=payments].indexes`:

```json
{
  "name": "uniq_payments_payment_key_sparse",
  "fields": ["payment_key"],
  "unique": true,
  "sparse": true,
  "description": "토스 paymentKey가 있는 결제는 전 시스템에서 한 번만 내부 결제와 연결됩니다."
},
{
  "name": "uniq_payments_paid_checkout",
  "fields": ["checkout_id"],
  "unique": true,
  "partialFilterExpression": {
    "checkout_id": { "$exists": true },
    "status": "paid"
  },
  "description": "하나의 일반결제 구매 의도 아래 paid 결제가 2건 이상 생기지 않게 합니다."
},
{
  "name": "uniq_payments_subscription_billing_cycle_paid",
  "fields": ["subscription_id", "billing_cycle_key"],
  "unique": true,
  "partialFilterExpression": {
    "subscription_id": { "$exists": true },
    "billing_cycle_key": { "$exists": true },
    "status": "paid"
  },
  "description": "같은 구독 청구 회차가 중복 과금으로 paid 처리되지 않게 합니다."
}
```

- [ ] **Step 4: Update `subscriptions` fields and indexes**

Add these fields to `database.collections[id=subscriptions].fields`:

```json
{
  "name": "product_code",
  "type": "string",
  "required": true,
  "description": "상품별 활성 구독 중복을 막기 위해 구독 생성 시점의 상품 코드를 스냅샷으로 저장합니다."
},
{
  "name": "current_period_start_at",
  "type": "Date",
  "required": false,
  "description": "현재 이용 기간 시작 시각입니다."
},
{
  "name": "current_period_end_at",
  "type": "Date",
  "required": false,
  "description": "현재 이용 기간 종료 시각이며 해지 예약과 권한 유지 기준입니다."
}
```

Add this index to `database.collections[id=subscriptions].indexes`:

```json
{
  "name": "uniq_subscriptions_user_product_service_holding",
  "fields": ["user_id", "product_code"],
  "unique": true,
  "partialFilterExpression": {
    "status": { "$in": ["pending", "active", "past_due", "cancel_scheduled"] }
  },
  "description": "회원이 같은 상품에 대해 결제 대기, 활성, 연체, 해지 예약 구독을 동시에 둘 이상 가질 수 없게 합니다."
}
```

- [ ] **Step 5: Update `billing-methods` fields and indexes**

Change `database.collections[id=billing-methods].fields[name=status].enum` to:

```json
["active", "inactive", "deleted"]
```

Add this index:

```json
{
  "name": "uniq_billing_methods_active_default",
  "fields": ["user_id", "is_default"],
  "unique": true,
  "partialFilterExpression": {
    "is_default": true,
    "status": "active"
  },
  "description": "회원별 활성 기본 결제수단이 2개 이상 생기지 않게 합니다."
}
```

- [ ] **Step 6: Update `invoices` fields and indexes**

Add these fields to `database.collections[id=invoices].fields`:

```json
{
  "name": "subscription_id",
  "type": "ObjectId",
  "required": false,
  "ref": "subscriptions._id",
  "description": "구독 결제로 발행된 인보이스인 경우 원본 구독을 참조합니다."
},
{
  "name": "billing_cycle_key",
  "type": "string",
  "required": false,
  "description": "구독 청구 회차를 식별하는 키입니다. 정기결제와 재시도 중복 인보이스 생성을 막습니다."
}
```

Add this index:

```json
{
  "name": "uniq_invoices_subscription_billing_cycle",
  "fields": ["subscription_id", "billing_cycle_key"],
  "unique": true,
  "partialFilterExpression": {
    "subscription_id": { "$exists": true },
    "billing_cycle_key": { "$exists": true },
    "status": { "$in": ["issued", "paid"] }
  },
  "description": "같은 구독 청구 회차에 대해 유효 인보이스가 2개 이상 생기지 않게 합니다."
}
```

- [ ] **Step 7: Run the critical index tests**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_payment_safety_indexes_are_documented tests.test_generate_docs.GenerateDocsTest.test_subscription_and_billing_safety_indexes_are_documented`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add docs-data/documentation.json tests/test_generate_docs.py
git commit -m "docs: document critical payment db invariants"
```

---

### Task 3: Add Idempotency, Lock, And Audit Collections

**Files:**
- Modify: `docs-data/documentation.json`
- Test: `tests/test_generate_docs.py`

- [ ] **Step 1: Write failing tests for operational safety collections**

Add this test:

```python
def test_operational_safety_collections_are_documented(self):
    data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
    collection_ids = {collection["id"] for collection in data["database"]["collections"]}
    self.assertTrue({"idempotency-keys", "operation-locks", "operator-audits"}.issubset(collection_ids))

    idempotency = self._collection_by_id(data, "idempotency-keys")
    idempotency_unique = self._index_by_name(idempotency, "uniq_idempotency_keys_scope_key")
    idempotency_ttl = self._index_by_name(idempotency, "ttl_idempotency_keys_expires_at")
    self.assertEqual(idempotency_unique["fields"], ["scope", "key_hash"])
    self.assertTrue(idempotency_unique["unique"])
    self.assertEqual(idempotency_ttl["fields"], ["expires_at"])
    self.assertEqual(idempotency_ttl["expireAfterSeconds"], 0)

    locks = self._collection_by_id(data, "operation-locks")
    lock_unique = self._index_by_name(locks, "uniq_operation_locks_lock_key")
    lock_ttl = self._index_by_name(locks, "ttl_operation_locks_locked_until_at")
    self.assertEqual(lock_unique["fields"], ["lock_key"])
    self.assertTrue(lock_unique["unique"])
    self.assertEqual(lock_ttl["fields"], ["locked_until_at"])
    self.assertEqual(lock_ttl["expireAfterSeconds"], 0)

    audits = self._collection_by_id(data, "operator-audits")
    audit_fields = {field["name"] for field in audits["fields"]}
    self.assertTrue({"operator_id", "action", "target_type", "target_id", "previous_state", "next_state", "result", "created_at"}.issubset(audit_fields))
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_operational_safety_collections_are_documented`

Expected: FAIL because the collections do not exist yet.

- [ ] **Step 3: Add `idempotency-keys` collection**

Append this collection to `database.collections`:

```json
{
  "id": "idempotency-keys",
  "name": "idempotency_keys",
  "title": "멱등키 저장소",
  "description": "외부 결제 호출 전후의 요청 해시와 결과를 저장해 같은 요청은 같은 결과를 반환하고 다른 요청은 충돌로 차단합니다.",
  "fields": [
    { "name": "_id", "type": "ObjectId", "required": true, "description": "멱등키 문서 ID입니다." },
    { "name": "scope", "type": "string", "required": true, "description": "API 또는 작업 범위입니다. 예: payments-confirm, subscriptions-confirm, admin-payment-cancel." },
    { "name": "key_hash", "type": "string", "required": true, "description": "평문 Idempotency-Key를 저장하지 않기 위한 해시입니다." },
    { "name": "request_hash", "type": "string", "required": true, "description": "같은 키로 다른 요청 바디가 들어오는지 판정하는 정규화 요청 해시입니다." },
    { "name": "status", "type": "string", "required": true, "enum": ["processing", "succeeded", "failed", "conflicted"], "description": "멱등 처리 상태입니다." },
    { "name": "resource_type", "type": "string", "required": false, "description": "멱등 결과가 연결된 리소스 유형입니다. 예: payment, subscription, invoice." },
    { "name": "resource_id", "type": "string", "required": false, "description": "멱등 결과가 연결된 내부 리소스 ID입니다." },
    { "name": "response_status", "type": "number", "required": false, "description": "재시도에 반환할 HTTP 상태 코드입니다." },
    { "name": "response_body", "type": "object", "required": false, "description": "재시도에 반환할 응답 요약입니다. 민감정보와 PG 원문 전체는 저장하지 않습니다." },
    { "name": "locked_until_at", "type": "Date", "required": false, "description": "처리 중 요청의 임시 점유 만료 시각입니다." },
    { "name": "created_at", "type": "Date", "required": true, "description": "최초 요청 시각입니다." },
    { "name": "updated_at", "type": "Date", "required": true, "description": "마지막 상태 갱신 시각입니다." },
    { "name": "expires_at", "type": "Date", "required": true, "description": "멱등 결과 보존 만료 시각입니다. 결제 승인 계열은 최소 30일 이상 유지합니다." }
  ],
  "indexes": [
    {
      "name": "uniq_idempotency_keys_scope_key",
      "fields": ["scope", "key_hash"],
      "unique": true,
      "description": "같은 범위 안에서 같은 멱등키는 하나의 요청 기록만 가질 수 있습니다."
    },
    {
      "name": "idx_idempotency_keys_resource",
      "fields": ["resource_type", "resource_id"],
      "description": "리소스 기준으로 멱등 처리 이력을 조회합니다."
    },
    {
      "name": "ttl_idempotency_keys_expires_at",
      "fields": ["expires_at"],
      "expireAfterSeconds": 0,
      "description": "보존 기간이 지난 멱등키를 자동 정리합니다."
    }
  ],
  "relatedApis": ["payments-orders", "payments-confirm", "subscriptions-checkout", "subscriptions-confirm", "subscriptions-cancel", "subscriptions-resume", "subscriptions-change", "billing-issue", "billing-method-default", "billing-method-delete", "internal-billing-retry", "payments-cancel", "admin-payment-cancel", "admin-subscription-adjust"],
  "riskIds": ["idempotency-store-missing", "payment-confirm-concurrent-approval", "subscription-retry-duplicate-charge", "cancel-refund-duplicate-or-mismatch", "plan-change-double-apply"]
}
```

- [ ] **Step 4: Add `operation-locks` collection**

Append this collection:

```json
{
  "id": "operation-locks",
  "name": "operation_locks",
  "title": "운영 락",
  "description": "정기결제 배치, 인보이스 재시도, 구독 상태 보정처럼 동시에 실행되면 안 되는 작업의 점유 상태를 저장합니다.",
  "fields": [
    { "name": "_id", "type": "ObjectId", "required": true, "description": "락 문서 ID입니다." },
    { "name": "lock_key", "type": "string", "required": true, "description": "점유 대상 키입니다. 예: billing-run:2026-07-08, invoice-retry:inv_123." },
    { "name": "owner_token", "type": "string", "required": true, "description": "락 소유 실행자가 갱신과 해제에 사용하는 난수 토큰입니다." },
    { "name": "fencing_token", "type": "number", "required": true, "description": "늦게 도착한 이전 실행자의 쓰기를 차단하기 위한 단조 증가 토큰입니다." },
    { "name": "status", "type": "string", "required": true, "enum": ["active", "released", "expired"], "description": "락 상태입니다." },
    { "name": "locked_until_at", "type": "Date", "required": true, "description": "락 만료 시각입니다." },
    { "name": "acquired_at", "type": "Date", "required": true, "description": "락 획득 시각입니다." },
    { "name": "released_at", "type": "Date", "required": false, "description": "정상 해제 시각입니다." },
    { "name": "metadata", "type": "object", "required": false, "description": "작업 유형, 배치일, 실행자 ID 등 운영 추적용 메타데이터입니다." }
  ],
  "indexes": [
    {
      "name": "uniq_operation_locks_lock_key",
      "fields": ["lock_key"],
      "unique": true,
      "description": "동일 작업 키에 대해 활성 실행자가 둘 이상 생기지 않게 합니다."
    },
    {
      "name": "idx_operation_locks_status_until",
      "fields": ["status", "locked_until_at"],
      "description": "만료된 활성 락을 찾아 회수하거나 운영 알림을 발생시킵니다."
    },
    {
      "name": "ttl_operation_locks_locked_until_at",
      "fields": ["locked_until_at"],
      "expireAfterSeconds": 0,
      "description": "만료된 락 문서를 자동 정리합니다. 운영 감사가 필요한 실행 결과는 operator_audits에 남깁니다."
    }
  ],
  "relatedApis": ["internal-billing-run", "internal-billing-retry", "subscriptions-change", "subscriptions-cancel", "admin-subscription-adjust"],
  "riskIds": ["recurring-billing-duplicate-run", "subscription-retry-duplicate-charge", "cancel-scheduled-billing-race", "plan-change-double-apply"]
}
```

- [ ] **Step 5: Add `operator-audits` collection**

Append this collection:

```json
{
  "id": "operator-audits",
  "name": "operator_audits",
  "title": "운영자 감사 로그",
  "description": "관리자 결제 취소, 구독 보정, 외부 결제 동기화 같은 수동 개입의 전후 상태와 근거를 불변 로그로 저장합니다.",
  "fields": [
    { "name": "_id", "type": "ObjectId", "required": true, "description": "감사 로그 문서 ID입니다." },
    { "name": "operator_id", "type": "ObjectId", "required": true, "ref": "users._id", "description": "작업을 수행한 관리자 사용자 ID입니다." },
    { "name": "action", "type": "string", "required": true, "description": "수행한 운영 작업입니다. 예: admin_payment_cancel, provider_payment_sync, status_override." },
    { "name": "target_type", "type": "string", "required": true, "description": "대상 리소스 유형입니다. 예: payment, subscription, invoice." },
    { "name": "target_id", "type": "string", "required": true, "description": "대상 리소스 ID입니다." },
    { "name": "idempotency_key_id", "type": "ObjectId", "required": false, "ref": "idempotency_keys._id", "description": "멱등 처리와 연결된 경우 해당 멱등키 문서입니다." },
    { "name": "previous_state", "type": "object", "required": true, "description": "변경 전 주요 상태 스냅샷입니다." },
    { "name": "next_state", "type": "object", "required": true, "description": "변경 후 주요 상태 스냅샷입니다." },
    { "name": "reason_code", "type": "string", "required": true, "description": "운영자가 선택한 표준 사유 코드입니다." },
    { "name": "reason_message", "type": "string", "required": false, "description": "운영자가 입력한 상세 사유입니다." },
    { "name": "request_ip", "type": "string", "required": false, "description": "관리자 요청 IP입니다." },
    { "name": "result", "type": "string", "required": true, "enum": ["succeeded", "failed", "rejected"], "description": "운영 작업 결과입니다." },
    { "name": "created_at", "type": "Date", "required": true, "description": "감사 로그 생성 시각입니다." }
  ],
  "indexes": [
    {
      "name": "idx_operator_audits_target",
      "fields": ["target_type", "target_id", "created_at"],
      "description": "결제, 구독, 인보이스별 운영 개입 이력을 조회합니다."
    },
    {
      "name": "idx_operator_audits_operator",
      "fields": ["operator_id", "created_at"],
      "description": "관리자별 작업 이력을 조회합니다."
    },
    {
      "name": "idx_operator_audits_action",
      "fields": ["action", "created_at"],
      "description": "작업 유형별 감사 이력을 조회합니다."
    }
  ],
  "relatedApis": ["admin-payment-cancel", "admin-subscription-adjust", "payments-cancel"],
  "riskIds": ["provider-paid-internal-unpaid", "cancel-refund-duplicate-or-mismatch", "idempotency-store-missing"]
}
```

- [ ] **Step 6: Run the collection test**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_operational_safety_collections_are_documented`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add docs-data/documentation.json tests/test_generate_docs.py
git commit -m "docs: add idempotency lock and audit collections"
```

---

### Task 4: Wire API Access And Relationships To New Collections

**Files:**
- Modify: `docs-data/documentation.json`
- Test: `tests/test_generate_docs.py`

- [ ] **Step 1: Write failing tests for API access mappings**

Add this test:

```python
def test_payment_safety_api_access_is_documented(self):
    data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
    access_by_api = {item["apiId"]: item for item in data["database"]["apiAccess"]}

    payments_confirm = access_by_api["payments-confirm"]
    self.assertIn("idempotency-keys", payments_confirm["reads"])
    self.assertIn("idempotency-keys", payments_confirm["writes"])
    self.assertIn("payments", payments_confirm["writes"])

    billing_run = access_by_api["internal-billing-run"]
    self.assertIn("operation-locks", billing_run["reads"])
    self.assertIn("operation-locks", billing_run["writes"])

    admin_adjust = access_by_api["admin-subscription-adjust"]
    self.assertIn("operator-audits", admin_adjust["writes"])
    self.assertIn("idempotency-keys", admin_adjust["writes"])
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_payment_safety_api_access_is_documented`

Expected: FAIL because the new collections are not wired into API access yet.

- [ ] **Step 3: Update API access mappings**

Update these existing `database.apiAccess` entries:

```json
{
  "apiId": "payments-confirm",
  "reads": ["payments", "checkouts", "idempotency-keys"],
  "writes": ["payments", "invoices", "idempotency-keys"],
  "description": "일반결제 승인 결과를 멱등 저장소와 함께 결제와 인보이스에 반영합니다."
}
```

```json
{
  "apiId": "subscriptions-confirm",
  "reads": ["subscriptions", "payment-customers", "idempotency-keys"],
  "writes": ["subscriptions", "payments", "invoices", "payment-instruments", "billing-methods", "idempotency-keys"],
  "description": "구독 확정, 빌링키 저장, 첫 결제 결과를 멱등 저장소와 함께 반영합니다."
}
```

```json
{
  "apiId": "internal-billing-run",
  "reads": ["subscriptions", "billing-methods", "payment-instruments", "operation-locks"],
  "writes": ["operation-locks", "payments", "invoices", "subscriptions"],
  "description": "배치 실행 락을 획득한 뒤 정기결제 대상 구독을 조회하고 결제 성공/실패 결과를 저장합니다."
}
```

```json
{
  "apiId": "internal-billing-retry",
  "reads": ["invoices", "payments", "subscriptions", "payment-instruments", "idempotency-keys", "operation-locks"],
  "writes": ["idempotency-keys", "operation-locks", "payments", "invoices", "subscriptions"],
  "description": "인보이스 회차 락과 멱등키를 사용해 실패 결제를 한 번만 재시도합니다."
}
```

```json
{
  "apiId": "admin-subscription-adjust",
  "reads": ["subscriptions", "payments", "invoices", "idempotency-keys"],
  "writes": ["subscriptions", "payments", "invoices", "idempotency-keys", "operator-audits"],
  "description": "운영자 보정 전후 상태를 감사 로그에 저장하고 멱등키로 중복 보정을 방지합니다."
}
```

Add missing `apiAccess` entries for `admin-payment-cancel`, `payments-cancel`, `billing-method-default`, and `billing-method-delete`:

```json
{
  "apiId": "admin-payment-cancel",
  "reads": ["payments", "idempotency-keys"],
  "writes": ["payments", "idempotency-keys", "operator-audits"],
  "description": "관리자 결제 취소를 멱등 처리하고 운영자 감사 로그를 저장합니다."
}
```

```json
{
  "apiId": "payments-cancel",
  "reads": ["payments", "idempotency-keys"],
  "writes": ["payments", "idempotency-keys", "operator-audits"],
  "description": "회원 결제 취소 요청을 멱등 처리하고 취소 이력을 감사 가능한 형태로 저장합니다."
}
```

```json
{
  "apiId": "billing-method-default",
  "reads": ["billing-methods", "payment-instruments", "idempotency-keys"],
  "writes": ["billing-methods", "idempotency-keys"],
  "description": "회원별 기본 결제수단 변경을 멱등 처리하고 unique partial index로 기본값 경합을 차단합니다."
}
```

```json
{
  "apiId": "billing-method-delete",
  "reads": ["billing-methods", "payment-instruments", "subscriptions", "idempotency-keys"],
  "writes": ["billing-methods", "payment-instruments", "idempotency-keys"],
  "description": "결제수단 삭제를 멱등 처리하고 활성 구독의 마지막 결제수단 삭제를 차단합니다."
}
```

- [ ] **Step 4: Add relationships**

Append relationships:

```json
{
  "from": "operator_audits.idempotency_key_id",
  "to": "idempotency_keys._id",
  "type": "reference",
  "description": "운영자 작업은 멱등 처리 결과와 연결될 수 있습니다."
}
```

```json
{
  "from": "invoices.subscription_id",
  "to": "subscriptions._id",
  "type": "reference",
  "description": "구독 인보이스는 원본 구독을 참조합니다."
}
```

- [ ] **Step 5: Run API access tests**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_payment_safety_api_access_is_documented`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs-data/documentation.json tests/test_generate_docs.py
git commit -m "docs: wire payment safety collections to api access"
```

---

### Task 5: Render Safety Metadata In Database Documentation

**Files:**
- Modify: `scripts/generate_docs.py`
- Test: `tests/test_generate_docs.py`
- Generated: `database-doc.html`

- [ ] **Step 1: Write failing generated HTML test**

Add this test:

```python
def test_database_docs_render_partial_sparse_and_ttl_indexes(self):
    data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmpdir:
        generate_docs(data, Path(tmpdir), render_d2=False)
        html = Path(tmpdir, data["site"]["pages"]["database"]["file"]).read_text(encoding="utf-8")

    self.assertIn("uniq_payments_payment_key_sparse", html)
    self.assertIn("sparse", html)
    self.assertIn("partialFilterExpression", html)
    self.assertIn("checkout_id", html)
    self.assertIn("ttl_idempotency_keys_expires_at", html)
    self.assertIn("expireAfterSeconds", html)
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_database_docs_render_partial_sparse_and_ttl_indexes`

Expected: FAIL because generated index tables currently render only fields, unique flag, and description.

- [ ] **Step 3: Update index table rendering**

In `scripts/generate_docs.py`, update the index table in `render_database_page` so each index renders:

```python
index_rows = "".join(
    "<tr>"
    f"<td><code>{e(index.get('name', '-'))}</code></td>"
    f"<td><code>{e(', '.join(index['fields']))}</code></td>"
    f"<td>{'예' if index.get('unique') else '아니오'}</td>"
    f"<td>{'예' if index.get('sparse') else '아니오'}</td>"
    f"<td>{e(json.dumps(index.get('partialFilterExpression', '-'), ensure_ascii=False))}</td>"
    f"<td>{e(str(index.get('expireAfterSeconds', '-')))}</td>"
    f"<td>{e(index.get('description', '-'))}</td>"
    "</tr>"
    for index in collection.get("indexes", [])
)
```

Use this table header:

```html
<thead><tr><th>이름</th><th>필드</th><th>유니크</th><th>Sparse</th><th>Partial Filter</th><th>TTL Seconds</th><th>설명</th></tr></thead>
```

- [ ] **Step 4: Run the generated HTML test**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_database_docs_render_partial_sparse_and_ttl_indexes`

Expected: PASS.

- [ ] **Step 5: Regenerate docs**

Run: `python3 scripts/generate_docs.py --data docs-data/documentation.json --out .`

Expected: command exits with status 0 and updates `database-doc.html`.

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_docs.py tests/test_generate_docs.py database-doc.html
git commit -m "docs: render mongodb safety index metadata"
```

---

### Task 6: Strengthen Validation Against Documentation Drift

**Files:**
- Modify: `scripts/generate_docs.py`
- Test: `tests/test_generate_docs.py`

- [ ] **Step 1: Write failing validation tests**

Add these tests:

```python
def test_validate_data_rejects_index_fields_missing_from_collection(self):
    data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
    payments = self._collection_by_id(data, "payments")
    payments["indexes"].append({
        "name": "idx_bad_missing_field",
        "fields": ["missing_field"],
        "description": "이 테스트는 존재하지 않는 필드를 거부해야 합니다."
    })

    with self.assertRaisesRegex(ValueError, "references missing field missing_field"):
        generate_docs(data, Path(tempfile.mkdtemp()), render_d2=False)

def test_validate_data_rejects_api_access_missing_collection(self):
    data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
    data["database"]["apiAccess"].append({
        "apiId": "payments-confirm",
        "reads": ["not-a-collection"],
        "writes": [],
        "description": "이 테스트는 존재하지 않는 컬렉션 참조를 거부해야 합니다."
    })

    with self.assertRaisesRegex(ValueError, "references missing collection not-a-collection"):
        generate_docs(data, Path(tempfile.mkdtemp()), render_d2=False)
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_validate_data_rejects_index_fields_missing_from_collection tests.test_generate_docs.GenerateDocsTest.test_validate_data_rejects_api_access_missing_collection`

Expected: FAIL because validation does not check index fields or all API access collection references yet.

- [ ] **Step 3: Add validation for index fields**

In `validate_data(data)`, inside the database collection loop, add:

```python
field_names = {field["name"] for field in collection["fields"]}
for index in collection.get("indexes", []):
    for field_name in index["fields"]:
        if field_name not in field_names:
            raise ValueError(f"Collection {collection['id']} index {index.get('name', ','.join(index['fields']))} references missing field {field_name}")
```

- [ ] **Step 4: Add validation for API access collection references**

In the existing `for access in data["database"].get("apiAccess", [])` block, make sure both reads and writes are checked:

```python
for collection_id in access.get("reads", []) + access.get("writes", []):
    if collection_id not in collection_ids:
        raise ValueError(f"API access {access['apiId']} references missing collection {collection_id}")
```

- [ ] **Step 5: Run validation tests**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_validate_data_rejects_index_fields_missing_from_collection tests.test_generate_docs.GenerateDocsTest.test_validate_data_rejects_api_access_missing_collection`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_docs.py tests/test_generate_docs.py
git commit -m "test: prevent payment database documentation drift"
```

---

### Task 7: Full Verification

**Files:**
- Generated: root HTML files

- [ ] **Step 1: Regenerate documentation**

Run: `python3 scripts/generate_docs.py --data docs-data/documentation.json --out .`

Expected: exits with status 0.

- [ ] **Step 2: Run all tests**

Run: `python3 -m unittest tests/test_generate_docs.py`

Expected: all tests pass.

- [ ] **Step 3: Inspect generated DB doc for safety sections**

Run: `rg -n "uniq_payments_payment_key_sparse|uniq_payments_paid_checkout|uniq_subscriptions_user_product_service_holding|idempotency_keys|operation_locks|operator_audits|expireAfterSeconds|partialFilterExpression" database-doc.html`

Expected: each pattern appears at least once.

- [ ] **Step 4: Check generated docs are the only intended output changes**

Run: `git status --short`

Expected: modified source files, tests, and generated HTML pages touched by the generator; no unrelated deletions or binary changes.

- [ ] **Step 5: Commit final generated artifacts**

```bash
git add docs-data/documentation.json docs-data/schema/documentation.schema.json scripts/generate_docs.py tests/test_generate_docs.py database-doc.html all-api-doc.html api-detail-doc.html sequence-index.html index.html subscription-api-doc.html
git commit -m "docs: finalize payment database safety invariants"
```

---

## Self-Review

- Spec coverage: The plan covers DB uniqueness, partial indexes, sparse indexes, idempotency storage, lock storage, audit logging, API access mappings, generated docs, and regression tests.
- Placeholder scan: No deferred placeholders remain; each task names exact files, expected fields, index names, commands, and expected outcomes.
- Type consistency: Collection IDs use existing kebab-case IDs, MongoDB collection names use snake_case, and index names are stable across JSON, tests, and generated HTML.
