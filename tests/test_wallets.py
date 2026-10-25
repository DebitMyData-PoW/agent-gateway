"""EVM wallets as DID controllers: addresses, networks, and checking wallet signatures.

The smart-wallet checks at the end run on a local chain (anvil) and are skipped without it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from eth_account import Account
from eth_account.messages import encode_defunct

from dmd_gateway.adapters.evm_rpc import EvmRpcClient
from dmd_gateway.domain.routes import PaymentRouteRegistry, load_registry
from dmd_gateway.identity.did import WALLET_CONTEXT, WALLET_METHOD, agent_did_document
from dmd_gateway.identity.wallets import (
    EOA,
    ERC1271,
    ERC6492,
    ERC6492_SUFFIX,
    SmartWalletUnsupported,
    Wallet,
    WalletCheckUnavailable,
    WalletError,
    WalletVerifier,
    checksummed,
    controller_networks,
    decode_wallet_signature,
    parse_wallet,
    recovered_signer,
    split_account,
    wallet_from_account_id,
)

from .conftest import REGISTRY_PATH, REPO_ROOT
from .evm_chain import ANVIL, LocalChain, local_chain

ADDRESS = "0x52908400098527886E0F7030069857D2E4169EE7"  # EIP-55 test vector
BASE = "eip155:8453"
SEPOLIA = "eip155:84532"
MESSAGE = "DebitMyData controller proof\nNonce: test"
OWNER = Account.create()
STRANGER = Account.create()


def registry_at(path: Path) -> PaymentRouteRegistry:
    return load_registry(path, environ={}, available_adapters=set())


def sign(account: Any, message: str = MESSAGE) -> bytes:
    return bytes(account.sign_message(encode_defunct(text=message)).signature)


# --------------------------------------------------------------------------- addresses


def test_addresses_are_checksummed_and_checked() -> None:
    assert checksummed(ADDRESS.lower()) == ADDRESS
    assert checksummed(ADDRESS.upper().replace("0X", "0x")) == ADDRESS
    assert checksummed(ADDRESS) == ADDRESS
    for bad, why in [
        ("0x52908400098527886E0F7030069857D2E4169eE7", "checksum"),  # one letter changed
        ("0x" + "0" * 40, "zero address"),
        ("0x1234", "40 hexadecimal"),
        ("52908400098527886E0F7030069857D2E4169EE7", "40 hexadecimal"),
        ("0x" + "g" * 40, "40 hexadecimal"),
    ]:
        with pytest.raises(WalletError, match=why):
            checksummed(bad)


def test_wallets_are_written_as_caip10_accounts() -> None:
    assert split_account(f"  {ADDRESS.lower()} ") == (None, ADDRESS)
    assert split_account(f"{SEPOLIA}:{ADDRESS.lower()}") == (SEPOLIA, ADDRESS)
    wallet = wallet_from_account_id(f"{BASE}:{ADDRESS}")
    assert (wallet.chain_id, wallet.account_id) == (8453, f"{BASE}:{ADDRESS}")
    with pytest.raises(WalletError, match="CAIP-10"):
        wallet_from_account_id(ADDRESS)


def test_a_wallet_is_on_a_network_the_gateway_supports() -> None:
    allowed = {"allowed_networks": [BASE, SEPOLIA], "default_network": BASE}
    assert parse_wallet(ADDRESS, None, **allowed) == Wallet(BASE, ADDRESS)
    assert parse_wallet(ADDRESS, SEPOLIA, **allowed) == Wallet(SEPOLIA, ADDRESS)
    assert parse_wallet(f"{SEPOLIA}:{ADDRESS}", None, **allowed) == Wallet(SEPOLIA, ADDRESS)
    assert parse_wallet(f"{SEPOLIA}:{ADDRESS}", SEPOLIA, **allowed) == Wallet(SEPOLIA, ADDRESS)
    with pytest.raises(WalletError, match="differs"):
        parse_wallet(f"{SEPOLIA}:{ADDRESS}", BASE, **allowed)
    with pytest.raises(WalletError, match="supported: eip155:8453, eip155:84532"):
        parse_wallet(ADDRESS, "eip155:1", **allowed)


def test_wallet_networks_come_from_the_approved_rails(tmp_path: Path) -> None:
    production = registry_at(REGISTRY_PATH)
    assert controller_networks(production) == ([BASE], BASE)

    # Staging: only the Base Sepolia USDC rail is approved; the wrapped DMD rail on Base
    # mainnet is not, so mainnet wallets are not offered there.
    staging = registry_at(REPO_ROOT / "config" / "payment_routes.regtest.yaml")
    assert controller_networks(staging) == ([SEPOLIA], SEPOLIA)

    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    for rail in data["rails"].values():
        rail["approved"] = False
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert controller_networks(registry_at(path)) == ([BASE], BASE)


# --------------------------------------------------------------------------- signatures


def test_wallet_signatures_are_hex() -> None:
    signature = sign(OWNER)
    assert decode_wallet_signature("0x" + signature.hex()) == signature
    assert decode_wallet_signature(signature.hex().upper()) == signature
    with pytest.raises(WalletError, match="hexadecimal"):
        decode_wallet_signature("0xnot-hex")
    with pytest.raises(WalletError, match="65 bytes"):
        decode_wallet_signature("0x" + "ab" * 64)
    with pytest.raises(WalletError, match="at most 8192"):
        decode_wallet_signature("0x" + "ab" * 8193)


def test_an_ordinary_wallet_signature_names_its_signer() -> None:
    assert recovered_signer(MESSAGE, sign(OWNER)) == OWNER.address
    assert recovered_signer(MESSAGE + " ", sign(OWNER)) != OWNER.address
    assert recovered_signer(MESSAGE, b"\x00" * 65) is None
    assert recovered_signer(MESSAGE, sign(OWNER) + b"\x00") is None


def scripted_rpc(
    answer: Callable[[str, list[Any]], httpx.Response], calls: list[str] | None = None
) -> EvmRpcClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body["method"])
        if body["method"] == "eth_chainId":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x2105"}
            )
        return answer(body["method"], body["params"])

    return EvmRpcClient("https://rpc.test", transport=httpx.MockTransport(handler))


def result(value: str) -> Callable[[str, list[Any]], httpx.Response]:
    return lambda method, params: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "result": value}
    )


def rpc_error(code: int, message: str) -> Callable[[str, list[Any]], httpx.Response]:
    return lambda method, params: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}
    )


def test_ordinary_wallets_are_checked_without_the_chain() -> None:
    calls: list[str] = []
    verifier = WalletVerifier({8453: scripted_rpc(result("0x00"), calls)})
    assert verifier.verify(Wallet(BASE, OWNER.address), MESSAGE, sign(OWNER)) == EOA
    assert calls == []
    # Without any RPC, ordinary wallets still work.
    assert WalletVerifier({}).verify(Wallet(BASE, OWNER.address), MESSAGE, sign(OWNER)) == EOA


def test_other_signatures_are_asked_to_the_wallets_network() -> None:
    wallet = Wallet(BASE, ADDRESS)
    calls: list[str] = []
    verifier = WalletVerifier({8453: scripted_rpc(result("0x01"), calls)})
    assert verifier.verify(wallet, MESSAGE, sign(STRANGER)) == ERC1271
    assert verifier.verify(wallet, MESSAGE, sign(STRANGER) + ERC6492_SUFFIX) == ERC6492
    assert calls == ["eth_chainId", "eth_call", "eth_call"]  # the chain is checked once

    # The answer of the validator decides; a revert is a "no".
    assert (
        WalletVerifier({8453: scripted_rpc(result("0x00"))}).verify(wallet, MESSAGE, sign(STRANGER))
        is None
    )
    for revert in (rpc_error(3, "execution reverted"), rpc_error(-32000, "execution reverted")):
        assert (
            WalletVerifier({8453: scripted_rpc(revert)}).verify(wallet, MESSAGE, sign(STRANGER))
            is None
        )

    # An ERC-6492 signature is never taken for an ordinary one, even by the right key.
    counterfactual = WalletVerifier({8453: scripted_rpc(result("0x00"))})
    owner_wallet = Wallet(BASE, OWNER.address)
    assert counterfactual.verify(owner_wallet, MESSAGE, sign(OWNER) + ERC6492_SUFFIX) is None


def test_an_unanswered_check_is_not_a_no() -> None:
    wallet = Wallet(BASE, ADDRESS)
    with pytest.raises(SmartWalletUnsupported, match="eip155:8453"):
        WalletVerifier({}).verify(wallet, MESSAGE, sign(STRANGER))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    offline = EvmRpcClient("https://rpc.test", transport=httpx.MockTransport(down))
    with pytest.raises(WalletCheckUnavailable):
        WalletVerifier({8453: offline}).verify(wallet, MESSAGE, sign(STRANGER))
    with pytest.raises(WalletCheckUnavailable, match="could not run the check"):
        WalletVerifier({8453: scripted_rpc(rpc_error(-32603, "internal error"))}).verify(
            wallet, MESSAGE, sign(STRANGER)
        )
    limited = scripted_rpc(lambda method, params: httpx.Response(429))
    with pytest.raises(WalletCheckUnavailable, match="rate limit"):
        WalletVerifier({8453: limited}).verify(wallet, MESSAGE, sign(STRANGER))

    def other_chain(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    mainnet = EvmRpcClient("https://rpc.test", transport=httpx.MockTransport(other_chain))
    with pytest.raises(WalletCheckUnavailable, match="another chain"):
        WalletVerifier({8453: mainnet}).verify(wallet, MESSAGE, sign(STRANGER))


def test_the_verifier_uses_the_x402_rails_rpc() -> None:
    registry = registry_at(REGISTRY_PATH)
    verifier = WalletVerifier.from_registry(registry, {"DMD_BASE_RPC_URL": "https://rpc.test"})
    assert set(verifier._clients) == {8453}
    assert WalletVerifier.from_registry(registry, {})._clients == {}
    # A plain-HTTP remote RPC is refused: smart wallets then stay unchecked.
    refused = WalletVerifier.from_registry(registry, {"DMD_BASE_RPC_URL": "http://rpc.test"})
    assert refused._clients == {}


# --------------------------------------------------------------------------- DID document


def test_the_did_document_lists_the_wallet() -> None:
    did = "did:web:example.com:agents:01ABC"
    wallet = Wallet(BASE, ADDRESS)
    document = agent_did_document(did, None, record_url="https://example.com/r", wallet=wallet)
    assert WALLET_CONTEXT in document["@context"]
    (method,) = document["verificationMethod"]
    assert method == {
        "id": f"{did}#wallet",
        "type": WALLET_METHOD,
        "controller": did,
        "blockchainAccountId": f"{BASE}:{ADDRESS}",
    }
    assert document["authentication"] == document["assertionMethod"] == [f"{did}#wallet"]


# --------------------------------------------------------------------------- smart wallets


@pytest.fixture(scope="module")
def chain() -> Iterator[LocalChain]:
    if ANVIL is None:
        pytest.skip("anvil (Foundry) is not installed")
    with local_chain(8453) as running:
        yield running


@pytest.fixture(scope="module")
def verifier(chain: LocalChain) -> WalletVerifier:
    return WalletVerifier({8453: EvmRpcClient(chain.url)})


def test_deployed_smart_wallets_answer_for_themselves(
    chain: LocalChain, verifier: WalletVerifier
) -> None:
    wallet = Wallet(BASE, chain.deploy_wallet(OWNER.address, b"\x01" * 32))
    assert verifier.verify(wallet, MESSAGE, sign(OWNER)) == ERC1271
    assert verifier.verify(wallet, MESSAGE, sign(STRANGER)) is None
    assert verifier.verify(wallet, MESSAGE + "!", sign(OWNER)) is None


def test_undeployed_smart_wallets_are_checked_without_deploying_them(
    chain: LocalChain, verifier: WalletVerifier
) -> None:
    salt = b"\x02" * 32
    address = chain.wallet_address(OWNER.address, salt)
    wallet = Wallet(BASE, address)
    assert chain.code(address) == "0x"
    good = chain.counterfactual_signature(OWNER.address, salt, sign(OWNER))
    bad = chain.counterfactual_signature(OWNER.address, salt, sign(STRANGER))
    assert verifier.verify(wallet, MESSAGE, good) == ERC6492
    assert verifier.verify(wallet, MESSAGE, bad) is None
    # The check only simulated the deployment.
    assert chain.code(address) == "0x"
    # A deployment for another owner does not make the signature valid for this wallet.
    other = chain.counterfactual_signature(STRANGER.address, salt, sign(STRANGER))
    assert verifier.verify(wallet, MESSAGE, other) is None


def test_ordinary_wallets_through_the_chain_check(verifier: WalletVerifier) -> None:
    # A wrong ordinary-wallet signature goes to the chain too, and is refused there.
    assert verifier.verify(Wallet(BASE, OWNER.address), MESSAGE, sign(STRANGER)) is None
