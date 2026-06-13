from __future__ import annotations


def test_admin_console_login_page_is_server_rendered(client) -> None:
    response = client.get("/admin/console/login")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<h1>관리자 로그인</h1>" in response.text
    assert 'action="/admin/auth/login"' in response.text


def test_admin_console_scheduler_runs_page_requires_admin_and_renders_shell(
    client,
    admin_headers,
) -> None:
    response = client.get("/admin/console/scheduler-runs", headers=admin_headers)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<h1>스케쥴러 실행 이력</h1>" in response.text
    assert 'data-api="GET /admin/scheduler-runs"' in response.text
    assert 'data-api="POST /admin/scheduler-runs"' in response.text


def test_admin_console_products_page_renders_documented_route(
    client,
    admin_headers,
) -> None:
    response = client.get("/admin/console/products", headers=admin_headers)

    assert response.status_code == 200
    assert "<h1>상품 관리 목록</h1>" in response.text
    assert 'data-api="GET /admin/products"' in response.text
