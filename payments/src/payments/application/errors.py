from __future__ import annotations


class PaymentApplicationError(Exception):
    """결제 애플리케이션 오류 기본 클래스."""


class AuthenticationError(PaymentApplicationError):
    """내부 서비스 인증에 실패했을 때 발생합니다."""


class AuthorizationError(PaymentApplicationError):
    """요청자가 리소스 소유자가 아니거나 사용자 컨텍스트가 없을 때 발생합니다."""


class ResourceNotFoundError(PaymentApplicationError):
    """요청한 결제 리소스를 찾을 수 없을 때 발생합니다."""


class InvalidStateTransitionError(PaymentApplicationError):
    """현재 상태에서 허용되지 않는 결제 상태 전이가 요청됐을 때 발생합니다."""


class IdempotencyConflictError(PaymentApplicationError):
    """같은 멱등성 키가 다른 요청 payload에 사용됐을 때 발생합니다."""
