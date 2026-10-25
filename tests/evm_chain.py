"""A local EVM chain (anvil, from Foundry) with test smart-contract wallets.

Used by the smart-wallet tests, which are skipped when anvil is not installed. The wallets
are tests/contracts/TestWallet.sol: an ERC-1271 wallet whose valid signatures are its
owner's, deployed with CREATE2 by TestWalletFactory so that its address is known before it
exists (what ERC-6492 signatures are for).
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from dmd_gateway.identity.wallets import ERC6492_SUFFIX

ANVIL = shutil.which("anvil")
CONTRACTS = json.loads(
    (Path(__file__).parent / "contracts" / "test_wallet.json").read_text(encoding="utf-8")
)


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LocalChain:
    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx.Client(trust_env=False, timeout=10)
        self._factory: str | None = None

    def rpc(self, method: str, params: list[Any]) -> Any:
        body = self._http.post(
            self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    def _send(self, transaction: dict[str, str]) -> dict[str, Any]:
        sender = self.rpc("eth_accounts", [])[0]
        tx = self.rpc(
            "eth_sendTransaction", [{"from": sender, "gas": hex(3_000_000), **transaction}]
        )
        for _ in range(100):
            receipt = self.rpc("eth_getTransactionReceipt", [tx])
            if receipt:
                return dict(receipt)
            time.sleep(0.05)
        raise RuntimeError("transaction not mined")

    @property
    def factory(self) -> str:
        if self._factory is None:
            receipt = self._send({"data": CONTRACTS["TestWalletFactory"]["bytecode"]})
            self._factory = to_checksum_address(receipt["contractAddress"])
        return self._factory

    def _deploy_call(self, owner: str, salt: bytes) -> bytes:
        return _selector("deploy(address,bytes32)") + abi_encode(
            ["address", "bytes32"], [owner, salt]
        )

    def wallet_address(self, owner: str, salt: bytes) -> str:
        data = _selector("addressOf(address,bytes32)") + abi_encode(
            ["address", "bytes32"], [owner, salt]
        )
        result = self.rpc("eth_call", [{"to": self.factory, "data": "0x" + data.hex()}, "latest"])
        return to_checksum_address("0x" + result[-40:])

    def deploy_wallet(self, owner: str, salt: bytes) -> str:
        self._send({"to": self.factory, "data": "0x" + self._deploy_call(owner, salt).hex()})
        address = self.wallet_address(owner, salt)
        assert self.code(address) not in ("0x", "")
        return address

    def counterfactual_signature(self, owner: str, salt: bytes, signature: bytes) -> bytes:
        """An ERC-6492 signature for the not-yet-deployed wallet of ``owner``."""
        return (
            abi_encode(
                ["address", "bytes", "bytes"],
                [self.factory, self._deploy_call(owner, salt), signature],
            )
            + ERC6492_SUFFIX
        )

    def code(self, address: str) -> str:
        return str(self.rpc("eth_getCode", [address, "latest"]))


@contextmanager
def local_chain(chain_id: int) -> Iterator[LocalChain]:
    assert ANVIL is not None
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 (fixed arguments)
        [ANVIL, "--port", str(port), "--chain-id", str(chain_id), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    chain = LocalChain(f"http://127.0.0.1:{port}")
    try:
        for _ in range(100):
            try:
                chain.rpc("eth_chainId", [])
                break
            except (httpx.HTTPError, RuntimeError):
                time.sleep(0.1)
        else:
            raise RuntimeError("anvil did not start")
        yield chain
    finally:
        process.terminate()
        process.wait(timeout=10)
