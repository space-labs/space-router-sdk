"""High-level SPACE payment client for SpaceRouter Consumers.

Usage:
    consumer = SpaceRouterSPACE(
        gateway_url="http://gateway:8081",
        proxy_url="http://gateway:8080",
        private_key="0x...",
        chain_id=102031,
        escrow_contract="0x...",
    )

    # 1. Get challenge
    challenge = await consumer.request_challenge()

    # 2. Build auth headers
    headers = consumer.build_auth_headers(challenge)

    # 3. Make proxied request with those headers
    # (via httpx proxy or direct connection)
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from spacerouter.payment.client_wallet import ClientPaymentWallet
from spacerouter.payment.eip712 import EIP712Domain, Receipt

logger = logging.getLogger(__name__)


class SpaceRouterSPACE:
    """High-level Consumer client for SPACE-token proxy payments.

    Parameters
    ----------
    gateway_url : str
        Management API URL (e.g., ``http://gateway:8081``) for /auth/challenge.
    proxy_url : str
        Proxy endpoint URL (e.g., ``http://gateway:8080``).
    private_key : str
        Consumer's wallet private key.
    chain_id : int
        Creditcoin chain ID (102031 for testnet).
    escrow_contract : str
        TokenPaymentEscrow proxy address.
    domain_name : str
        EIP-712 domain name (default: ``TokenPaymentEscrow``).
    domain_version : str
        EIP-712 domain version (default: ``1``).
    max_rate_per_gb : int, optional
        Maximum acceptable rate per GB (reject receipts above this).
    """

    def __init__(
        self,
        gateway_url: str,
        proxy_url: str,
        private_key: str,
        chain_id: int = 102031,
        escrow_contract: str = "",
        domain_name: str = "TokenPaymentEscrow",
        domain_version: str = "1",
        max_rate_per_gb: Optional[int] = None,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.proxy_url = proxy_url.rstrip("/")
        self.wallet = ClientPaymentWallet(private_key)
        self.domain = EIP712Domain(
            name=domain_name,
            version=domain_version,
            chain_id=chain_id,
            verifying_contract=escrow_contract,
        )
        self.max_rate_per_gb = max_rate_per_gb

    @property
    def address(self) -> str:
        return self.wallet.address

    async def request_challenge(self) -> str:
        """Request a one-time challenge from the gateway.

        Returns the challenge string.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.gateway_url}/auth/challenge",
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["challenge"]

    def build_auth_headers(self, challenge: str) -> dict[str, str]:
        """Build proxy request headers for SPACE payment authentication."""
        return self.wallet.build_auth_headers(challenge)

    def sign_receipt(self, receipt: Receipt) -> str:
        """Sign a receipt received from the gateway after a proxy request."""
        return self.wallet.sign_receipt(receipt, self.domain)

    def validate_receipt(self, receipt: Receipt) -> tuple[bool, list[str]]:
        """Validate a receipt from the gateway.

        Returns (is_valid, list_of_errors).
        """
        errors = []

        # Check client address matches our wallet
        if receipt.client_address.lower() != self.address.lower():
            errors.append(
                f"clientAddress mismatch: expected {self.address}, got {receipt.client_address}"
            )

        # Check price is reasonable
        if receipt.total_price < 0:
            errors.append("totalPrice is negative")

        if self.max_rate_per_gb is not None and receipt.data_amount > 0:
            gb = 1024 ** 3
            effective_rate = (receipt.total_price * gb) // receipt.data_amount
            if effective_rate > self.max_rate_per_gb:
                errors.append(
                    f"Effective rate {effective_rate} exceeds max {self.max_rate_per_gb}"
                )

        return len(errors) == 0, errors
