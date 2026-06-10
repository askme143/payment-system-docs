from typing import Protocol

from payments.domain.entities.checkout import Checkout


class CheckoutRepository(Protocol):
    def find_checkout(self) -> Checkout: ...
