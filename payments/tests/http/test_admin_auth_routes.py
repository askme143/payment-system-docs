from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

import pytest

from payments.application.admin_auth import hash_admin_password
from payments.domain.entities.admin_auth import AdminAccount


def test_admin_login_sends_email_link(client, test_dependencies, caplog) -> None:
    _seed_admin(test_dependencies)
    caplog.set_level(logging.INFO)

    response = client.post(
        "/admin/auth/login",
        headers={
            "X-Request-Id": "req_admin_auth",
            "User-Agent": "admin-console/login",
        },
        json={
            "email": "OPS@example.com",
            "password": "correct-horse-battery-staple",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "delivery": "email_link",
        "expiresInSeconds": 600,
    }
    assert test_dependencies.admin_auth_email_sender.login_links[0][0] == (
        "ops@example.com"
    )
    raw_token = test_dependencies.admin_auth_email_sender.login_links[0][1]
    assert raw_token.startswith("alt_")
    assert all(
        token.token_hash != raw_token
        for token in test_dependencies.admin_auth.auth_tokens.values()
    )
    [record] = _admin_auth_access_records(caplog, "login_link_request")
    assert record.payment_auth_result == "succeeded"
    assert record.payment_admin_id == "admin_1"
    assert record.payment_request_id == "req_admin_auth"
    assert record.payment_request_ip is not None
    assert record.payment_user_agent == "admin-console/login"


def test_admin_login_rate_limits_repeated_link_requests(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)

    for index in range(3):
        response = client.post(
            "/admin/auth/login",
            headers={"X-Request-Id": f"req_admin_auth_rate_{index}"},
            json={
                "email": "ops@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        assert response.status_code == 202

    limited = client.post(
        "/admin/auth/login",
        headers={"X-Request-Id": "req_admin_auth_rate_limited"},
        json={
            "email": "ops@example.com",
            "password": "correct-horse-battery-staple",
        },
    )

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert len(test_dependencies.admin_auth_email_sender.login_links) == 3


def test_admin_login_revokes_login_link_when_email_send_fails(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)
    test_dependencies.admin_auth_email_sender.fail_login_link = True

    with pytest.raises(RuntimeError, match="SMTP unavailable"):
        client.post(
            "/admin/auth/login",
            headers={"X-Request-Id": "req_admin_auth_email_failure"},
            json={
                "email": "ops@example.com",
                "password": "correct-horse-battery-staple",
            },
        )

    login_tokens = [
        token
        for token in test_dependencies.admin_auth.auth_tokens.values()
        if token.token_type == "login_link"
    ]
    assert len(login_tokens) == 1
    assert login_tokens[0].status == "revoked"
    assert login_tokens[0].consumed_at == test_dependencies.clock.utc_now()


def test_admin_login_rejects_invalid_contract_values_as_400(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)

    invalid_payloads = [
        {"email": 123, "password": "correct-horse-battery-staple"},
        {"email": "ops@example.com", "password": ""},
        {"email": "invalid-email", "password": "correct-horse-battery-staple"},
        {"email": "ops@", "password": "correct-horse-battery-staple"},
    ]

    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            "/admin/auth/login",
            headers={"X-Request-Id": f"req_admin_auth_invalid_{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_admin_login_confirm_me_refresh_logout_flow(
    client,
    test_dependencies,
    caplog,
) -> None:
    _seed_admin(test_dependencies)
    caplog.set_level(logging.INFO)
    client.post(
        "/admin/auth/login",
        headers={"X-Request-Id": "req_admin_auth"},
        json={
            "email": "ops@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    login_token = test_dependencies.admin_auth_email_sender.login_links[0][1]

    confirmed = client.post(
        "/admin/auth/login/confirm",
        headers={
            "X-Request-Id": "req_admin_auth_confirm",
            "User-Agent": "admin-console/1.0",
        },
        json={"loginToken": login_token},
    )

    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["admin"] == {
        "adminId": "admin_1",
        "email": "ops@example.com",
        "displayName": "운영 담당자",
        "roles": ["operator"],
        "permissions": [
            "payment_read",
            "payment_cancel",
            "subscription_adjust",
        ],
    }
    assert body["adminAccessToken"].startswith("aat_")
    assert body["adminRefreshToken"].startswith("art_")
    assert test_dependencies.admin_auth_uow_factory.enter_count == 1
    assert test_dependencies.admin_auth_uow_factory.commit_count == 1
    assert test_dependencies.admin_auth_uow_factory.rollback_count == 0
    assert _admin_access_payload(body["adminAccessToken"])["roles"] == ["operator"]
    assert _admin_access_payload(body["adminAccessToken"])["permissions"] == [
        "payment_read",
        "payment_cancel",
        "subscription_adjust",
    ]
    refresh_tokens = [
        token
        for token in test_dependencies.admin_auth.auth_tokens.values()
        if token.token_type == "refresh_token"
    ]
    assert refresh_tokens[-1].user_agent == "admin-console/1.0"

    me = client.get(
        "/admin/auth/me",
        headers={
            "X-Request-Id": "req_admin_me",
            "Authorization": f"Bearer {body['adminAccessToken']}",
        },
    )
    assert me.status_code == 200
    assert me.json()["status"] == "active"

    refreshed = client.post(
        "/admin/auth/refresh",
        headers={
            "X-Request-Id": "req_admin_refresh",
            "User-Agent": "admin-console/1.1",
        },
        json={"adminRefreshToken": body["adminRefreshToken"]},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["adminRefreshToken"] != body["adminRefreshToken"]
    assert test_dependencies.admin_auth_uow_factory.enter_count == 2
    assert test_dependencies.admin_auth_uow_factory.commit_count == 2
    assert test_dependencies.admin_auth_uow_factory.rollback_count == 0
    assert _admin_access_payload(refreshed.json()["adminAccessToken"])[
        "permissions"
    ] == [
        "payment_read",
        "payment_cancel",
        "subscription_adjust",
    ]
    refresh_tokens = [
        token
        for token in test_dependencies.admin_auth.auth_tokens.values()
        if token.token_type == "refresh_token"
    ]
    assert refresh_tokens[-1].user_agent == "admin-console/1.1"

    reused = client.post(
        "/admin/auth/refresh",
        headers={"X-Request-Id": "req_admin_refresh_again"},
        json={"adminRefreshToken": body["adminRefreshToken"]},
    )
    assert reused.status_code == 401
    assert test_dependencies.admin_auth_uow_factory.enter_count == 3
    assert test_dependencies.admin_auth_uow_factory.commit_count == 3
    assert test_dependencies.admin_auth_uow_factory.rollback_count == 0
    active_refresh_tokens = [
        token
        for token in test_dependencies.admin_auth.auth_tokens.values()
        if token.token_type == "refresh_token" and token.status == "active"
    ]
    assert active_refresh_tokens == []

    logout = client.post(
        "/admin/auth/logout",
        headers={
            "X-Request-Id": "req_admin_logout",
            "Authorization": f"Bearer {refreshed.json()['adminAccessToken']}",
            "User-Agent": "admin-console/1.2",
        },
        json={"adminRefreshToken": refreshed.json()["adminRefreshToken"]},
    )
    assert logout.status_code == 204
    logged_out_refresh_tokens = [
        token
        for token in test_dependencies.admin_auth.auth_tokens.values()
        if token.token_type == "refresh_token"
        and token.status == "revoked"
        and token.user_agent == "admin-console/1.2"
    ]
    assert logged_out_refresh_tokens
    assert logged_out_refresh_tokens[-1].last_used_at == (
        test_dependencies.clock.utc_now()
    )
    assert logged_out_refresh_tokens[-1].request_ip is not None

    refresh_after_logout = client.post(
        "/admin/auth/refresh",
        headers={"X-Request-Id": "req_admin_refresh_after_logout"},
        json={"adminRefreshToken": refreshed.json()["adminRefreshToken"]},
    )
    assert refresh_after_logout.status_code == 401
    confirm_records = _admin_auth_access_records(caplog, "login_confirm")
    assert confirm_records[-1].payment_auth_result == "succeeded"
    assert confirm_records[-1].payment_admin_id == "admin_1"
    assert confirm_records[-1].payment_request_id == "req_admin_auth_confirm"
    assert confirm_records[-1].payment_user_agent == "admin-console/1.0"
    logout_records = _admin_auth_access_records(caplog, "logout")
    assert logout_records[-1].payment_auth_result == "succeeded"
    assert logout_records[-1].payment_admin_id == "admin_1"
    assert logout_records[-1].payment_request_id == "req_admin_logout"
    assert logout_records[-1].payment_user_agent == "admin-console/1.2"


def test_admin_login_confirm_rejects_invalid_token_contract(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)

    invalid_values = [None, 123, "invalid-token"]
    for index, value in enumerate(invalid_values):
        payload = {} if value is None else {"loginToken": value}
        response = client.post(
            "/admin/auth/login/confirm",
            headers={"X-Request-Id": f"req_admin_confirm_invalid_{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"

    missing = client.post(
        "/admin/auth/login/confirm",
        headers={"X-Request-Id": "req_admin_confirm_missing"},
        json={"loginToken": "alt_missing"},
    )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"


def test_admin_token_endpoints_match_documented_error_codes(
    client,
    admin_headers,
) -> None:
    broken_me = client.get(
        "/admin/auth/me",
        headers={
            "X-Request-Id": "req_admin_me_broken",
            "Authorization": "Bearer aat_not-json.signature",
        },
    )
    assert broken_me.status_code == 401

    invalid_refresh_values = [None, 123, "invalid-token"]
    for index, value in enumerate(invalid_refresh_values):
        payload = {} if value is None else {"adminRefreshToken": value}
        response = client.post(
            "/admin/auth/refresh",
            headers={"X-Request-Id": f"req_admin_refresh_invalid_{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"

    missing_refresh = client.post(
        "/admin/auth/refresh",
        headers={"X-Request-Id": "req_admin_refresh_missing"},
        json={"adminRefreshToken": "art_missing"},
    )
    assert missing_refresh.status_code == 401

    invalid_logout_body = client.post(
        "/admin/auth/logout",
        headers=admin_headers,
        json={"adminRefreshToken": 123},
    )
    assert invalid_logout_body.status_code == 204


def test_admin_logout_ignores_invalid_refresh_token_without_global_logout(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)
    client.post(
        "/admin/auth/login",
        headers={"X-Request-Id": "req_admin_auth"},
        json={
            "email": "ops@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    login_token = test_dependencies.admin_auth_email_sender.login_links[0][1]
    confirmed = client.post(
        "/admin/auth/login/confirm",
        headers={"X-Request-Id": "req_admin_auth_confirm"},
        json={"loginToken": login_token},
    )

    logout = client.post(
        "/admin/auth/logout",
        headers={
            "X-Request-Id": "req_admin_logout_invalid_body",
            "Authorization": f"Bearer {confirmed.json()['adminAccessToken']}",
        },
        json={"adminRefreshToken": 123},
    )
    refreshed = client.post(
        "/admin/auth/refresh",
        headers={"X-Request-Id": "req_admin_refresh_after_invalid_logout"},
        json={"adminRefreshToken": confirmed.json()["adminRefreshToken"]},
    )

    assert logout.status_code == 204
    assert refreshed.status_code == 200


def test_admin_password_reset_hides_unknown_account_and_resets(
    client,
    test_dependencies,
    caplog,
) -> None:
    _seed_admin(test_dependencies)
    caplog.set_level(logging.INFO)

    unknown = client.post(
        "/admin/auth/password-reset/request",
        headers={"X-Request-Id": "req_reset_unknown"},
        json={"email": "missing@example.com"},
    )
    assert unknown.status_code == 202
    assert unknown.json() == {"accepted": True}
    assert test_dependencies.admin_auth_email_sender.password_reset_links == []

    requested = client.post(
        "/admin/auth/password-reset/request",
        headers={"X-Request-Id": "req_reset"},
        json={"email": "ops@example.com"},
    )
    assert requested.status_code == 202
    reset_token = test_dependencies.admin_auth_email_sender.password_reset_links[0][1]

    confirmed = client.post(
        "/admin/auth/password-reset/confirm",
        headers={
            "X-Request-Id": "req_reset_confirm",
            "User-Agent": "admin-console/reset",
        },
        json={
            "resetToken": reset_token,
            "newPassword": "new-correct-horse-battery-staple",
        },
    )
    assert confirmed.status_code == 204
    assert test_dependencies.admin_auth_uow_factory.enter_count == 1
    assert test_dependencies.admin_auth_uow_factory.commit_count == 1
    assert test_dependencies.admin_auth_uow_factory.rollback_count == 0
    reset_tokens = [
        token
        for token in test_dependencies.admin_auth.auth_tokens.values()
        if token.token_type == "password_reset"
    ]
    assert reset_tokens[-1].status == "consumed"
    assert reset_tokens[-1].last_used_at == test_dependencies.clock.utc_now()
    assert reset_tokens[-1].request_ip is not None
    assert reset_tokens[-1].user_agent == "admin-console/reset"

    login = client.post(
        "/admin/auth/login",
        headers={"X-Request-Id": "req_admin_auth_new"},
        json={
            "email": "ops@example.com",
            "password": "new-correct-horse-battery-staple",
        },
    )
    assert login.status_code == 202
    [record] = _admin_auth_access_records(caplog, "password_reset_confirm")
    assert record.payment_auth_result == "succeeded"
    assert record.payment_admin_id == "admin_1"
    assert record.payment_request_id == "req_reset_confirm"
    assert record.payment_user_agent == "admin-console/reset"


def test_admin_password_reset_request_rejects_invalid_email_as_400(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)

    invalid_payloads = [
        {},
        {"email": 123},
        {"email": "invalid-email"},
        {"email": "ops@"},
    ]

    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            "/admin/auth/password-reset/request",
            headers={"X-Request-Id": f"req_reset_invalid_{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_admin_password_reset_request_rate_limits_repeated_email(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)

    for index in range(3):
        response = client.post(
            "/admin/auth/password-reset/request",
            headers={"X-Request-Id": f"req_reset_rate_{index}"},
            json={"email": "missing@example.com"},
        )
        assert response.status_code == 202

    limited = client.post(
        "/admin/auth/password-reset/request",
        headers={"X-Request-Id": "req_reset_rate_limited"},
        json={"email": "missing@example.com"},
    )

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert test_dependencies.admin_auth_email_sender.password_reset_links == []


def test_admin_password_reset_rejects_reused_password(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)

    requested = client.post(
        "/admin/auth/password-reset/request",
        headers={"X-Request-Id": "req_reset"},
        json={"email": "ops@example.com"},
    )
    assert requested.status_code == 202
    reset_token = test_dependencies.admin_auth_email_sender.password_reset_links[0][1]

    rejected = client.post(
        "/admin/auth/password-reset/confirm",
        headers={"X-Request-Id": "req_reset_confirm"},
        json={
            "resetToken": reset_token,
            "newPassword": "correct-horse-battery-staple",
        },
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "bad_request"
    reset_tokens = [
        token
        for token in test_dependencies.admin_auth.auth_tokens.values()
        if token.token_type == "password_reset"
    ]
    assert reset_tokens[-1].status == "active"


def test_admin_password_reset_confirm_rejects_invalid_contract_values_as_400(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)
    requested = client.post(
        "/admin/auth/password-reset/request",
        headers={"X-Request-Id": "req_reset_policy"},
        json={"email": "ops@example.com"},
    )
    assert requested.status_code == 202
    reset_token = test_dependencies.admin_auth_email_sender.password_reset_links[0][1]

    invalid_payloads = [
        {},
        {"resetToken": 123, "newPassword": "new-correct-horse-battery-staple"},
        {"resetToken": "invalid-token", "newPassword": "new-correct-horse"},
        {"resetToken": reset_token, "newPassword": "short"},
        {"resetToken": reset_token, "newPassword": "123456789012"},
        {"resetToken": reset_token, "newPassword": 123},
    ]

    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            "/admin/auth/password-reset/confirm",
            headers={"X-Request-Id": f"req_reset_confirm_invalid_{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"

    missing = client.post(
        "/admin/auth/password-reset/confirm",
        headers={"X-Request-Id": "req_reset_confirm_missing"},
        json={"resetToken": "apr_missing", "newPassword": "new-correct-horse"},
    )

    assert missing.status_code == 401


def test_admin_login_locks_after_repeated_failures(
    client,
    test_dependencies,
    caplog,
) -> None:
    _seed_admin(test_dependencies)
    caplog.set_level(logging.INFO)

    response = None
    for index in range(5):
        response = client.post(
            "/admin/auth/login",
            headers={"X-Request-Id": f"req_admin_auth_failed_{index}"},
            json={"email": "ops@example.com", "password": "wrong-password"},
        )

    assert response is not None
    assert response.status_code == 401
    locked = client.post(
        "/admin/auth/login",
        headers={"X-Request-Id": "req_admin_auth"},
        json={
            "email": "ops@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    assert locked.status_code == 423
    failure_records = _admin_auth_access_records(caplog, "login_link_request")
    assert [record.payment_auth_result for record in failure_records] == [
        "failed",
        "failed",
        "failed",
        "failed",
        "failed",
        "failed",
    ]
    assert failure_records[0].payment_admin_id == "admin_1"
    assert failure_records[0].payment_request_id == "req_admin_auth_failed_0"


def test_admin_login_returns_locked_for_disabled_account(
    client,
    test_dependencies,
) -> None:
    _seed_admin(test_dependencies)
    test_dependencies.admin_auth.admin_accounts["admin_1"].status = "disabled"

    response = client.post(
        "/admin/auth/login",
        headers={"X-Request-Id": "req_admin_auth_disabled"},
        json={
            "email": "ops@example.com",
            "password": "correct-horse-battery-staple",
        },
    )

    assert response.status_code == 423
    assert response.json()["error"]["code"] == "account_locked"


def _seed_admin(test_dependencies) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    test_dependencies.admin_auth.admin_accounts["admin_1"] = AdminAccount(
        id="admin_1",
        email="ops@example.com",
        email_lower="ops@example.com",
        password_hash=hash_admin_password("correct-horse-battery-staple"),
        display_name="운영 담당자",
        status="active",
        roles=["operator"],
        permissions=[
            "payment_read",
            "payment_cancel",
            "subscription_adjust",
        ],
        permission_version=1,
        failed_login_count=0,
        created_at=now,
        updated_at=now,
    )


def _admin_access_payload(token: str) -> dict[str, object]:
    encoded_body = token.removeprefix("aat_").split(".", 1)[0]
    padded_body = encoded_body + "=" * (-len(encoded_body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded_body).decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _admin_auth_access_records(caplog, event: str):
    return [
        record
        for record in caplog.records
        if record.message == "admin_auth_access"
        and record.payment_auth_event == event
    ]
