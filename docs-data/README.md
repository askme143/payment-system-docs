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
  "sequences": [],
  "policies": {}
}
```

## Source Of Truth Rules

1. `apis` is the only place to define API id, method, path, category, and role.
2. `apiDetails` is the only place to define headers, cookies, body, response, failure rules, and processing logic.
3. `sequences` must reference APIs by `apiId`. Do not rewrite API contracts inside a sequence.
4. `sequencePage.status = "planned"` may omit diagrams and appear only in the sequence index.
5. `sequencePage.status = "available"` should include one or more diagrams.
6. A sequence step can reference an internal API with `apiId` or a provider call with `externalCall`.
7. HTML pages should be generated from JSON. Agents should not edit generated HTML for API contract changes.

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
  "id": "initial-subscription-success",
  "title": "신규 회원 최초 구독 성공 플로우",
  "file": "subscription-api-doc.html",
  "status": "available",
  "kind": "success",
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
- Every linked HTML file can be generated.
