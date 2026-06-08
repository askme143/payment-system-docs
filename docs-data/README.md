# Payment Documentation Data Schema

This directory defines the JSON structure that should become the source of truth for the payment documentation.

The goal is to keep generated HTML stable while letting multiple agents edit data safely.

## Files

- `schema/documentation.schema.json`: JSON Schema for the full documentation data model.

## Document Model

Use one root JSON document with these top-level keys:

```json
{
  "version": "1.0.0",
  "site": {},
  "actors": [],
  "apiCategories": [],
  "apis": [],
  "apiDetails": {},
  "sequenceGroups": [],
  "sequences": [],
  "database": {},
  "policies": {}
}
```

## Source Of Truth Rules

1. `apis` is the only place to define API id, method, path, category, and role.
2. `apiDetails` is the only place to define headers, cookies, body, response, failure rules, and processing logic.
3. `sequenceGroups` defines the topic groups shown in the sequence index.
4. `sequences` must reference a topic group by `groupId` and APIs by `apiId`. Do not rewrite API contracts inside a sequence.
5. `sequencePage.status = "planned"` may omit diagrams and appear only in the sequence index.
6. `sequencePage.status = "available"` should include one or more diagrams.
7. A sequence step can reference an internal API with `apiId` or a provider call with `externalCall`.
8. `database.collections` defines MongoDB collections, fields, indexes, and related APIs.
9. `database.apiAccess` maps APIs to MongoDB read/write collections.
10. HTML pages should be generated from JSON. Agents should not edit generated HTML for API contract changes.

## ID Rules

IDs use lowercase kebab case:

```text
subscriptions-confirm
webhooks-toss-payments
initial-subscription-success
```

Use the same API id everywhere:

```json
{
  "apiIds": ["subscriptions-confirm"],
  "steps": [
    {
      "type": "message",
      "from": "client",
      "to": "server",
      "label": "구독 확정 요청",
      "apiId": "subscriptions-confirm"
    }
  ]
}
```

## Minimal API Entry

```json
{
  "id": "subscriptions-confirm",
  "categoryId": "subscriptions",
  "method": "POST",
  "path": "/subscriptions/confirm",
  "role": "프론트 성공 페이지가 호출하며, 빌링키 발급과 첫 결제로 구독을 활성화합니다.",
  "visibility": "authenticated",
  "detailStatus": "available",
  "detailAnchor": "confirm"
}
```

## Minimal Sequence Page

```json
{
  "id": "subscriptions",
  "title": "구독",
  "order": 1,
  "description": "구독 시작, 변경, 해지와 재개 흐름입니다."
}
```

## Minimal MongoDB Collection

```json
{
  "id": "subscriptions",
  "name": "subscriptions",
  "title": "구독",
  "description": "사용자의 구독 상태와 다음 결제 일정을 관리합니다.",
  "fields": [
    {
      "name": "userId",
      "type": "ObjectId",
      "required": true,
      "ref": "users._id",
      "description": "구독 소유 사용자입니다."
    },
    {
      "name": "status",
      "type": "string",
      "required": true,
      "enum": ["pending", "active", "past_due", "cancel_scheduled", "canceled"],
      "description": "구독의 현재 상태입니다."
    }
  ],
  "indexes": [
    {
      "fields": ["userId", "status"],
      "description": "사용자의 활성/예약 구독 조회에 사용합니다."
    }
  ],
  "relatedApis": ["subscriptions-confirm", "subscriptions-cancel"]
}
```

```json
{
  "id": "initial-subscription-success",
  "title": "신규 회원 최초 구독 성공 플로우",
  "file": "subscription-api-doc.html",
  "status": "available",
  "kind": "success",
  "groupId": "subscriptions",
  "summary": "체크아웃 생성, 빌링 인증, 빌링키 발급, 첫 결제 승인 흐름입니다.",
  "apiIds": [
    "subscriptions-checkout",
    "subscriptions-confirm",
    "webhooks-toss-payments"
  ],
  "actorIds": ["client", "server", "toss"],
  "diagrams": [
    {
      "id": "initial-subscription-success-main",
      "title": "신규 회원 최초 구독 성공",
      "actorIds": ["client", "server", "toss"],
      "relatedApiIds": [
        "subscriptions-checkout",
        "subscriptions-confirm",
        "webhooks-toss-payments"
      ],
      "steps": [
        {
          "type": "message",
          "from": "client",
          "to": "server",
          "label": "구독 체크아웃 생성 요청",
          "apiId": "subscriptions-checkout",
          "note": "planId, successUrl, failUrl"
        }
      ]
    }
  ]
}
```

## Validation Intent

The schema enforces structure, but it cannot enforce every cross-reference by itself. A future generator or validation script should also check:

- Every `api.categoryId` exists in `apiCategories`.
- Every `api.detailAnchor` exists in the generated API detail page when `detailStatus` is `available`.
- Every sequence `apiId` exists in `apis`.
- Every sequence `actorId`, `from`, and `to` exists in `actors`.
- Every database `relatedApis` and `apiAccess.apiId` exists in `apis`.
- Every database `apiAccess` collection id exists in `database.collections`.
- Every linked HTML file can be generated.
