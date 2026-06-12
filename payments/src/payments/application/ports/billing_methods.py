from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

BillingKeyStatus = Literal["active", "revoked"]
BillingMethodStatus = Literal["active", "inactive", "deleted"]


@dataclass(frozen=True, slots=True)
class BillingMethodRecord:
    billing_method_id: str
    status: BillingMethodStatus
    is_default: bool
    method: str
    card_company: str
    masked_card_number: str
    billing_key_status: BillingKeyStatus
    created_at: datetime


class BillingMethodRepository(Protocol):
    async def list_active_billing_methods_for_user(
        self,
        user_id: str,
    ) -> list[BillingMethodRecord]:
        raise NotImplementedError

    async def count_active_subscriptions_for_user(self, user_id: str) -> int:
        raise NotImplementedError

    async def get_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
    ) -> BillingMethodRecord | None:
        raise NotImplementedError

    async def get_any_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
    ) -> BillingMethodRecord | None:
        raise NotImplementedError

    async def get_billing_method_owner(self, billing_method_id: str) -> str | None:
        raise NotImplementedError

    async def set_default_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
        changed_at: datetime,
    ) -> str | None:
        raise NotImplementedError

    async def deactivate_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
        deleted_at: datetime,
    ) -> None:
        raise NotImplementedError
