"""SSLCommerz payment gateway client.

Two calls, and the difference between them is the whole security model:

1. **Session init** (`/gwprocess/v4/api.php`) — we post the order and get back a
   `GatewayPageURL`. The student is sent there to pay.
2. **Validation** (`/validator/api/validationserverAPI.php`) — we ask SSLCommerz,
   server to server, what actually happened to a `val_id`.

**Nothing the browser tells us is trusted.** The student returns from the
gateway to `success_url` carrying a `val_id`, but that redirect is just an HTTP
request anyone can forge by typing a URL. It is only a hint that something may
have happened. The ticket is issued on the strength of step 2 — a direct call to
SSLCommerz authenticated with the store password, whose response we compare
against the order's own amount and currency.

That comparison matters as much as the call. A validation response that says
`VALID` for 1.00 BDT against a 100.00 BDT order is a successful attack if you
only check the status field.

The store password is a credential. It lives in the environment, never in the
repository, and never leaves this module — it is not logged and not returned to
any client.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("unitrack.sslcommerz")

SANDBOX_BASE = "https://sandbox.sslcommerz.com"
LIVE_BASE = "https://securepay.sslcommerz.com"

# SSLCommerz reports these for a payment that actually completed. `VALIDATED` is
# returned when a val_id is checked a second time, which happens whenever a
# student refreshes the success page — it must be accepted, not treated as fraud.
SUCCESSFUL_STATUSES = frozenset({"VALID", "VALIDATED"})

# Risk score in the validation response: "1" means SSLCommerz flagged the
# transaction for manual review. Treating that as paid is how chargebacks
# happen, so it is surfaced rather than silently accepted.
RISK_FLAGGED = "1"


class GatewayError(RuntimeError):
    """The gateway could not be reached, or answered something unusable."""


def _base_url() -> str:
    return LIVE_BASE if settings.sslcommerz_live else SANDBOX_BASE


class SslCommerzClient:
    def __init__(self, timeout_s: float = 20.0) -> None:
        self._timeout = timeout_s

    async def create_session(
        self,
        *,
        tran_id: str,
        amount_paisa: int,
        currency: str,
        success_url: str,
        fail_url: str,
        cancel_url: str,
        ipn_url: str | None,
        customer_name: str,
        customer_email: str,
        customer_phone: str | None,
        product_name: str,
    ) -> str:
        """Open a payment session and return the URL to send the student to.

        SSLCommerz wants a decimal string in major units, so paisa are converted
        here rather than at the call site — keeping the integer-only rule true
        everywhere except this one boundary.

        The `cus_*` and `product_*` fields are mandatory even when meaningless;
        omitting them fails the session with an unhelpful message.
        """
        payload: dict[str, str] = {
            "store_id": settings.sslcommerz_store_id,
            "store_passwd": settings.sslcommerz_store_password,
            "total_amount": f"{amount_paisa / 100:.2f}",
            "currency": currency,
            "tran_id": tran_id,
            "success_url": success_url,
            "fail_url": fail_url,
            "cancel_url": cancel_url,
            "shipping_method": "NO",
            "product_name": product_name,
            "product_category": "transport",
            "product_profile": "non-physical-goods",
            "cus_name": customer_name,
            "cus_email": customer_email,
            "cus_phone": customer_phone or "N/A",
            "cus_add1": "N/A",
            "cus_city": "Dhaka",
            "cus_country": "Bangladesh",
        }
        if ipn_url:
            payload["ipn_url"] = ipn_url

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{_base_url()}/gwprocess/v4/api.php", data=payload
                )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayError(f"session init failed: {exc}") from exc

        if body.get("status") != "SUCCESS" or not body.get("GatewayPageURL"):
            # `failedreason` is the gateway's own wording and safe to log; the
            # payload we sent is not, because it carries the store password.
            raise GatewayError(f"session refused: {body.get('failedreason') or body.get('status')}")

        return str(body["GatewayPageURL"])

    async def validate(self, val_id: str) -> dict[str, Any]:
        """Ask SSLCommerz what really happened. This is the source of truth.

        Returns the raw response so the caller can compare amount and currency
        itself and store the whole thing for reconciliation.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{_base_url()}/validator/api/validationserverAPI.php",
                    params={
                        "val_id": val_id,
                        "store_id": settings.sslcommerz_store_id,
                        "store_passwd": settings.sslcommerz_store_password,
                        "format": "json",
                    },
                )
                response.raise_for_status()
                return dict(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayError(f"validation failed: {exc}") from exc


def payment_succeeded(validation: dict[str, Any]) -> bool:
    return str(validation.get("status", "")).upper() in SUCCESSFUL_STATUSES


def amount_matches(validation: dict[str, Any], expected_paisa: int, expected_currency: str) -> bool:
    """Confirm the gateway settled the amount we actually asked for.

    Compared in paisa after rounding, because the response carries a decimal
    string and float equality on money is never reliable. `currency_amount` is
    the figure in the store's own currency, which is the one our order is in.
    """
    try:
        settled_paisa = round(float(validation["currency_amount"]) * 100)
    except (KeyError, TypeError, ValueError):
        return False

    currency = str(validation.get("currency_type") or validation.get("currency") or "").upper()
    return settled_paisa == expected_paisa and currency == expected_currency.upper()


def risk_flagged(validation: dict[str, Any]) -> bool:
    return str(validation.get("risk_level", "")) == RISK_FLAGGED
