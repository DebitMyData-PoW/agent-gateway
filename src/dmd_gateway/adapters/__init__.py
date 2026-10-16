"""Payment adapters.

``AVAILABLE_ADAPTERS`` lists the adapters that are actually implemented. A rail in the
route registry can only be enabled when its adapter appears here, so having a rail in
configuration never enables it by itself.
"""

from __future__ import annotations

from dmd_gateway.adapters.base import PaymentAdapter

# adapter name (as used in config/payment_routes.yaml) -> implementation
AVAILABLE_ADAPTERS: dict[str, type[PaymentAdapter]] = {}

__all__ = ["AVAILABLE_ADAPTERS", "PaymentAdapter"]
