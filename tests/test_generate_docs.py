import json
import tempfile
import unittest
from pathlib import Path

from scripts.generate_docs import generate_docs, render_diagram


class GenerateDocsTest(unittest.TestCase):
    def test_wraps_long_svg_text_inside_sequence_boxes(self):
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

        svg = render_diagram(diagram, actors, {})

        self.assertIn("<desc>payment.status == ready &amp;&amp; payment.amount == request.amount</desc>", svg)
        self.assertIn("<tspan x=\"370\" dy=\"0\">payment.status == ready &amp;&amp;</tspan>", svg)
        self.assertIn("<tspan x=\"370\" dy=\"20\">payment.amount == request.amount</tspan>", svg)
        self.assertIn("height=\"145\"", svg)

    def test_generates_catalog_detail_and_sequence_pages(self):
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
            "sequences": [
                {
                    "id": "initial-subscription-success",
                    "title": "최초 구독 성공",
                    "file": "subscription-api-doc.html",
                    "status": "available",
                    "kind": "success",
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
                    "subscription-api-doc.html"
                },
                {path.name for path in generated}
            )
            catalog = (out_dir / "all-api-doc.html").read_text(encoding="utf-8")
            detail = (out_dir / "api-detail-doc.html").read_text(encoding="utf-8")
            sequence = (out_dir / "subscription-api-doc.html").read_text(encoding="utf-8")

            self.assertIn("POST /subscriptions/confirm", catalog)
            self.assertIn("href=\"./api-detail-doc.html#confirm\"", catalog)
            self.assertIn("Idempotency-Key", detail)
            self.assertIn("구독 확정 요청", sequence)
            self.assertIn("href=\"./all-api-doc.html#subscriptions-confirm\"", sequence)

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

            self.assertIn("POST /subscriptions/{subscriptionId}/cancel", detail)
            self.assertIn("해지 예약", detail)
            self.assertIn("기간 종료 시 해지 예약", sequence)
            self.assertIn("cancel_scheduled", sequence)

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
            sequence = (out_dir / "billing-method-sequence.html").read_text(encoding="utf-8")

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
            self.assertIn("공통 기본 billingKey", recurring_sequence)
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


if __name__ == "__main__":
    unittest.main()
