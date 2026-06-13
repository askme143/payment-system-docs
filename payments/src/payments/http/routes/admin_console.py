from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from payments.application.admin_catalog import AdminRequestContext
from payments.http.dependencies import HttpDependencies, admin_context_dependency


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/admin/console", tags=["admin-console"])
    require_admin_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        (
            "scheduler_read",
            "scheduler_run",
            "audit_read",
            "product_manage",
            "product_read",
        ),
    )

    @router.get("/login", response_class=HTMLResponse)
    async def login() -> HTMLResponse:
        return _page(
            "관리자 로그인",
            """
<form method="post" action="/admin/auth/login">
  <label>Email <input name="email" type="email" autocomplete="email"></label>
  <button type="submit">로그인</button>
</form>
""",
        )

    @router.get("/login/confirm", response_class=HTMLResponse)
    async def login_confirm() -> HTMLResponse:
        return _page(
            "관리자 로그인 확인",
            '<main data-api="POST /admin/auth/login/confirm"></main>',
        )

    @router.get("", response_class=HTMLResponse)
    async def home(
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> HTMLResponse:
        _ = context
        return _page(
            "어드민 콘솔",
            """
<nav>
  <a href="/admin/console/scheduler-runs">스케쥴러 실행 이력</a>
  <a href="/admin/console/operator-audits">운영자 감사 목록</a>
  <a href="/admin/console/products">상품 관리 목록</a>
</nav>
<main data-api="GET /admin/auth/me"></main>
""",
        )

    @router.get("/scheduler-runs", response_class=HTMLResponse)
    async def scheduler_runs(
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> HTMLResponse:
        _ = context
        return _page(
            "스케쥴러 실행 이력",
            """
<main data-api="GET /admin/scheduler-runs">
  <form method="post" data-api="POST /admin/scheduler-runs"></form>
</main>
""",
        )

    @router.get("/scheduler-runs/{run_id}", response_class=HTMLResponse)
    async def scheduler_run_detail(
        run_id: str,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> HTMLResponse:
        _ = context
        body = (
            f'<main data-run-id="{run_id}" '
            'data-api="GET /admin/scheduler-runs/{runId}"></main>'
        )
        return _page(
            "스케쥴러 실행 상세",
            body,
        )

    @router.get("/operator-audits", response_class=HTMLResponse)
    async def operator_audits(
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> HTMLResponse:
        _ = context
        return _page(
            "운영자 감사 목록",
            '<main data-api="GET /admin/operator-audits"></main>',
        )

    @router.get("/operator-audits/{audit_id}", response_class=HTMLResponse)
    async def operator_audit_detail(
        audit_id: str,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> HTMLResponse:
        _ = context
        body = (
            f'<main data-audit-id="{audit_id}" '
            'data-api="GET /admin/operator-audits/{auditId}"></main>'
        )
        return _page(
            "운영자 감사 상세",
            body,
        )

    @router.get("/products", response_class=HTMLResponse)
    async def products(
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> HTMLResponse:
        _ = context
        return _page(
            "상품 관리 목록",
            '<main data-api="GET /admin/products"></main>',
        )

    @router.get("/products/{product_id}", response_class=HTMLResponse)
    async def product_detail(
        product_id: str,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> HTMLResponse:
        _ = context
        body = (
            f'<main data-product-id="{product_id}" '
            'data-api="GET /admin/products/{productId}"></main>'
        )
        return _page(
            "상품 상세 및 관리",
            body,
        )

    return router


def _page(title: str, body: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
</head>
<body>
  <header><h1>{title}</h1></header>
  {body}
</body>
</html>"""
    return HTMLResponse(html)
