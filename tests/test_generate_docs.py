import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts.generate_docs import generate_docs, render_d2_diagram


class GenerateDocsTest(unittest.TestCase):
    def test_documentation_includes_core_mongodb_collections(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        collection_ids = {collection["id"] for collection in data["database"]["collections"]}

        self.assertTrue(
            {
                "users",
                "payment-customers",
                "payment-instruments",
                "products",
                "subscription-plans",
                "one-time-skus",
                "checkouts",
                "billing-auths"
            }.issubset(collection_ids)
        )

        collections = {collection["id"]: collection for collection in data["database"]["collections"]}
        user_fields = {field["name"] for field in collections["users"]["fields"]}
        payment_customer_fields = {field["name"] for field in collections["payment-customers"]["fields"]}
        payment_customer_indexes = {tuple(index["fields"]) for index in collections["payment-customers"]["indexes"]}
        billing_method_fields = {field["name"] for field in collections["billing-methods"]["fields"]}
        payment_instrument_fields = {field["name"] for field in collections["payment-instruments"]["fields"]}
        payment_instrument_indexes = {tuple(index["fields"]) for index in collections["payment-instruments"]["indexes"]}

        self.assertNotIn("customer_key", user_fields)
        self.assertNotIn("default_billing_method_id", user_fields)
        self.assertIn("customer_key", payment_customer_fields)
        self.assertIn(("provider", "customer_key"), payment_customer_indexes)
        self.assertNotIn("billing_key", billing_method_fields)
        self.assertIn("instrument_id", billing_method_fields)
        self.assertIn("billing_key", payment_instrument_fields)
        self.assertIn(("provider", "billing_key_hash"), payment_instrument_indexes)

        snake_name = re.compile(r"^_id$|^[a-z][a-z0-9_]*$")
        for collection in data["database"]["collections"]:
            self.assertRegex(collection["name"], snake_name)
            for field in collection["fields"]:
                self.assertRegex(field["name"], snake_name)
                if "ref" in field:
                    for ref_part in field["ref"].split("."):
                        self.assertRegex(ref_part, snake_name)
            for index in collection.get("indexes", []):
                for field_name in index["fields"]:
                    self.assertRegex(field_name, snake_name)
        for relationship in data["database"]["relationships"]:
            for side in (relationship["from"], relationship["to"]):
                if " 또는 " in side:
                    continue
                for ref_part in side.split("."):
                    self.assertRegex(ref_part, snake_name)

    def test_renders_sequence_steps_as_d2_source(self):
        actors = {
            "server": {"id": "server", "label": "우리 서버", "subtitle": "API", "kind": "server"}
        }
        diagram = {
            "id": "main",
            "title": "긴 조건식",
            "actorIds": ["server"],
            "steps": [
                {
                    "type": "self",
                    "from": "server",
                    "label": "시도 소유자, 상태, 금액 재검증",
                    "code": "payment.status == ready && payment.amount == request.amount",
                    "note": "현재 paymentId/orderId만 승인하고 이전 실패 시도는 재사용하지 않음"
                }
            ]
        }

        source = render_d2_diagram(diagram, actors, {})

        self.assertIn("shape: sequence_diagram", source)
        self.assertIn('server: "우리 서버"', source)
        self.assertIn('server.style.fill: "#eaf8f3"', source)
        self.assertIn('server.note_1: "시도 소유자, 상태, 금액 재검증', source)
        self.assertNotIn("group_1: {", source)
        self.assertNotIn("server -> server:", source)
        self.assertIn("payment.status == ready && payment.amount == request.amount", source)
        self.assertIn("현재 paymentId/orderId만 승인하고 이전 실패 시도는 재사용하지 않음", source)

    def test_renders_message_details_as_notes_for_readability(self):
        actors = {
            "client": {"id": "client", "label": "웹 클라이언트", "subtitle": "Browser", "kind": "client"},
            "server": {"id": "server", "label": "우리 서버", "subtitle": "API", "kind": "server"}
        }
        apis = {
            "payments-confirm": {
                "method": "POST",
                "path": "/payments/confirm"
            }
        }
        diagram = {
            "id": "main",
            "title": "결제 승인",
            "actorIds": ["client", "server"],
            "steps": [
                {
                    "type": "message",
                    "from": "client",
                    "to": "server",
                    "label": "프론트 성공 페이지가 결제 승인 요청",
                    "apiId": "payments-confirm",
                    "note": "paymentId, paymentKey, orderId, amount, Idempotency-Key"
                }
            ]
        }

        source = render_d2_diagram(diagram, actors, apis)

        self.assertIn("client -> server: |md", source)
        self.assertIn('style.stroke: "#1c7c66"', source)
        self.assertIn('style.font-color: "#334155"', source)
        self.assertIn('style.font-size: 18', source)
        self.assertIn('style.italic: false', source)
        self.assertNotIn('style.bold: true', source)
        self.assertIn("프론트 성공 페이지가 결제 승인 요청", source)
        self.assertIn("**POST /payments/confirm**", source)
        self.assertIn("paymentId, paymentKey, orderId, amount, Idempotency-Key", source)
        self.assertNotIn("**paymentId, paymentKey, orderId, amount, Idempotency-Key**", source)
        self.assertNotIn('client -> server: "프론트 성공 페이지가 결제 승인 요청"', source)

    def test_only_uri_lines_are_bold_in_message_labels(self):
        actors = {
            "server": {"id": "server", "label": "우리 서버", "subtitle": "API", "kind": "server"},
            "client": {"id": "client", "label": "웹 클라이언트", "subtitle": "Browser", "kind": "client"}
        }
        diagram = {
            "id": "main",
            "title": "결제 완료",
            "actorIds": ["server", "client"],
            "steps": [
                {
                    "type": "message",
                    "from": "server",
                    "to": "client",
                    "label": "결제 완료 결과 반환",
                    "code": "checkoutId, paymentId, status=paid, receiptUrl",
                    "note": "프론트는 결제 완료 화면으로 이동"
                }
            ]
        }

        source = render_d2_diagram(diagram, actors, {})

        self.assertIn("checkoutId, paymentId, status=paid, receiptUrl", source)
        self.assertNotIn("**checkoutId, paymentId, status=paid, receiptUrl**", source)

    def test_generates_catalog_detail_and_sequence_pages(self):
        data = {
            "version": "1.0.0",
            "site": {
                "title": "결제 시스템",
                "pages": {
                    "sequenceIndex": {"title": "시퀀스 목록", "file": "sequence-index.html"},
                    "apiCatalog": {"title": "전체 API 목록", "file": "all-api-doc.html"},
                    "apiDetails": {"title": "API 상세", "file": "api-detail-doc.html"},
                    "database": {"title": "MongoDB 구조", "file": "database-doc.html"}
                }
            },
            "actors": [
                {"id": "client", "label": "웹 클라이언트", "subtitle": "Browser", "kind": "client"},
                {"id": "server", "label": "우리 서버", "subtitle": "API", "kind": "server"}
            ],
            "apiCategories": [
                {"id": "subscriptions", "title": "구독 API", "order": 1}
            ],
            "apis": [
                {
                    "id": "subscriptions-confirm",
                    "categoryId": "subscriptions",
                    "method": "POST",
                    "path": "/subscriptions/confirm",
                    "role": "구독을 확정합니다.",
                    "visibility": "authenticated",
                    "detailStatus": "available",
                    "detailAnchor": "confirm"
                }
            ],
            "apiDetails": {
                "subscriptions-confirm": {
                    "summary": "프론트 성공 페이지가 호출합니다.",
                    "request": {
                        "headers": [
                            {"name": "Idempotency-Key", "required": True, "description": "중복 결제를 방지합니다."}
                        ],
                        "cookies": [
                            {"name": "session", "required": True, "description": "회원 식별용 세션입니다."}
                        ],
                        "bodyFields": [
                            {"name": "subscriptionId", "required": True, "description": "구독 ID입니다."}
                        ],
                        "bodyExample": {"subscriptionId": "sub_123"}
                    },
                    "responses": [
                        {"status": 200, "description": "구독 확정 성공", "bodyExample": {"status": "active"}}
                    ],
                    "logic": ["구독 소유자를 검증합니다.", "첫 결제를 실행합니다."]
                }
            },
            "sequenceGroups": [
                {
                    "id": "subscriptions",
                    "title": "구독",
                    "order": 1,
                    "description": "구독 시작, 변경, 해지 흐름입니다."
                }
            ],
            "sequences": [
                {
                    "id": "initial-subscription-success",
                    "title": "최초 구독 성공",
                    "file": "subscription-api-doc.html",
                    "status": "available",
                    "kind": "success",
                    "groupId": "subscriptions",
                    "summary": "구독 확정 흐름입니다.",
                    "apiIds": ["subscriptions-confirm"],
                    "actorIds": ["client", "server"],
                    "diagrams": [
                        {
                            "id": "main",
                            "title": "최초 구독 성공",
                            "actorIds": ["client", "server"],
                            "relatedApiIds": ["subscriptions-confirm"],
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
                    ]
                }
            ],
            "database": {
                "engine": "MongoDB",
                "description": "결제 시스템의 주요 컬렉션과 API별 읽기/쓰기 관계입니다.",
                "collections": [
                    {
                        "id": "subscriptions",
                        "name": "subscriptions",
                        "title": "구독",
                        "description": "사용자의 구독 상태와 다음 결제 일정을 관리합니다.",
                        "fields": [
                            {
                                "name": "_id",
                                "type": "ObjectId",
                                "required": True,
                                "description": "구독 문서 ID입니다."
                            },
                            {
                                "name": "status",
                                "type": "string",
                                "required": True,
                                "enum": ["active", "cancel_scheduled", "canceled"],
                                "description": "구독 상태입니다."
                            }
                        ],
                        "indexes": [
                            {
                                "fields": ["userId", "status"],
                                "unique": False,
                                "description": "사용자의 현재 구독 조회에 사용합니다."
                            }
                        ],
                        "relatedApis": ["subscriptions-confirm"]
                    }
                ],
                "relationships": [
                    {
                        "from": "subscriptions.userId",
                        "to": "users._id",
                        "type": "reference",
                        "description": "구독 소유 사용자를 참조합니다."
                    }
                ],
                "apiAccess": [
                    {
                        "apiId": "subscriptions-confirm",
                        "reads": [],
                        "writes": ["subscriptions"],
                        "description": "구독 확정 시 구독, 결제, 인보이스 문서를 생성합니다."
                    }
                ],
                "stateModels": [
                    {
                        "id": "subscription-status",
                        "title": "구독 상태",
                        "collection": "subscriptions",
                        "field": "status",
                        "states": ["active", "cancel_scheduled", "canceled"],
                        "transitions": [
                            {
                                "from": "active",
                                "to": "cancel_scheduled",
                                "event": "사용자가 다음 결제 전 해지를 예약합니다."
                            }
                        ]
                    }
                ]
            },
            "policies": {
                "idempotency": ["같은 요청의 재시도는 기존 성공 결과를 반환합니다."],
                "httpStatus": [{"status": 409, "usage": "다른 값과 충돌할 때 사용합니다."}],
                "security": ["시크릿 키는 서버에만 저장합니다."],
                "tosspayments": ["토스는 confirm API로 직접 POST하지 않습니다."]
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_path = root / "documentation.json"
            out_dir = root / "site"
            data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            generated = generate_docs(data_path, out_dir)

            self.assertEqual(
                {
                    "sequence-index.html",
                    "all-api-doc.html",
                    "api-detail-doc.html",
                    "database-doc.html",
                    "subscription-api-doc.html"
                },
                {path.name for path in generated}
            )
            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "subscription-api-doc.html").read_text(encoding="utf-8")
            database = (out_dir / "database-doc.html").read_text(encoding="utf-8")

            self.assertIn("POST /subscriptions/confirm", catalog)
            self.assertIn("href=\"./api-detail-doc.html#confirm\"", catalog)
            index = (out_dir / "sequence-index.html").read_text(encoding="utf-8")
            self.assertIn("<h3>구독</h3>", index)
            self.assertIn("구독 시작, 변경, 해지 흐름입니다.", index)
            self.assertLess(index.index("<h3>구독</h3>"), index.index("최초 구독 성공"))
            self.assertIn("max-height: calc(100vh - 36px);", detail)
            self.assertIn("overflow-y: auto;", detail)
            self.assertIn('class="skip-link"', detail)
            self.assertIn('data-doc-search', detail)
            self.assertIn('aria-label="문서 목차"', detail)
            self.assertIn('class="table-scroll"', detail)
            self.assertIn('<div class="hero">', detail)
            self.assertIn('<div class="wrap">', detail)
            self.assertNotIn('<div class="wrap hero">', detail)
            self.assertIn("-webkit-text-size-adjust: 100%;", detail)
            self.assertIn("overflow-x: hidden;", detail)
            self.assertIn("@media (max-width: 520px)", detail)
            self.assertIn("overflow-x: auto;", detail)
            self.assertIn(".d2-svg", sequence)
            self.assertIn("width: 820px;", sequence)
            self.assertIn('id="content"', sequence)
            self.assertIn("Idempotency-Key", detail)
            self.assertIn("구독 확정 요청", sequence)
            self.assertIn("href=\"./all-api-doc.html#subscriptions-confirm\"", sequence)
            self.assertIn("diagrams/initial-subscription-success-main.d2", sequence)
            self.assertIn("shape: sequence_diagram", (out_dir / "diagrams" / "initial-subscription-success-main.d2").read_text(encoding="utf-8"))
            self.assertIn("MongoDB 구조", database)
            self.assertIn("subscriptions", database)
            self.assertIn("subscriptions.userId", database)
            self.assertIn("POST /subscriptions/confirm", database)
            self.assertIn("active → cancel_scheduled", database)

    def test_sequence_page_uses_svg_when_d2_rendering_is_enabled(self):
        data = {
            "version": "1.0.0",
            "site": {
                "title": "결제 시스템",
                "pages": {
                    "sequenceIndex": {"title": "시퀀스 목록", "file": "sequence-index.html"},
                    "apiCatalog": {"title": "전체 API 목록", "file": "all-api-doc.html"},
                    "apiDetails": {"title": "API 상세", "file": "api-detail-doc.html"}
                }
            },
            "actors": [
                {"id": "client", "label": "웹 클라이언트", "subtitle": "Browser", "kind": "client"},
                {"id": "server", "label": "우리 서버", "subtitle": "API", "kind": "server"}
            ],
            "apiCategories": [{"id": "payments", "title": "결제 API", "order": 1}],
            "apis": [
                {
                    "id": "payments-confirm",
                    "categoryId": "payments",
                    "method": "POST",
                    "path": "/payments/confirm",
                    "role": "결제를 승인합니다.",
                    "visibility": "authenticated",
                    "detailStatus": "planned",
                    "detailAnchor": "payments-confirm"
                }
            ],
            "apiDetails": {},
            "sequences": [
                {
                    "id": "payment",
                    "title": "결제",
                    "file": "payment.html",
                    "status": "available",
                    "kind": "success",
                    "summary": "결제 흐름입니다.",
                    "apiIds": ["payments-confirm"],
                    "actorIds": ["client", "server"],
                    "diagrams": [
                        {
                            "id": "main",
                            "title": "결제 승인",
                            "actorIds": ["client", "server"],
                            "relatedApiIds": ["payments-confirm"],
                            "steps": [
                                {"type": "message", "from": "client", "to": "server", "label": "승인 요청", "apiId": "payments-confirm"}
                            ]
                        }
                    ]
                }
            ],
            "policies": {"idempotency": [], "httpStatus": [], "security": [], "tosspayments": []}
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_path = root / "documentation.json"
            out_dir = root / "site"
            data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            generate_docs(data_path, out_dir, rendered_d2_ids={"payment-main"})

            sequence = (out_dir / "payment.html").read_text(encoding="utf-8")

            self.assertIn('<img class="d2-svg" src="diagrams/payment-main.svg"', sequence)

    def test_real_documentation_includes_subscription_cancel_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "subscription-cancel-sequence.html",
                {path.name for path in generated}
            )
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "subscription-cancel-sequence.html").read_text(encoding="utf-8")
            diagram = (out_dir / "diagrams" / "subscription-cancel-subscription-cancel-expiration.d2").read_text(encoding="utf-8")

            self.assertIn("POST /subscriptions/{subscriptionId}/cancel", detail)
            self.assertIn("해지 예약", detail)
            self.assertIn("기간 종료 시 해지 예약", sequence)
            self.assertIn("해지 예약 만료 처리", sequence)
            self.assertIn("cancel_scheduled", sequence)
            self.assertIn("cancel_scheduled -&gt; canceled", sequence)
            self.assertIn("currentPeriodEnd &lt;= now", sequence)
            self.assertIn("재구독", sequence)
            self.assertIn("subscription-cancel-subscription-cancel-expiration.d2", sequence)
            self.assertIn("만료 대상 구독 조회", diagram)
            self.assertIn("currentPeriodEnd <= now", diagram)
            self.assertIn("최종 종료 상태 저장", diagram)
            self.assertIn("구독 최신 상태 조회", diagram)

    def test_real_documentation_includes_cancel_related_api_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")

            self.assertIn("POST /subscriptions/{subscriptionId}/resume", detail)
            self.assertIn("해지 예약 철회", detail)
            self.assertIn("POST /internal/subscription-billing/run", detail)
            self.assertIn("cancel_scheduled", detail)
            self.assertIn("nextBillingDate가 null인 구독은 제외", detail)

    def test_real_documentation_includes_subscription_resume_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "subscription-resume-sequence.html",
                {path.name for path in generated}
            )
            sequence = (out_dir / "subscription-resume-sequence.html").read_text(encoding="utf-8")

            self.assertIn("구독 재개 플로우", sequence)
            self.assertIn("POST /subscriptions/{subscriptionId}/resume", sequence)
            self.assertIn("해지 예약 철회", sequence)
            self.assertIn("cancel_scheduled", sequence)
            self.assertIn("nextBillingDate 복구", sequence)

    def test_real_documentation_includes_subscription_plan_change_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "subscription-change-sequence.html",
                {path.name for path in generated}
            )
            sequence = (out_dir / "subscription-change-sequence.html").read_text(encoding="utf-8")

            self.assertIn("구독 플랜 변경 플로우", sequence)
            self.assertIn("PATCH /subscriptions/{subscriptionId}", sequence)
            self.assertIn("즉시 업그레이드 및 차액 결제", sequence)
            self.assertIn("다음 주기 플랜 변경 예약", sequence)
            self.assertIn("pendingPlanId", sequence)
            self.assertIn("targetPlan.productCode == subscription.productCode", sequence)

    def test_real_documentation_includes_subscription_plan_change_api_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")

            self.assertIn("href=\"./api-detail-doc.html#change\"", catalog)
            self.assertIn("PATCH /subscriptions/{subscriptionId}", detail)
            self.assertIn("targetPlanId", detail)
            self.assertIn("confirmationToken", detail)
            self.assertIn("결제일 변경 기능은 제공하지 않습니다", detail)
            self.assertIn("nextBillingDate는 직접 입력받지 않습니다", detail)
            self.assertIn("targetPlan.productCode == subscription.productCode", detail)
            self.assertIn("pendingPlanId", detail)

    def test_real_documentation_requires_server_decided_plan_change_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "subscription-change-sequence.html").read_text(encoding="utf-8")

            self.assertIn("POST /subscriptions/{subscriptionId}/change-preview", catalog)
            self.assertIn("serverDecision", detail)
            self.assertIn("서버가 업그레이드와 다운그레이드를 판정", detail)
            self.assertIn("업그레이드는 즉시 변경하고 다운그레이드는 다음 결제일에 반영", detail)
            self.assertIn("confirmationToken", detail)
            self.assertIn("즉시 결제 금액과 다음 결제일 안내를 확인", detail)
            self.assertIn("구체적인 다음 결제일을 확인", detail)
            self.assertIn("결제 성공 이메일에는 영수증 링크", detail)
            self.assertIn("유저 확인 후 플랜 변경 확정 요청", sequence)
            self.assertIn("결제 성공 이메일 발송", sequence)

    def test_real_documentation_includes_billing_method_management_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "billing-method-sequence.html",
                {path.name for path in generated}
            )
            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "billing-method-sequence.html").read_text(encoding="utf-8")

            self.assertIn("href=\"./api-detail-doc.html#billing-auth\"", catalog)
            self.assertIn("href=\"./api-detail-doc.html#billing-issue\"", catalog)
            self.assertIn("href=\"./api-detail-doc.html#billing-methods\"", catalog)
            self.assertIn("href=\"./api-detail-doc.html#billing-method-default\"", catalog)
            self.assertIn("href=\"./api-detail-doc.html#billing-method-delete\"", catalog)
            self.assertIn("POST /billing/auth", detail)
            self.assertIn("POST /billing/issue", detail)
            self.assertIn("GET /billing/methods", detail)
            self.assertIn("PATCH /billing/methods/{billingMethodId}/default", detail)
            self.assertIn("DELETE /billing/methods/{billingMethodId}", detail)
            self.assertIn("setAsDefault", detail)
            self.assertIn("POST /v1/billing/authorizations/issue", detail)
            self.assertIn("defaultBillingMethodId", detail)
            self.assertIn("last_method_for_active_subscriptions", detail)
            self.assertIn("모든 상품 구독에 공통 적용", detail)
            self.assertIn("결제수단 관리 플로우", sequence)
            self.assertIn("결제 수단 추가", sequence)
            self.assertIn("기본 결제수단 지정", sequence)
            self.assertIn("결제 수단 삭제", sequence)
            self.assertIn("모든 상품 구독에 공통 적용되는 기본 결제수단", sequence)
            self.assertIn("활성 구독이 1개 이상 있는 회원은 공통 결제수단이 최소 1개 남아야 합니다", sequence)
            self.assertIn("POST /billing/auth", sequence)
            self.assertIn("POST /billing/issue", sequence)
            self.assertIn("DELETE /billing/methods/{billingMethodId}", sequence)

    def test_real_documentation_supports_multiple_product_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            success_sequence = (out_dir / "subscription-api-doc.html").read_text(encoding="utf-8")
            recurring_sequence = (out_dir / "recurring-billing-sequence.html").read_text(encoding="utf-8")

            self.assertIn("productCode", detail)
            self.assertIn("subscriptions", detail)
            self.assertIn("같은 productCode의 활성 구독이 이미 있으면", detail)
            self.assertIn("다른 productCode의 활성 구독이 있으면 별도 상품 구독으로 허용", detail)
            self.assertIn("상품별 중복 구독 검증", success_sequence)
            self.assertIn("UNIQUE active(userId, productCode)", success_sequence)
            self.assertIn("기본 결제수단 토큰", recurring_sequence)
            self.assertIn("productCode, plan amount", recurring_sequence)

    def test_real_documentation_includes_one_time_payment_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "one-time-payment-sequence.html",
                {path.name for path in generated}
            )
            sequence_index = (out_dir / "sequence-index.html").read_text(encoding="utf-8")
            sequence = (out_dir / "one-time-payment-sequence.html").read_text(encoding="utf-8")

            self.assertIn("href=\"./one-time-payment-sequence.html\"", sequence_index)
            self.assertIn("일반결제 플로우", sequence)
            self.assertIn("POST /payments/orders", sequence)
            self.assertIn("POST /payments/confirm", sequence)
            self.assertIn("GET /payments/{paymentId}", sequence)
            self.assertIn("POST /v1/payments/confirm", sequence)
            self.assertIn("paymentKey, orderId, amount", sequence)
            self.assertIn("승인 응답과 웹훅은 같은 paymentKey 기준으로 멱등 처리", sequence)
            self.assertIn("checkoutId, paymentId, orderId, attemptNo", sequence)
            self.assertIn("이전 failed/canceled 시도는 이력으로 유지", sequence)

    def test_real_documentation_includes_one_time_payment_failure_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "one-time-payment-failure-sequence.html",
                {path.name for path in generated}
            )
            sequence_index = (out_dir / "sequence-index.html").read_text(encoding="utf-8")
            sequence = (out_dir / "one-time-payment-failure-sequence.html").read_text(encoding="utf-8")

            self.assertIn("href=\"./one-time-payment-failure-sequence.html\"", sequence_index)
            self.assertIn("일반결제 실패 및 재시도 플로우", sequence)
            self.assertIn("사용자 결제 취소", sequence)
            self.assertIn("payment.status = failed, failure.reason=user_canceled", sequence)
            self.assertIn("결제창 인증 실패 결과 기록", sequence)
            self.assertIn("failUrl?code=...&amp;message=...&amp;orderId=...", sequence)
            self.assertIn("PAYMENT_CONFIRM_VALIDATION_FAILED", sequence)
            self.assertIn("POST /v1/payments/confirm", sequence)
            self.assertIn("PAYMENT_CONFIRM_FAILED", sequence)
            self.assertIn("이미 실패 처리된 paymentKey면 상태 변경 없이 200 OK", sequence)
            self.assertIn("실패 후 다른 결제수단으로 재시도", sequence)
            self.assertIn("new paymentId, new orderId", sequence)
            self.assertIn("결제창 실패 결과 미보고 및 만료", sequence)
            self.assertIn("auth_result_not_reported", sequence)

    def test_real_documentation_includes_one_time_payment_api_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")

            self.assertIn("href=\"./api-detail-doc.html#payments-orders\"", catalog)
            self.assertIn("href=\"./api-detail-doc.html#payments-confirm\"", catalog)
            self.assertIn("href=\"./api-detail-doc.html#payments-detail\"", catalog)
            self.assertIn("POST /payments/orders", detail)
            self.assertIn("POST /payments/confirm", detail)
            self.assertIn("GET /payments/{paymentId}", detail)
            self.assertIn("POST /payments/{paymentId}/auth-result", detail)
            self.assertIn("프론트가 일반결제 결제창을 열기 전에", detail)
            self.assertIn("토스페이먼츠 일반결제 승인 API POST /v1/payments/confirm", detail)
            self.assertIn("PAYMENT_CONFIRM_FAILED", detail)
            self.assertIn("checkoutId", detail)
            self.assertIn("failure.phase", detail)
            self.assertIn("user_canceled", detail)
            self.assertIn("auth_failed", detail)
            self.assertIn("auth_result_not_reported", detail)
            self.assertIn("create_new_payment_attempt", detail)
            self.assertIn("토스 시크릿 키, 카드 전체 번호", detail)

    def test_real_documentation_includes_payment_cancel_refund_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "payment-cancel-refund-sequence.html",
                {path.name for path in generated}
            )
            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "payment-cancel-refund-sequence.html").read_text(encoding="utf-8")

            self.assertIn("href=\"./api-detail-doc.html#payments-cancel\"", catalog)
            self.assertIn("href=\"./api-detail-doc.html#admin-payment-cancel\"", catalog)
            self.assertIn("POST /payments/{paymentId}/cancel", detail)
            self.assertIn("POST /admin/payments/{paymentId}/cancel", detail)
            self.assertIn("cancelAmount", detail)
            self.assertIn("POST /v1/payments/{paymentKey}/cancel", detail)
            self.assertIn("전체 취소 및 부분 취소", sequence)
            self.assertIn("부분 취소 누적 금액 검증", sequence)
            self.assertIn("payment.status = canceled 또는 partial_canceled", sequence)
            self.assertIn("운영자 결제 취소", sequence)
            self.assertIn("cancelHistory", sequence)

    def test_real_documentation_includes_invoice_history_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "invoice-history-sequence.html",
                {path.name for path in generated}
            )
            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "invoice-history-sequence.html").read_text(encoding="utf-8")
            diagram = (out_dir / "diagrams" / "invoice-history-billing-history-main.d2").read_text(encoding="utf-8")

            self.assertIn("href=\"./api-detail-doc.html#invoices-list\"", catalog)
            self.assertIn("GET /invoices", detail)
            self.assertIn("status, subscriptionId, from, to, cursor, limit", detail)
            self.assertIn("receiptAvailable", detail)
            self.assertIn("failureSummary", detail)
            self.assertIn("상세 API에서 영수증 URL과 실패 사유를 확인", detail)
            self.assertIn("마이페이지 청구 내역 조회", sequence)
            self.assertIn("GET /invoices", sequence)
            self.assertIn("GET /invoices/{invoiceId}", sequence)
            self.assertIn("영수증 링크 노출", sequence)
            self.assertIn("실패 사유와 재시도 안내 노출", sequence)
            self.assertIn("GET /invoices", diagram)
            self.assertIn("GET /invoices/{invoiceId}", diagram)

    def test_real_documentation_includes_admin_product_management_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "admin-product-management-sequence.html",
                {path.name for path in generated}
            )
            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "admin-product-management-sequence.html").read_text(encoding="utf-8")

            self.assertIn("POST /admin/products", catalog)
            self.assertIn("POST /admin/products/{productId}/subscription-plans", catalog)
            self.assertIn("PATCH /admin/products/{productId}/subscription-plans/{planId}", detail)
            self.assertIn("POST /admin/products/{productId}/one-time-skus", catalog)
            self.assertIn("PATCH /admin/products/{productId}/one-time-skus/{skuId}", detail)
            self.assertIn("PATCH /admin/products/{productId}/status", detail)
            self.assertIn("공통 Product 아래에서 구독상품과 일반상품을 분리", detail)
            self.assertIn("productType", detail)
            self.assertIn("subscriptionPlans", detail)
            self.assertIn("oneTimeSkus", detail)
            self.assertIn("changeReason", detail)
            self.assertIn("effectiveFor", detail)
            self.assertIn("MISSING_ACTIVE_SELLING_UNIT", detail)
            self.assertIn("구독상품 생성 및 플랜 구성", sequence)
            self.assertIn("일반상품 생성 및 SKU 구성", sequence)
            self.assertIn("구독 플랜은 기존 활성 구독의 과거 가격을 덮어쓰지 않습니다", sequence)
            self.assertIn("일반상품 SKU는 주문 생성 시점에 가격 스냅샷으로 고정합니다", sequence)

    def test_real_documentation_includes_admin_subscription_adjustment_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "admin-subscription-adjustment-sequence.html",
                {path.name for path in generated}
            )
            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "admin-subscription-adjustment-sequence.html").read_text(encoding="utf-8")
            diagram = (out_dir / "diagrams" / "admin-subscription-adjustment-next-billing-postpone.d2").read_text(encoding="utf-8")

            self.assertIn("href=\"./api-detail-doc.html#admin-subscription-adjust\"", catalog)
            self.assertIn("POST /admin/subscriptions/{subscriptionId}/adjust", detail)
            self.assertIn("provider_payment_sync", detail)
            self.assertIn("postpone_next_billing", detail)
            self.assertIn("set_next_billing_date", detail)
            self.assertIn("postponeBy.days", detail)
            self.assertIn("nextBillingAt", detail)
            self.assertIn("운영자 구독 수동 보정", sequence)
            self.assertIn("토스 결제 성공 누락 보정", sequence)
            self.assertIn("다음 결제일 연기", sequence)
            self.assertIn("정책 예외 상태 보정", sequence)
            self.assertIn("current nextBillingAt + postponeBy.days", diagram)

    def test_real_documentation_includes_subscription_final_failure_cancel_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            self.assertIn(
                "subscription-final-failure-sequence.html",
                {path.name for path in generated}
            )
            sequence_index = (out_dir / "sequence-index.html").read_text(encoding="utf-8")
            sequence = (out_dir / "subscription-final-failure-sequence.html").read_text(encoding="utf-8")
            diagram = (out_dir / "diagrams" / "subscription-final-failure-final-failure-cancel.d2").read_text(encoding="utf-8")

            self.assertIn("href=\"./subscription-final-failure-sequence.html\"", sequence_index)
            self.assertIn("구독 결제 최종 실패 후 구독 종료 플로우", sequence)
            self.assertIn("최종 실패 후 즉시 구독 종료", sequence)
            self.assertIn("subscription=canceled", sequence)
            self.assertIn("nextBillingDate=null", sequence)
            self.assertIn("다시 이용하려면 새 구독을 시작", sequence)
            self.assertNotIn("graceEndsAt", sequence)
            self.assertNotIn("serviceAccess=limited", sequence)
            self.assertNotIn("복구 조건", sequence)
            self.assertIn("template=subscription_canceled_payment_failed", diagram)
            self.assertIn("status=canceled, cancelReason=payment_final_failed", diagram)


if __name__ == "__main__":
    unittest.main()
