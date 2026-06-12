from __future__ import annotations


class PaymentApplicationError(Exception):
    """결제 애플리케이션 오류 기본 클래스."""


class AuthenticationError(PaymentApplicationError):
    """내부 서비스 인증에 실패했을 때 발생합니다."""


class AccountLockedError(PaymentApplicationError):
    """계정 잠금 또는 일시 차단 상태일 때 발생합니다."""


class AuthorizationError(PaymentApplicationError):
    """요청자가 리소스 소유자가 아니거나 사용자 컨텍스트가 없을 때 발생합니다."""


class BadRequestError(PaymentApplicationError):
    """요청 본문 또는 파라미터가 현재 리소스와 맞지 않을 때 발생합니다."""


class ForbiddenError(PaymentApplicationError):
    """인증된 요청자가 대상 리소스 소유자가 아닐 때 발생합니다."""


class ProviderError(PaymentApplicationError):
    """외부 결제 provider 호출 또는 응답 검증에 실패했을 때 발생합니다."""

    def __init__(
        self,
        message: str,
        *,
        provider_code: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.provider_code = provider_code
        self.retryable = retryable


class PaymentRequiredResponseError(PaymentApplicationError):
    """결제 실패를 저장한 뒤 문서화된 402 응답 본문을 반환할 때 발생합니다."""

    def __init__(self, message: str, response_body: dict[str, object]) -> None:
        super().__init__(message)
        self.response_body = response_body


class PaymentConfirmRejectedError(PaymentRequiredResponseError):
    """일반결제 승인 실패를 저장한 뒤 문서화된 실패 본문을 반환할 때 발생합니다."""

    def __init__(self, response_body: dict[str, object]) -> None:
        super().__init__("payment confirmation failed", response_body)


class RateLimitError(PaymentApplicationError):
    """요청 빈도 제한을 초과했을 때 발생합니다."""


class ResourceNotFoundError(PaymentApplicationError):
    """요청한 결제 리소스를 찾을 수 없을 때 발생합니다."""


class InvalidStateTransitionError(PaymentApplicationError):
    """현재 상태에서 허용되지 않는 결제 상태 전이가 요청됐을 때 발생합니다."""


class ConflictResponseError(InvalidStateTransitionError):
    """충돌 상태를 저장된 API 계약의 409 응답 본문으로 반환할 때 발생합니다."""

    def __init__(self, message: str, response_body: dict[str, object]) -> None:
        super().__init__(message)
        self.response_body = response_body


class IdempotencyConflictError(PaymentApplicationError):
    """같은 멱등성 키가 다른 요청 payload에 사용됐을 때 발생합니다."""
