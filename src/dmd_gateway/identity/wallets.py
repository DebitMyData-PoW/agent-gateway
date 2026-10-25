"""EVM wallets as DID controllers (ID-06): wallet addresses, and the proof that an agent
controls one.

A wallet proves control by signing the challenge message the way every EVM wallet signs a
text message (EIP-191, ``personal_sign``). Three kinds of wallet are accepted:

  * ordinary wallets (EOAs): the address recovered from the signature must be the wallet's
    own address. Checked here, without asking the chain;
  * smart-contract wallets (ERC-1271), such as Coinbase Smart Wallet: the wallet contract
    itself says whether the signature is valid;
  * smart-contract wallets that are not deployed yet (ERC-6492): the signature carries the
    call that would deploy the wallet, and the check simulates that deployment first.

The last two are checked with a single read-only ``eth_call`` on the wallet's network, which
runs ERC-6492's reference validator without deploying it (see ``erc6492.py``). Recovering
the wallet's own address first is safe for contract wallets: no private key exists for a
contract's address, so a recovered signer can never equal it.

Wallets are written as CAIP-10 account IDs, ``eip155:<chain id>:<checksummed address>``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import _hash_eip191_message, encode_defunct
from eth_utils import is_checksum_address, to_checksum_address

from dmd_gateway.adapters.evm_rpc import EvmRpcClient, EvmRpcError, EvmRpcUnavailable
from dmd_gateway.domain.routes import PaymentRouteRegistry
from dmd_gateway.identity.erc6492 import VALIDATE_SIG_OFFCHAIN_BYTECODE

ERC6492_SUFFIX = bytes.fromhex("64926492" * 8)
MAX_SIGNATURE_BYTES = 8192
DEFAULT_NETWORK = "eip155:8453"  # Base

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NETWORK = re.compile(r"^eip155:([1-9][0-9]{0,11})$")
_ACCOUNT = re.compile(r"^(eip155:[1-9][0-9]{0,11}):(0x[0-9a-fA-F]{40})$")

# How each kind of wallet proved control (stored with the proof).
EOA = "eip191_signature"
ERC1271 = "erc1271_signature"
ERC6492 = "erc6492_signature"


class WalletError(ValueError):
    """A wallet address or network that cannot be used as a controller."""


class WalletCheckUnavailable(RuntimeError):
    """The wallet's network could not be asked; the answer is unknown, not negative."""


class SmartWalletUnsupported(RuntimeError):
    """The signature is not an ordinary wallet's, and this gateway has no RPC for the wallet's
    network to ask a smart wallet."""


@dataclass(frozen=True, slots=True)
class Wallet:
    network: str  # CAIP-2, e.g. eip155:8453
    address: str  # EIP-55 checksummed

    @property
    def chain_id(self) -> int:
        return int(self.network.split(":", 1)[1])

    @property
    def account_id(self) -> str:
        """CAIP-10: ``eip155:8453:0xAbC...``."""
        return f"{self.network}:{self.address}"


def checksummed(address: str) -> str:
    """The EIP-55 form of an address; mixed-case input must already be correctly
    checksummed, so a mistyped address is refused instead of silently accepted."""
    if not _ADDRESS.match(address):
        raise WalletError("a wallet address is 0x followed by 40 hexadecimal characters")
    if int(address, 16) == 0:
        raise WalletError("the zero address belongs to no one and cannot be a controller")
    body = address[2:]
    if body != body.lower() and body != body.upper() and not is_checksum_address(address):
        raise WalletError("the wallet address has a wrong checksum (check its capital letters)")
    return to_checksum_address(address)


def split_account(text: str) -> tuple[str | None, str]:
    """``0x...`` or ``eip155:<chain id>:0x...`` -> (network or None, checksummed address)."""
    value = text.strip()
    account = _ACCOUNT.match(value)
    if account:
        return account.group(1), checksummed(account.group(2))
    return None, checksummed(value)


def parse_wallet(
    text: str,
    network: str | None,
    *,
    allowed_networks: Collection[str],
    default_network: str,
) -> Wallet:
    """The wallet named in a quote, on one of the networks this gateway checks wallets on."""
    in_text, address = split_account(text)
    if in_text and network and in_text != network:
        raise WalletError(
            f"controller_network {network} differs from the network in the wallet ({in_text})"
        )
    chosen = in_text or network or default_network
    if not _NETWORK.match(chosen):
        raise WalletError("a network is written eip155:<chain id>, e.g. eip155:8453 for Base")
    if chosen not in allowed_networks:
        raise WalletError(
            f"wallets on {chosen} cannot be controllers here; supported: "
            + ", ".join(sorted(allowed_networks))
        )
    return Wallet(chosen, address)


def wallet_from_account_id(account_id: str) -> Wallet:
    network, address = split_account(account_id)
    if network is None:
        raise WalletError("expected a CAIP-10 account ID, eip155:<chain id>:0x...")
    return Wallet(network, address)


def message_hash(message: str) -> bytes:
    """The EIP-191 hash a wallet signs for a text message."""
    return bytes(_hash_eip191_message(encode_defunct(text=message)))


def decode_wallet_signature(text: str) -> bytes:
    value = text.strip()
    if value.startswith(("0x", "0X")):
        value = value[2:]
    try:
        signature = bytes.fromhex(value)
    except ValueError as exc:
        raise WalletError("a wallet signature is hexadecimal, e.g. 0x1a2b...") from exc
    if not 65 <= len(signature) <= MAX_SIGNATURE_BYTES:
        raise WalletError(
            f"a wallet signature is 65 bytes for an ordinary wallet and at most "
            f"{MAX_SIGNATURE_BYTES} bytes for a smart wallet"
        )
    return signature


def recovered_signer(message: str, signature: bytes) -> str | None:
    """The address that made a 65-byte signature of ``message``, or None."""
    if len(signature) != 65:
        return None
    try:
        return str(Account.recover_message(encode_defunct(text=message), signature=signature))
    except Exception:  # malformed r, s or v: not a signature of this message by anyone
        return None


def controller_networks(registry: PaymentRouteRegistry) -> tuple[list[str], str]:
    """The EVM networks of the approved payment rails, on which wallets can be controllers,
    and the default one (the x402 rail's network)."""
    rails = [r for r in registry.rails.values() if r.approved and _NETWORK.match(r.network)]
    networks = sorted({r.network for r in rails})
    x402 = [r.network for r in rails if r.x402]
    default = x402[0] if x402 else (networks[0] if networks else DEFAULT_NETWORK)
    return networks or [DEFAULT_NETWORK], default


def _is_revert(error: EvmRpcError) -> bool:
    # Nodes report a reverted eth_call as code 3 (with the revert data) or as -32000/-32015
    # with "revert" in the message; anything else is a problem of the node, not an answer.
    return error.code == 3 or "revert" in str(error).lower()


class WalletVerifier:
    """Checks wallet signatures; smart wallets are asked on their own network."""

    def __init__(self, clients: Mapping[int, EvmRpcClient]) -> None:
        self._clients = dict(clients)
        self._checked_chains: set[int] = set()

    @classmethod
    def from_registry(
        cls, registry: PaymentRouteRegistry, environ: Mapping[str, str]
    ) -> WalletVerifier:
        """One chain RPC per network that has one configured: the x402 rails' RPCs."""
        clients: dict[int, EvmRpcClient] = {}
        for rail in registry.rails.values():
            chain_id = rail.evm_chain_id
            url = (environ.get(rail.x402.rpc_url_env) or "").strip() if rail.x402 else ""
            if chain_id is not None and url and chain_id not in clients:
                try:
                    clients[chain_id] = EvmRpcClient(url)
                except ValueError:
                    continue  # refused URL (not https): smart wallets there stay unchecked
        return cls(clients)

    def verify(self, wallet: Wallet, message: str, signature: bytes) -> str | None:
        """How the wallet proved control (EOA, ERC1271 or ERC6492), or None if it did not.

        Raises ``WalletCheckUnavailable`` when the answer needs the wallet's network and the
        network cannot be asked right now, ``SmartWalletUnsupported`` when the gateway has no
        RPC for that network at all.
        """
        counterfactual = signature.endswith(ERC6492_SUFFIX)
        if not counterfactual and recovered_signer(message, signature) == wallet.address:
            return EOA
        client = self._clients.get(wallet.chain_id)
        if client is None:
            raise SmartWalletUnsupported(
                f"smart-wallet signatures on {wallet.network} cannot be checked by this gateway"
            )
        try:
            if wallet.chain_id not in self._checked_chains:
                if client.chain_id() != wallet.chain_id:
                    raise WalletCheckUnavailable(
                        f"the RPC for {wallet.network} is on another chain"
                    )
                self._checked_chains.add(wallet.chain_id)
            arguments = abi_encode(
                ["address", "bytes32", "bytes"],
                [wallet.address, message_hash(message), signature],
            )
            result = client.call(
                "eth_call",
                [{"data": "0x" + (VALIDATE_SIG_OFFCHAIN_BYTECODE + arguments).hex()}, "latest"],
            )
        except EvmRpcUnavailable as exc:
            raise WalletCheckUnavailable(f"{wallet.network}: {exc}") from exc
        except EvmRpcError as exc:
            if _is_revert(exc):
                return None  # the validator reverted: not a valid signature for this wallet
            raise WalletCheckUnavailable(
                f"{wallet.network}: the node could not run the check"
            ) from exc
        if isinstance(result, str) and result.lower() == "0x01":
            return ERC6492 if counterfactual else ERC1271
        return None
