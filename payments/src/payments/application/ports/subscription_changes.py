from __future__ import annotations

from typing import Protocol

from payments.domain.entities.subscription_change_preview import (
    SubscriptionChangePreview,
)


class SubscriptionChangeTokenCodec(Protocol):
    def encode_plan_change_preview(
        self,
        preview: SubscriptionChangePreview,
    ) -> str:
        raise NotImplementedError

    def decode_plan_change_preview(
        self,
        confirmation_token: str,
    ) -> SubscriptionChangePreview | None:
        raise NotImplementedError
