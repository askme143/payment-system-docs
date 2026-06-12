import ast
import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts.generate_docs import generate_docs, render_d2_diagram


class GenerateDocsTest(unittest.TestCase):
    def _collection_by_id(self, data, collection_id):
        return next(collection for collection in data["database"]["collections"] if collection["id"] == collection_id)

    def _index_by_name(self, collection, index_name):
        return next(index for index in collection["indexes"] if index.get("name") == index_name)

    def _entity_fields(self, filename):
        source = Path("payments/src/payments/domain/entities", filename).read_text(encoding="utf-8")
        tree = ast.parse(source)
        fields = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                    continue
                annotation = ast.unparse(statement.annotation)
                has_none_default = isinstance(statement.value, ast.Constant) and statement.value.value is None
                fields[statement.target.id] = {
                    "annotation": annotation,
                    "optional": "None" in annotation or "Optional[" in annotation or has_none_default,
                }
        return fields

    def test_actor_theme_schema_covers_documented_themes(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        schema = json.loads(Path("docs-data/schema/documentation.schema.json").read_text(encoding="utf-8"))

        documented_themes = {actor["theme"] for actor in data["actors"] if "theme" in actor}
        schema_themes = set(schema["$defs"]["actor"]["properties"]["theme"]["enum"])

        self.assertTrue(documented_themes.issubset(schema_themes))

    def test_schema_root_requires_documented_top_level_models(self):
        schema = json.loads(Path("docs-data/schema/documentation.schema.json").read_text(encoding="utf-8"))

        for key in [
            "version",
            "site",
            "actors",
            "apiCategories",
            "apis",
            "apiDetails",
            "sequenceGroups",
            "sequences",
            "database",
            "systemArchitecture",
            "policies",
        ]:
            self.assertIn(key, schema["required"])

    def test_system_architecture_page_is_generated(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        self.assertIn("systemArchitecture", data["site"]["pages"])
        self.assertIn("systemArchitecture", data)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generated = generate_docs("docs-data/documentation.json", out_dir)

            generated_names = {path.name for path in generated}
            self.assertIn("system-architecture-doc.html", generated_names)
            architecture = (out_dir / "system-architecture-doc.html").read_text(encoding="utf-8")
            sequence_index = (out_dir / "sequence-index.html").read_text(encoding="utf-8")

            for expected in [
                "notification_outbox",
                "notification_templates",
                "Notification Worker",
                "retry_scheduled",
                "dead_letter",
                "SMTP",
            ]:
                self.assertIn(expected, architecture)
            self.assertIn("이메일 발송 시스템 아키텍처 문서", architecture)
            self.assertIn("이메일 발송 시스템 아키텍처 문서", sequence_index)
            self.assertNotIn("결제 시스템 아키텍처 문서", architecture)

    def test_system_architecture_d2_files_are_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            diagram_dir = out_dir / "diagrams"
            outbox_d2 = diagram_dir / "system-architecture-email-notification-outbox.d2"
            lifecycle_d2 = diagram_dir / "system-architecture-email-delivery-lifecycle.d2"

            self.assertTrue(outbox_d2.exists())
            self.assertTrue(lifecycle_d2.exists())
            self.assertIn("notification_outbox", outbox_d2.read_text(encoding="utf-8"))
            self.assertIn("dead_letter", lifecycle_d2.read_text(encoding="utf-8"))

    def test_system_architecture_diagrams_do_not_render_tooltip_appendix(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            outbox_d2 = (
                out_dir
                / "diagrams"
                / "system-architecture-email-notification-outbox.d2"
            ).read_text(encoding="utf-8")
            lifecycle_d2 = (
                out_dir
                / "diagrams"
                / "system-architecture-email-delivery-lifecycle.d2"
            ).read_text(encoding="utf-8")

            self.assertNotIn("tooltip:", outbox_d2)
            self.assertNotIn("tooltip:", lifecycle_d2)

    def test_schema_allows_mongodb_index_safety_metadata(self):
        schema = json.loads(Path("docs-data/schema/documentation.schema.json").read_text(encoding="utf-8"))
        index_properties = schema["$defs"]["dbIndex"]["properties"]

        self.assertIn("name", index_properties)
        self.assertIn("sparse", index_properties)
        self.assertIn("partialFilterExpression", index_properties)
        self.assertIn("expireAfterSeconds", index_properties)

    def test_schema_allows_nested_mongodb_field_contracts(self):
        schema = json.loads(Path("docs-data/schema/documentation.schema.json").read_text(encoding="utf-8"))
        field_properties = schema["$defs"]["dbField"]["properties"]

        self.assertIn("properties", field_properties)
        self.assertIn("items", field_properties)
        self.assertEqual(field_properties["properties"]["items"]["$ref"], "#/$defs/dbField")

    def test_payment_safety_indexes_are_documented(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        payments = self._collection_by_id(data, "payments")

        payment_key = self._index_by_name(payments, "uniq_payments_payment_key_sparse")
        self.assertEqual(payment_key["fields"], ["payment_key"])
        self.assertTrue(payment_key["unique"])
        self.assertTrue(payment_key["sparse"])
        payment_key_field = next(field for field in payments["fields"] if field["name"] == "payment_key")
        payment_key_contract = payment_key_field["description"] + " " + payment_key.get("description", "")
        self.assertIn("생략", payment_key_contract)
        self.assertIn("null", payment_key_contract)

        paid_checkout = self._index_by_name(payments, "uniq_payments_paid_checkout")
        self.assertEqual(paid_checkout["fields"], ["checkout_id"])
        self.assertTrue(paid_checkout["unique"])
        self.assertEqual(paid_checkout["partialFilterExpression"], {"checkout_id": {"$type": "string"}, "status": "paid"})

        paid_billing_cycle = self._index_by_name(payments, "uniq_payments_subscription_billing_cycle_paid")
        self.assertEqual(paid_billing_cycle["fields"], ["subscription_id", "billing_cycle_key"])
        self.assertTrue(paid_billing_cycle["unique"])
        self.assertEqual(
            paid_billing_cycle["partialFilterExpression"],
            {"subscription_id": {"$type": "string"}, "billing_cycle_key": {"$type": "string"}, "status": "paid"}
        )

    def test_subscription_and_billing_safety_indexes_are_documented(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        subscriptions = self._collection_by_id(data, "subscriptions")
        billing_methods = self._collection_by_id(data, "billing-methods")
        invoices = self._collection_by_id(data, "invoices")

        active_subscription = self._index_by_name(subscriptions, "uniq_subscriptions_user_product_service_holding")
        self.assertEqual(active_subscription["fields"], ["user_id", "product_code"])
        self.assertTrue(active_subscription["unique"])
        self.assertEqual(active_subscription["partialFilterExpression"], {"status": {"$in": ["pending", "active", "past_due", "cancel_scheduled"]}})
        subscription_fields = {field["name"] for field in subscriptions["fields"]}
        self.assertNotIn("billing_method_id", subscription_fields)

        default_method = self._index_by_name(billing_methods, "uniq_billing_methods_active_default")
        self.assertEqual(default_method["fields"], ["user_id", "is_default"])
        self.assertTrue(default_method["unique"])
        self.assertEqual(default_method["partialFilterExpression"], {"is_default": True, "status": "active"})

        billing_cycle = self._index_by_name(invoices, "uniq_invoices_subscription_billing_cycle")
        self.assertEqual(billing_cycle["fields"], ["subscription_id", "billing_cycle_key"])
        self.assertTrue(billing_cycle["unique"])
        self.assertEqual(
            billing_cycle["partialFilterExpression"],
            {
                "subscription_id": {"$type": "string"},
                "billing_cycle_key": {"$type": "string"},
                "status": {"$in": ["issued", "paid"]}
            }
        )

    def test_operational_safety_collections_are_documented(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        collection_ids = {collection["id"] for collection in data["database"]["collections"]}
        self.assertTrue({"idempotency-keys", "operation-locks", "operator-audits"}.issubset(collection_ids))

        idempotency = self._collection_by_id(data, "idempotency-keys")
        idempotency_fields = {field["name"] for field in idempotency["fields"]}
        idempotency_unique = self._index_by_name(idempotency, "uniq_idempotency_keys_scope_key")
        idempotency_ttl = self._index_by_name(idempotency, "ttl_idempotency_keys_expires_at")
        self.assertIn("request_hash", idempotency_fields)
        self.assertEqual(idempotency_unique["fields"], ["scope", "key_hash"])
        self.assertTrue(idempotency_unique["unique"])
        self.assertEqual(idempotency_ttl["fields"], ["expires_at"])
        self.assertEqual(idempotency_ttl["expireAfterSeconds"], 0)

        locks = self._collection_by_id(data, "operation-locks")
        lock_fields = {field["name"]: field for field in locks["fields"]}
        lock_unique = self._index_by_name(locks, "uniq_operation_locks_lock_key")
        lock_ttl = self._index_by_name(locks, "ttl_operation_locks_locked_until_at")
        self.assertIn("fencing_token", lock_fields)
        self.assertIn("fencing_counter_key", lock_fields)
        self.assertEqual(lock_unique["fields"], ["lock_key"])
        self.assertTrue(lock_unique["unique"])
        self.assertEqual(lock_ttl["fields"], ["locked_until_at"])
        self.assertEqual(lock_ttl["expireAfterSeconds"], 0)
        lock_fencing_contract = " ".join(
            [
                locks["description"],
                lock_fields["fencing_token"]["description"],
                lock_fields["fencing_counter_key"]["description"]
            ]
        )
        self.assertIn("단조", lock_fencing_contract)
        self.assertIn("TTL", lock_fencing_contract)
        self.assertIn("durable", lock_fencing_contract)

        audits = self._collection_by_id(data, "operator-audits")
        audit_fields = {field["name"] for field in audits["fields"]}
        self.assertTrue({"operator_id", "action", "target_type", "target_id", "previous_state", "next_state", "result", "created_at"}.issubset(audit_fields))
        self.assertTrue({"idempotency_scope", "idempotency_key_hash", "idempotency_request_hash"}.issubset(audit_fields))

    def test_database_docs_render_partial_sparse_and_ttl_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            database = (out_dir / "database-doc.html").read_text(encoding="utf-8")

            self.assertIn("uniq_payments_payment_key_sparse", database)
            self.assertIn('data-label="Sparse">예</td>', database)
            self.assertIn("partialFilterExpression", database)
            self.assertIn("uniq_payments_paid_checkout", database)
            self.assertIn("$type", database)
            self.assertIn("ttl_idempotency_keys_expires_at", database)
            self.assertIn("expireAfterSeconds", database)

    def test_validate_data_rejects_index_fields_missing_from_collection(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        payments = self._collection_by_id(data, "payments")
        payments["indexes"].append({
            "name": "idx_bad_missing_field",
            "fields": ["missing_field"],
            "description": "이 테스트는 존재하지 않는 필드를 거부해야 합니다."
        })

        with self.assertRaisesRegex(ValueError, "references missing field missing_field"):
            with tempfile.TemporaryDirectory() as tmp:
                generate_docs(data, Path(tmp), render_d2=False)

    def test_payment_safety_api_access_is_documented(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        api_ids = [item["apiId"] for item in data["database"]["apiAccess"]]
        self.assertEqual(len(api_ids), len(set(api_ids)))
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

        existing_api_ids = {api["id"] for api in data["apis"]}
        for safety_collection_id in ["idempotency-keys", "operation-locks", "operator-audits"]:
            collection = self._collection_by_id(data, safety_collection_id)
            for api_id in collection["relatedApis"]:
                if api_id not in existing_api_ids:
                    continue
                self.assertIn(api_id, access_by_api)
                self.assertIn(
                    safety_collection_id,
                    access_by_api[api_id]["reads"] + access_by_api[api_id]["writes"]
                )

    def test_sequence_api_ids_cover_diagram_related_apis(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))

        for sequence in data["sequences"]:
            sequence_api_ids = set(sequence.get("apiIds", []))
            sequence_body = {key: value for key, value in sequence.items() if key != "apiIds"}
            serialized_sequence_body = json.dumps(sequence_body, ensure_ascii=False)
            for diagram in sequence.get("diagrams", []):
                related_api_ids = set(diagram.get("relatedApiIds", []))
                self.assertTrue(
                    related_api_ids.issubset(sequence_api_ids),
                    f"{sequence['id']}/{diagram['id']} has related APIs outside sequence.apiIds",
                )

            for api_id in sequence_api_ids:
                self.assertIn(
                    api_id,
                    serialized_sequence_body,
                    f"{sequence['id']} lists {api_id} without mentioning why it is related",
                )

    def test_every_documented_api_has_database_access_mapping(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        api_detail_ids = set(data["apiDetails"])
        api_access_ids = {item["apiId"] for item in data["database"]["apiAccess"]}
        collection_ids = {collection["id"] for collection in data["database"]["collections"]}

        self.assertEqual(api_detail_ids, api_access_ids)

        for access in data["database"]["apiAccess"]:
            self.assertIsInstance(access["reads"], list)
            self.assertIsInstance(access["writes"], list)
            for collection_id in access["reads"] + access["writes"]:
                self.assertIn(collection_id, collection_ids, access["apiId"])

        relationships = {(item["from"], item["to"]) for item in data["database"]["relationships"]}
        self.assertIn(("operator_audits.idempotency_key_id", "idempotency_keys._id"), relationships)
        self.assertNotIn(("operator_audits.operator_id", "users._id"), relationships)
        self.assertIn(("invoices.subscription_id", "subscriptions._id"), relationships)
        self.assertNotIn(("subscriptions.billing_method_id", "billing_methods._id"), relationships)

    def test_browser_session_cookies_do_not_cross_payment_system_boundary(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        api_visibility = {api["id"]: api.get("visibility") for api in data["apis"]}

        for api_id, detail in data["apiDetails"].items():
            visibility = api_visibility[api_id]
            request = detail["request"]
            cookie_names = {cookie["name"] for cookie in request.get("cookies", [])}
            header_names = {header["name"] for header in request.get("headers", [])}

            self.assertFalse(
                cookie_names & {"session", "SESSION", "ADMIN_SESSION", "csrf_token"},
                f"{api_id} must not expose browser session/csrf cookies to the payment system",
            )

            if visibility in {"public", "authenticated", "admin", "admin-auth"}:
                self.assertEqual(request.get("cookies", []), [], api_id)
                self.assertIn("X-Request-Id", header_names, api_id)
            if visibility in {"public", "authenticated", "admin"}:
                self.assertIn("Authorization", header_names, api_id)
            if visibility == "authenticated":
                self.assertIn("X-Request-User-Id", header_names, api_id)

        serialized = json.dumps(data["apiDetails"], ensure_ascii=False)
        self.assertNotIn("세션 쿠키", serialized)
        self.assertNotIn("쿠키 기반 인증", serialized)
        self.assertNotIn("csrf_token", serialized)
        self.assertNotIn("프론트 성공 페이지가 호출", serialized)
        self.assertNotIn("프론트 실패 페이지가 호출", serialized)

    def test_public_visibility_keeps_backend_service_auth_boundary(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        public_api_ids = {api["id"] for api in data["apis"] if api.get("visibility") == "public"}

        self.assertEqual({"plans-list", "plans-detail"}, public_api_ids)

        for api_id in public_api_ids:
            detail = data["apiDetails"][api_id]
            headers = {header["name"]: header for header in detail["request"]["headers"]}
            notes = " ".join(detail.get("notes", []))

            self.assertTrue(headers["Authorization"]["required"], api_id)
            self.assertIn("내부 서비스 토큰", headers["Authorization"]["description"])
            self.assertFalse(headers["X-Request-User-Id"]["required"], api_id)
            self.assertEqual([], detail["request"].get("cookies", []))
            self.assertIn("최종 사용자 인증", notes)
            self.assertIn("백엔드 서버", notes)

    def test_webhook_invoice_reconciliation_access_is_mapped(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        webhook_detail = data["apiDetails"]["webhooks-toss-payments"]
        webhook_access = next(access for access in data["database"]["apiAccess"] if access["apiId"] == "webhooks-toss-payments")
        webhook_contract = json.dumps(webhook_detail, ensure_ascii=False)

        self.assertIn("인보이스", webhook_contract)
        self.assertIn("invoices", webhook_access["reads"])
        self.assertIn("invoices", webhook_access["writes"])
        self.assertIn("인보이스", webhook_access["description"])

    def test_payment_docs_do_not_own_user_collection(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        collection_ids = {collection["id"] for collection in data["database"]["collections"]}

        self.assertNotIn("users", collection_ids)

        for collection in data["database"]["collections"]:
            for field in collection["fields"]:
                self.assertNotEqual(field.get("ref"), "users._id")
                if field["name"] in {"user_id", "operator_id"}:
                    self.assertEqual(field["type"], "ExternalUserId")
                    self.assertIn("외부", field["description"])

        for relationship in data["database"]["relationships"]:
            self.assertNotEqual(relationship["to"], "users._id")

        for access in data["database"]["apiAccess"]:
            self.assertNotIn("users", access["reads"])
            self.assertNotIn("users", access["writes"])

    def test_product_status_uses_paused_not_suspended(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        products = self._collection_by_id(data, "products")
        product_status = next(field for field in products["fields"] if field["name"] == "status")

        self.assertEqual(product_status["enum"], ["draft", "active", "paused", "archived"])

        product_source = Path("payments/src/payments/domain/entities/product.py").read_text(encoding="utf-8")
        self.assertIn('Literal["draft", "active", "paused", "archived"]', product_source)
        self.assertNotIn("suspended", product_source)

        product_api_text = json.dumps(
            {
                "admin-products-create": data["apiDetails"]["admin-products-create"],
                "admin-products-status": data["apiDetails"]["admin-products-status"],
            },
            ensure_ascii=False,
        )
        self.assertIn("paused", product_api_text)
        self.assertNotIn("suspended", product_api_text)

        product_sequence_text = json.dumps(
            next(sequence for sequence in data["sequences"] if sequence["id"] == "admin-product-management"),
            ensure_ascii=False,
        )
        self.assertIn("paused", product_sequence_text)
        self.assertNotIn("suspended", product_sequence_text)

    def test_subscription_status_contract_has_no_pending_billing_auth_or_suspended(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        subscriptions = self._collection_by_id(data, "subscriptions")
        subscription_status = next(field for field in subscriptions["fields"] if field["name"] == "status")

        allowed_statuses = ["pending", "active", "past_due", "cancel_scheduled", "canceled"]
        self.assertEqual(subscription_status["enum"], allowed_statuses)

        subscription_source = Path("payments/src/payments/domain/entities/subscription.py").read_text(encoding="utf-8")
        self.assertIn('Literal["pending", "active", "past_due", "cancel_scheduled", "canceled"]', subscription_source)
        self.assertNotIn("pending_billing_auth", subscription_source)
        self.assertNotIn("suspended", subscription_source)

        serialized_data = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("pending_billing_auth", serialized_data)

        scoped_api_details = {
            api_id: detail
            for api_id, detail in data["apiDetails"].items()
            if api_id.startswith("admin-") or api_id.startswith("subscriptions-change")
        }
        scoped_api_text = json.dumps(scoped_api_details, ensure_ascii=False)
        self.assertNotIn("suspended", scoped_api_text)

    def test_mongodb_ids_are_application_uuid_strings(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        serialized_database = json.dumps(data["database"], ensure_ascii=False)

        self.assertNotIn("ObjectId", serialized_database)
        self.assertNotIn("objectId", serialized_database)

        for collection in data["database"]["collections"]:
            id_field = next(field for field in collection["fields"] if field["name"] == "_id")
            self.assertEqual(id_field["type"], "UuidString")
            self.assertIn("UUID", id_field["description"])
            self.assertIn("애플리케이션", id_field["description"])
            self.assertIn("_id", id_field["description"])

            for field in collection["fields"]:
                if field.get("ref", "").endswith("._id"):
                    self.assertEqual(field["type"], "UuidString")

    def test_python_entity_id_fields_are_mongodb_ids_with_generators(self):
        entity_dir = Path("payments/src/payments/domain/entities")
        entity_files = [
            "billing_auth.py",
            "billing_method.py",
            "checkout.py",
            "idempotency_key.py",
            "invoice.py",
            "one_time_sku.py",
            "operation_lock.py",
            "operator_audit.py",
            "payment.py",
            "payment_cancel_request.py",
            "payment_customer.py",
            "payment_instrument.py",
            "product.py",
            "subscription.py",
            "subscription_plan.py",
            "webhook_event.py",
        ]

        for filename in entity_files:
            entity_path = entity_dir / filename
            self.assertTrue(entity_path.exists(), filename)
            source = entity_path.read_text(encoding="utf-8")
            self.assertIn("id: str", source, filename)
            self.assertIn("def generate_id", source, filename)
            self.assertIn("generate_uuid_id", source, filename)

        id_helper = (entity_dir / "ids.py").read_text(encoding="utf-8")
        self.assertIn("uuid.uuid7().hex", id_helper)
        self.assertIn("generate_uuid_id", id_helper)

    def test_documented_mongodb_collections_have_python_entities(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        entity_dir = Path("payments/src/payments/domain/entities")
        entity_by_collection = {
            "invoices": "invoice.py",
            "billing-methods": "billing_method.py",
            "payment-instruments": "payment_instrument.py",
            "webhook-events": "webhook_event.py",
            "idempotency-keys": "idempotency_key.py",
            "operation-locks": "operation_lock.py",
            "operator-audits": "operator_audit.py",
            "payment-cancel-requests": "payment_cancel_request.py",
        }

        for collection_id, filename in entity_by_collection.items():
            collection = self._collection_by_id(data, collection_id)
            entity_path = entity_dir / filename
            self.assertTrue(entity_path.exists(), filename)
            source = entity_path.read_text(encoding="utf-8")

            self.assertIn("id: str", source, filename)
            self.assertIn("def generate_id", source, filename)
            self.assertIn("generate_uuid_id", source, filename)

            for field in collection["fields"]:
                field_name = "id" if field["name"] == "_id" else field["name"]
                self.assertIn(f"{field_name}:", source, f"{filename} missing {field_name}")
                for enum_value in field.get("enum", []):
                    self.assertIn(f'"{enum_value}"', source, f"{filename} missing enum {enum_value}")

    def test_documented_optional_fields_match_python_entities(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        entity_by_collection = {
            "products": "product.py",
            "subscription-plans": "subscription_plan.py",
            "one-time-skus": "one_time_sku.py",
            "checkouts": "checkout.py",
            "payment-customers": "payment_customer.py",
            "billing-auths": "billing_auth.py",
            "subscriptions": "subscription.py",
            "payments": "payment.py",
            "payment-cancel-requests": "payment_cancel_request.py",
            "invoices": "invoice.py",
            "billing-methods": "billing_method.py",
            "payment-instruments": "payment_instrument.py",
            "webhook-events": "webhook_event.py",
            "idempotency-keys": "idempotency_key.py",
            "operation-locks": "operation_lock.py",
            "operator-audits": "operator_audit.py",
        }

        for collection_id, filename in entity_by_collection.items():
            collection = self._collection_by_id(data, collection_id)
            entity_fields = self._entity_fields(filename)
            for field in collection["fields"]:
                field_name = "id" if field["name"] == "_id" else field["name"]
                self.assertIn(field_name, entity_fields, f"{filename} missing {field_name}")
                self.assertEqual(
                    not field["required"],
                    entity_fields[field_name]["optional"],
                    f"{collection_id}.{field['name']} optional contract differs from {filename}",
                )

    def test_payment_entity_matches_documented_payment_collection(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        payments = self._collection_by_id(data, "payments")
        source = Path("payments/src/payments/domain/entities/payment.py").read_text(encoding="utf-8")

        for field in payments["fields"]:
            field_name = "id" if field["name"] == "_id" else field["name"]
            self.assertIn(f"{field_name}:", source)
            for enum_value in field.get("enum", []):
                self.assertIn(f'"{enum_value}"', source)

        optional_fields = [
            "subscription_id",
            "billing_cycle_key",
            "checkout_id",
            "payment_customer_id",
            "payment_key",
            "approved_at",
            "receipt_url",
            "method",
            "method_detail",
            "failure",
            "provider_response_summary",
            "cancelable_amount",
            "cancel_history",
            "expires_at",
            "retry_scheduled_at",
        ]
        for field_name in optional_fields:
            self.assertRegex(source, rf"{field_name}: .* \\| None = None")

        required_fields = ["order_id", "amount", "status", "created_at"]
        for field_name in required_fields:
            self.assertRegex(source, rf"{field_name}: (?!.*None = None)")

    def test_payment_result_snapshot_contract_is_documented(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        payments = self._collection_by_id(data, "payments")
        fields = {field["name"]: field for field in payments["fields"]}

        for field_name in [
            "approved_at",
            "receipt_url",
            "method",
            "method_detail",
            "failure",
            "provider_response_summary",
            "cancel_history",
        ]:
            self.assertIn(field_name, fields)

        self.assertIn("paidAmount", fields["amount"]["description"])
        self.assertIn("methodDetail", fields["method_detail"]["description"])
        self.assertIn("카드 결제일 때만", fields["method_detail"]["description"])
        self.assertIn("민감 정보", fields["provider_response_summary"]["description"])
        self.assertIn("진행 중/실패한 취소 요청", fields["cancel_history"]["description"])
        self.assertEqual(
            [field["name"] for field in fields["failure"]["properties"]],
            ["phase", "reason", "providerCode", "message", "retryable"]
        )
        failure_properties = {field["name"]: field for field in fields["failure"]["properties"]}
        self.assertEqual(failure_properties["phase"]["enum"], ["before_confirm", "confirm", "cancel", "webhook", "sync"])
        self.assertEqual(
            failure_properties["reason"]["enum"],
            [
                "user_canceled",
                "auth_failed",
                "provider_rejected",
                "provider_error",
                "validation_failed",
                "auth_result_not_reported",
                "expired",
            ]
        )
        provider_properties = {field["name"]: field for field in fields["provider_response_summary"]["properties"]}
        self.assertEqual(provider_properties["provider"]["enum"], ["tosspayments"])
        self.assertIn("providerStatus", provider_properties)
        cancel_item_properties = {field["name"]: field for field in fields["cancel_history"]["items"]["properties"]}
        self.assertEqual(cancel_item_properties["requestedBy"]["enum"], ["user", "admin", "system"])
        self.assertTrue(cancel_item_properties["cancelId"]["required"])

        payment_source = Path("payments/src/payments/domain/entities/payment.py").read_text(encoding="utf-8")
        self.assertIn("method_detail: dict[str, Any] | None = None", payment_source)
        self.assertIn("cancel_history: list[dict[str, Any]] | None = None", payment_source)

        contract_text = json.dumps(
            {
                "confirm": data["apiDetails"]["payments-confirm"],
                "detail": data["apiDetails"]["payments-detail"],
                "cancel": data["apiDetails"]["payments-cancel"],
                "adminCancel": data["apiDetails"]["admin-payment-cancel"],
            },
            ensure_ascii=False,
        )
        self.assertIn("methodDetail", contract_text)
        self.assertIn("payment.amount에서 매핑", contract_text)
        self.assertNotIn("마스킹된 카드 정보", contract_text)

    def test_database_doc_renders_nested_payment_snapshot_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            generate_docs("docs-data/documentation.json", out_dir)

            database = (out_dir / "database-doc.html").read_text(encoding="utf-8")
            self.assertIn("nested-schema-row", database)
            self.assertIn("<code>failure</code> properties", database)
            self.assertIn("<code>provider_response_summary</code> properties", database)
            self.assertIn("<code>cancel_history</code> items.properties", database)
            self.assertIn("<th>하위 필드</th><th>타입</th><th>필수</th><th>Enum</th><th>설명</th>", database)
            self.assertIn("<td class=\"nested-schema-cell\"><code>phase</code></td>", database)
            self.assertIn("before_confirm, confirm, cancel, webhook, sync", database)
            self.assertIn("<td class=\"nested-schema-cell\"><code>requestedBy</code></td>", database)
            self.assertIn("user, admin, system", database)
            self.assertIn("nested schema: properties", database)
            self.assertIn("nested schema: items.properties", database)

    def test_payment_cancel_request_contract_is_separate_from_payment_status(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        payments = self._collection_by_id(data, "payments")
        cancel_requests = self._collection_by_id(data, "payment-cancel-requests")
        payment_status = next(field for field in payments["fields"] if field["name"] == "status")
        cancel_status = next(field for field in cancel_requests["fields"] if field["name"] == "status")

        self.assertNotIn("cancel_pending", payment_status["enum"])
        self.assertEqual(cancel_status["enum"], ["pending", "succeeded", "failed"])

        cancel_fields = {field["name"]: field for field in cancel_requests["fields"]}
        for field_name in [
            "payment_id",
            "idempotency_key_hash",
            "cancel_amount",
            "cancel_reason",
            "requested_by",
            "provider_cancel_id",
            "failure",
        ]:
            self.assertIn(field_name, cancel_fields)

        unique = self._index_by_name(cancel_requests, "uniq_payment_cancel_requests_idempotency")
        self.assertEqual(unique["fields"], ["payment_id", "idempotency_key_hash"])
        self.assertTrue(unique["unique"])

        pending = self._index_by_name(cancel_requests, "idx_payment_cancel_requests_pending_created_at")
        self.assertEqual(pending["fields"], ["status", "created_at"])
        self.assertEqual(pending["partialFilterExpression"], {"status": "pending"})

        relationships = {(item["from"], item["to"]) for item in data["database"]["relationships"]}
        self.assertIn(("payment_cancel_requests.payment_id", "payments._id"), relationships)
        self.assertIn(("payment_cancel_requests.operator_audit_id", "operator_audits._id"), relationships)

        dependencies = {item["apiId"]: item for item in data["database"]["apiAccess"]}
        self.assertIn("payment-cancel-requests", dependencies["payments-cancel"]["reads"])
        self.assertIn("payment-cancel-requests", dependencies["payments-cancel"]["writes"])
        self.assertIn("payment-cancel-requests", dependencies["admin-payment-cancel"]["reads"])
        self.assertIn("payment-cancel-requests", dependencies["admin-payment-cancel"]["writes"])

        source = Path("payments/src/payments/domain/entities/payment_cancel_request.py").read_text(encoding="utf-8")
        self.assertIn('Literal["pending", "succeeded", "failed"]', source)
        self.assertIn('Literal["user", "admin", "system"]', source)
        self.assertIn('generate_uuid_id("pcancel")', source)

        contract_text = json.dumps(
            {
                "cancel": data["apiDetails"]["payments-cancel"],
                "adminCancel": data["apiDetails"]["admin-payment-cancel"],
                "risk": next(risk for risk in data["risks"] if risk["id"] == "cancel-refund-duplicate-or-mismatch"),
            },
            ensure_ascii=False,
        )
        self.assertIn("payment_cancel_requests", contract_text)
        self.assertIn("status=pending", contract_text)
        self.assertNotIn("cancel_pending", contract_text)

    def test_payment_expiration_contract_is_documented(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        payments = self._collection_by_id(data, "payments")
        payment_fields = {field["name"]: field for field in payments["fields"]}
        payment_status = payment_fields["status"]

        self.assertIn("expired", payment_status["enum"])
        self.assertIn("expires_at", payment_fields)
        self.assertIn("30분", payment_fields["expires_at"]["description"])

        ready_expiration_index = self._index_by_name(payments, "idx_payments_ready_expires_at")
        self.assertEqual(ready_expiration_index["fields"], ["status", "expires_at"])
        self.assertEqual(
            ready_expiration_index["partialFilterExpression"],
            {"status": "ready", "expires_at": {"$type": "date"}}
        )

        state_model = next(model for model in data["database"]["stateModels"] if model["id"] == "payment-status")
        self.assertIn("expired", state_model["states"])
        transitions = {(transition["from"], transition["to"]) for transition in state_model["transitions"]}
        self.assertIn(("ready", "expired"), transitions)

        payment_entity_fields = self._entity_fields("payment.py")
        self.assertIn("expired", payment_entity_fields["status"]["annotation"])
        self.assertTrue(payment_entity_fields["expires_at"]["optional"])

        contract_text = json.dumps(
            {
                "orders": data["apiDetails"]["payments-orders"],
                "confirm": data["apiDetails"]["payments-confirm"],
                "detail": data["apiDetails"]["payments-detail"],
                "authResult": data["apiDetails"]["payments-auth-result"],
            },
            ensure_ascii=False,
        )
        self.assertIn("PAYMENT_ATTEMPT_TTL은 30분", contract_text)
        self.assertIn("expiresAt=createdAt+30분", contract_text)
        self.assertIn("expiresAt <= now", contract_text)
        self.assertIn("토스 승인 API를 호출하지 않고", contract_text)
        self.assertIn("failure.reason=auth_result_not_reported", contract_text)

    def test_billing_auth_entity_persists_default_choice_and_expiration(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        billing_auths = self._collection_by_id(data, "billing-auths")
        source = Path("payments/src/payments/domain/entities/billing_auth.py").read_text(encoding="utf-8")

        documented_fields = {field["name"] for field in billing_auths["fields"]}
        self.assertIn("set_as_default", documented_fields)
        self.assertIn("expires_at", documented_fields)
        self.assertNotIn("billing_auth_id", documented_fields)
        self.assertNotIn("order_id", documented_fields)

        expected_fields = [
            "id",
            "user_id",
            "payment_customer_id",
            "customer_key_snapshot",
            "set_as_default",
            "status",
            "expires_at",
        ]
        for field_name in expected_fields:
            self.assertIn(f"{field_name}:", source)

        self.assertNotIn("billing_auth_id:", source)
        self.assertNotIn("order_id:", source)

        self.assertIn("datetime", source)

    def test_invoice_status_contract_stays_simple(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        invoices = self._collection_by_id(data, "invoices")
        invoice_status = next(field for field in invoices["fields"] if field["name"] == "status")

        self.assertEqual(invoice_status["enum"], ["issued", "paid", "voided", "refunded"])

        invoice_source = Path("payments/src/payments/domain/entities/invoice.py").read_text(encoding="utf-8")
        self.assertIn('Literal["issued", "paid", "voided", "refunded"]', invoice_source)

        invoice_contract_text = json.dumps(
            {
                "list": data["apiDetails"]["invoices-list"],
                "detail": data["apiDetails"]["invoices-detail"],
                "retry": data["apiDetails"]["internal-billing-retry"],
                "subscriptionRetry": next(sequence for sequence in data["sequences"] if sequence["id"] == "subscription-retry"),
                "finalFailure": next(sequence for sequence in data["sequences"] if sequence["id"] == "subscription-final-failure"),
                "risk": next(risk for risk in data["risks"] if risk["id"] == "subscription-retry-duplicate-charge"),
            },
            ensure_ascii=False,
        )

        self.assertNotIn("invoice.status=failed", invoice_contract_text)
        self.assertNotIn("invoice.status = pending", invoice_contract_text)
        self.assertNotIn("invoice failed", invoice_contract_text)
        self.assertNotIn("invoice=final_failed", invoice_contract_text)
        self.assertNotIn("final_failed", invoice_contract_text)
        self.assertNotIn("retrying", invoice_contract_text)
        self.assertNotIn("attemptCount", invoice_contract_text)
        self.assertNotIn("nextRetryAt", invoice_contract_text)
        self.assertIn("invoice.status=issued", invoice_contract_text)
        self.assertIn("paymentStatus", invoice_contract_text)
        self.assertIn("retry.scheduledAt", invoice_contract_text)

    def test_subscription_retry_schedule_lives_on_failed_payment(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        payments = self._collection_by_id(data, "payments")
        invoices = self._collection_by_id(data, "invoices")
        payment_fields = {field["name"]: field for field in payments["fields"]}
        invoice_fields = {field["name"]: field for field in invoices["fields"]}

        self.assertIn("retry_scheduled_at", payment_fields)
        self.assertIn("최신 failed payment.retry_scheduled_at", payment_fields["retry_scheduled_at"]["description"])
        self.assertNotIn("retry_scheduled_at", invoice_fields)

        retry_index = self._index_by_name(payments, "idx_payments_failed_retry_scheduled_at")
        self.assertEqual(retry_index["fields"], ["status", "retry_scheduled_at"])
        self.assertEqual(
            retry_index["partialFilterExpression"],
            {"status": "failed", "retry_scheduled_at": {"$type": "date"}}
        )

        payment_source = Path("payments/src/payments/domain/entities/payment.py").read_text(encoding="utf-8")
        self.assertIn("retry_scheduled_at: datetime | None = None", payment_source)

        retry_contract_text = json.dumps(
            {
                "detail": data["apiDetails"]["invoices-detail"],
                "billingRun": data["apiDetails"]["internal-billing-run"],
                "billingRetry": data["apiDetails"]["internal-billing-retry"],
                "subscriptionRetry": next(sequence for sequence in data["sequences"] if sequence["id"] == "subscription-retry"),
                "risk": next(risk for risk in data["risks"] if risk["id"] == "subscription-retry-duplicate-charge"),
            },
            ensure_ascii=False,
        )

        self.assertIn("payment.retry_scheduled_at", retry_contract_text)
        self.assertIn("latestPayment.retry_scheduled_at", retry_contract_text)
        self.assertIn("retry.scheduledAt", retry_contract_text)
        self.assertNotIn("invoice.retry_scheduled_at", retry_contract_text)

    def test_public_checkout_id_maps_to_mongodb_id_without_duplicate_field(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        checkouts = self._collection_by_id(data, "checkouts")
        fields = {field["name"]: field for field in checkouts["fields"]}

        self.assertIn("_id", fields)
        self.assertIn("checkoutId", fields["_id"]["description"])
        self.assertNotIn("checkout_id", fields)

        payments = self._collection_by_id(data, "payments")
        payment_checkout = next(field for field in payments["fields"] if field["name"] == "checkout_id")
        self.assertEqual(payment_checkout["ref"], "checkouts._id")

    def test_subscription_plan_entity_includes_required_entitlements(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        subscription_plans = self._collection_by_id(data, "subscription-plans")
        source = Path("payments/src/payments/domain/entities/subscription_plan.py").read_text(encoding="utf-8")

        entitlements = next(field for field in subscription_plans["fields"] if field["name"] == "entitlements")
        self.assertTrue(entitlements["required"])
        self.assertIn("entitlements:", source)
        self.assertNotRegex(source, r"entitlements: .* \| None = None")

    def test_selling_unit_status_docs_match_database_contract(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        expected_statuses = ["draft", "active", "paused", "archived"]

        subscription_plans = self._collection_by_id(data, "subscription-plans")
        one_time_skus = self._collection_by_id(data, "one-time-skus")
        plan_status = next(field for field in subscription_plans["fields"] if field["name"] == "status")
        sku_status = next(field for field in one_time_skus["fields"] if field["name"] == "status")

        self.assertEqual(plan_status["enum"], expected_statuses)
        self.assertEqual(sku_status["enum"], expected_statuses)

        selling_unit_docs = json.dumps(
            {
                "plan_update": data["apiDetails"]["admin-subscription-plans-update"],
                "sku_update": data["apiDetails"]["admin-one-time-skus-update"],
            },
            ensure_ascii=False,
        )
        self.assertNotIn("hidden", selling_unit_docs)
        self.assertIn("draft, active, paused, archived", selling_unit_docs)
        self.assertIn("paused 또는 archived", selling_unit_docs)

    def test_one_time_sku_keeps_api_dto_and_internal_storage_contract_distinct(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        one_time_skus = self._collection_by_id(data, "one-time-skus")
        source = Path("payments/src/payments/domain/entities/one_time_sku.py").read_text(encoding="utf-8")

        fields = {field["name"]: field for field in one_time_skus["fields"]}
        self.assertIn("_id", fields)
        self.assertIn("stock_policy", fields)
        self.assertEqual(fields["stock_policy"]["type"], "string")
        self.assertEqual(fields["stock_policy"]["enum"], ["unlimited", "limited"])
        self.assertFalse(fields["purchase_limit"]["required"])

        self.assertIn("id: str", source)
        self.assertIn('stock_policy: Literal["unlimited", "limited"]', source)
        self.assertIn("purchase_limit: dict | None = None", source)

        api_text = json.dumps(
            {
                "create": data["apiDetails"]["admin-one-time-skus-create"],
                "update": data["apiDetails"]["admin-one-time-skus-update"],
            },
            ensure_ascii=False,
        )
        self.assertIn("skuId", api_text)
        self.assertIn("stockPolicy.type", api_text)
        self.assertIn("OneTimeSku.id", api_text)
        self.assertIn("stock_policy", api_text)

    def test_documentation_includes_core_mongodb_collections(self):
        data = json.loads(Path("docs-data/documentation.json").read_text(encoding="utf-8"))
        collection_ids = {collection["id"] for collection in data["database"]["collections"]}

        self.assertTrue(
            {
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
        payment_customer_fields = {field["name"] for field in collections["payment-customers"]["fields"]}
        payment_customer_indexes = {tuple(index["fields"]) for index in collections["payment-customers"]["indexes"]}
        billing_method_fields = {field["name"] for field in collections["billing-methods"]["fields"]}
        payment_instrument_fields = {field["name"] for field in collections["payment-instruments"]["fields"]}
        payment_instrument_indexes = {tuple(index["fields"]) for index in collections["payment-instruments"]["indexes"]}

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
                    "label": "백엔드 서버가 결제 승인 요청 전달",
                    "apiId": "payments-confirm",
                    "note": "paymentId, paymentKey, orderId, amount, Authorization, X-Request-User-Id, X-Request-Id, Idempotency-Key"
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
        self.assertIn("백엔드 서버가 결제 승인 요청 전달", source)
        self.assertIn("**POST /payments/confirm**", source)
        self.assertIn("paymentId, paymentKey, orderId, amount, Authorization, X-Request-User-Id, X-Request-Id, Idempotency-Key", source)
        self.assertNotIn("**paymentId, paymentKey, orderId, amount, Authorization, X-Request-User-Id, X-Request-Id, Idempotency-Key**", source)
        self.assertNotIn('client -> server: "백엔드 서버가 결제 승인 요청 전달"', source)

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
                    "summary": "백엔드 서버가 프론트 성공 페이지 요청을 받아 결제 시스템에 전달합니다.",
                    "request": {
                        "headers": [
                            {"name": "Authorization", "required": True, "description": "내부 서비스 토큰입니다."},
                            {"name": "X-Request-User-Id", "required": True, "description": "백엔드 서버가 인증한 회원 ID입니다."},
                            {"name": "Idempotency-Key", "required": True, "description": "중복 결제를 방지합니다."}
                        ],
                        "cookies": [],
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
                                "type": "UuidString",
                                "required": True,
                                "description": "구독의 MongoDB _id입니다. 애플리케이션이 자체 UUID 방식으로 생성해 문자열로 저장합니다."
                            },
                            {
                                "name": "user_id",
                                "type": "ExternalUserId",
                                "required": True,
                                "description": "회원 서비스가 소유한 외부 사용자 식별자입니다."
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
                                "fields": ["user_id", "status"],
                                "unique": False,
                                "description": "사용자의 현재 구독 조회에 사용합니다."
                            }
                        ],
                        "relatedApis": ["subscriptions-confirm"]
                    }
                ],
                "relationships": [
                    {
                        "from": "subscriptions.user_id",
                        "to": "member_service.user",
                        "type": "reference",
                        "description": "구독 소유자는 외부 회원 서비스의 사용자입니다."
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
            self.assertIn('data-label="이름"', detail)
            self.assertIn('<div class="hero">', detail)
            self.assertIn('<div class="wrap">', detail)
            self.assertNotIn('<div class="wrap hero">', detail)
            self.assertIn("-webkit-text-size-adjust: 100%;", detail)
            self.assertIn("overflow-x: clip;", detail)
            self.assertNotIn("overflow-x: hidden;", detail)
            self.assertNotIn("overflow: hidden;", detail)
            self.assertIn("@media (min-width: 901px)", detail)
            self.assertIn("@media (min-width: 481px) and (max-width: 900px)", detail)
            self.assertIn("grid-template-columns: minmax(170px, 210px) minmax(0, 1fr);", detail)
            self.assertIn("grid-template-columns: 300px minmax(0, 1fr);", detail)
            self.assertIn("width: min(1440px, calc(100% - 64px));", detail)
            self.assertIn("grid-template-columns: minmax(0, 1fr);", detail)
            self.assertIn(".layout > nav,", detail)
            self.assertIn("max-width: 100%;", detail)
            self.assertIn("width: 100%;", detail)
            self.assertIn("margin-inline: 0;", detail)
            self.assertIn("td::before", detail)
            self.assertIn("content: attr(data-label);", detail)
            self.assertIn("border-spacing: 0;", detail)
            self.assertIn("display: grid;", detail)
            self.assertIn("gap: 8px;", detail)
            self.assertIn("box-shadow: inset 0 0 0 1px var(--line);", detail)
            self.assertIn("td:not(:last-child)", detail)
            self.assertIn("@media (max-width: 480px)", detail)
            self.assertIn('aria-current", "true"', detail)
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
            self.assertIn("subscriptions.user_id", database)
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

    def test_sequence_page_uses_existing_svg_without_rerendering_d2(self):
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
            diagrams_dir = out_dir / "diagrams"
            diagrams_dir.mkdir(parents=True)
            data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            (diagrams_dir / "payment-main.svg").write_text("<svg></svg>", encoding="utf-8")

            generate_docs(data_path, out_dir)

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
            success_diagram = (out_dir / "diagrams" / "initial-subscription-success-initial-subscription-success-main.d2").read_text(encoding="utf-8")
            recurring_sequence = (out_dir / "recurring-billing-sequence.html").read_text(encoding="utf-8")

            self.assertIn("productCode", detail)
            self.assertIn("subscriptions", detail)
            self.assertIn("같은 productCode의 활성 구독이 이미 있으면", detail)
            self.assertIn("다른 productCode의 활성 구독이 있으면 별도 상품 구독으로 허용", detail)
            self.assertIn("상품별 중복 구독 검증", success_sequence)
            self.assertIn("UNIQUE active(userId, productCode)", success_sequence)
            self.assertIn("현재 구독 상태 조회", success_sequence)
            self.assertIn("GET /subscriptions/me", success_diagram)
            self.assertIn("현재 구독 화면 데이터 반환", success_diagram)
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
            self.assertIn("reservedStock += quantity", sequence)
            self.assertIn("reservedStock -&gt; soldStock", sequence)
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
            self.assertIn("reservedStock을 해제", sequence)
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
            self.assertIn("생략하면 unlimited", detail)
            self.assertIn("대부분의 일반상품은 unlimited SKU", detail)
            self.assertIn("totalStock", detail)
            self.assertIn("reservedStock", detail)
            self.assertIn("soldStock", detail)
            self.assertIn("availableStock", detail)
            self.assertIn("totalStock - reservedStock - soldStock", detail)
            self.assertIn("MISSING_ACTIVE_SELLING_UNIT", detail)
            self.assertIn("구독상품 생성 및 플랜 구성", sequence)
            self.assertIn("일반상품 생성 및 SKU 구성", sequence)
            self.assertIn("구독 플랜은 기존 활성 구독의 과거 가격을 덮어쓰지 않습니다", sequence)
            self.assertIn("stockPolicy=unlimited 기본값", sequence)
            self.assertIn("limited SKU만 재고를 예약합니다", sequence)

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
            self.assertIn("subscription.status=canceled", sequence)
            self.assertIn("nextBillingDate=null", sequence)
            self.assertIn("다시 이용하려면 새 구독을 시작", sequence)
            self.assertNotIn("graceEndsAt", sequence)
            self.assertNotIn("serviceAccess=limited", sequence)
            self.assertNotIn("복구 조건", sequence)
            self.assertIn("template=subscription_canceled_payment_failed", diagram)
            self.assertIn("cancelReason=payment_retry_exhausted", diagram)


if __name__ == "__main__":
    unittest.main()
