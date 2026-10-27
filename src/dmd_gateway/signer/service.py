"""dmd-gateway-signer: signs DID credentials and donation receipts for the gateway (ID-15).

Runs as its own system user, with no network access, and listens on a Unix socket that only
the gateway's user can open. Settings come from the environment (``/etc/dmd-signer/signer.env``
in production):

    DMD_SIGNER_KEY_PATH        the issuer key (Ed25519 PEM, readable by the signer only)
    DMD_SIGNER_SOCKET          the socket to listen on, e.g. /run/dmd-signer/signer.sock
    DMD_SIGNER_DATABASE_URL    the gateway's database through a read-only role, e.g.
                               postgresql+psycopg://dmd_signer@/agent_gateway?host=/var/run/postgresql
    DMD_SIGNER_ISSUER_DID      the issuer's DID, e.g. did:web:api.debitmydata.io

Every signature and every refusal is logged (journalctl -u dmd-gateway-signer).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import socketserver
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.identity.issuer import IssuerKey, IssuerKeyError, load_issuer_key
from dmd_gateway.observability import configure_logging
from dmd_gateway.signer import policy
from dmd_gateway.signer.wire import WireError, encode, read_message

log = logging.getLogger("dmd_gateway.signer")
REQUEST_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Signer:
    key: IssuerKey
    sessions: sessionmaker[Session]

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "describe":
            return {
                "ok": True,
                "did": self.key.did,
                "public_key_multibase": self.key.public_key_multibase,
            }
        if op != "sign":
            return {"ok": False, "error": "unknown operation"}
        document, created = request.get("document"), request.get("created")
        if not isinstance(document, dict) or not isinstance(created, str):
            return {"ok": False, "error": "a sign request needs a document and created"}
        try:
            moment = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return {"ok": False, "error": "created is not a date and time"}
        if moment.tzinfo is None:
            return {"ok": False, "error": "created needs a time zone"}
        try:
            with self.sessions() as session, session.begin():
                facts = policy.check(session, document, self.key.did)
        except policy.Refused as exc:
            log.warning("signature refused", extra={"event": {"reason": str(exc)}})
            return {"ok": False, "error": f"refused: {exc}"}
        except SQLAlchemyError as exc:
            log.error("database unavailable", extra={"event": {"error": type(exc).__name__}})
            return {"ok": False, "error": "the signer cannot check the database"}
        signed = self.key.sign(document, created=moment)
        log.info("signed", extra={"event": facts})
        return {"ok": True, "document": signed}


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(REQUEST_TIMEOUT_SECONDS)
        try:
            request = read_message(sock)
            response = self.server.signer.handle(request)
        except (WireError, OSError) as exc:
            response = {"ok": False, "error": f"bad request: {exc}"}
        except Exception:  # noqa: BLE001 - answered and logged, the signer keeps running
            log.exception("signer error")
            response = {"ok": False, "error": "internal signer error"}
        try:
            sock.sendall(encode(response))
        except OSError:
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, signer: Signer) -> None:
        self.signer = signer
        super().__init__(path, _Handler)


def serve(signer: Signer, path: Path, *, mode: int = 0o660) -> _Server:
    """Bind the socket (replacing a stale one) and return the server, not yet serving."""
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError(f"{path} exists and is not a socket")
        path.unlink()
    old_umask = os.umask(0o117)
    try:
        server = _Server(str(path), signer)
    finally:
        os.umask(old_umask)
    os.chmod(path, mode)
    return server


def build(environ: Mapping[str, str]) -> tuple[Signer, Path, Engine]:
    missing = [
        name
        for name in (
            "DMD_SIGNER_KEY_PATH",
            "DMD_SIGNER_SOCKET",
            "DMD_SIGNER_DATABASE_URL",
            "DMD_SIGNER_ISSUER_DID",
        )
        if not environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError("missing settings: " + ", ".join(missing))
    did = environ["DMD_SIGNER_ISSUER_DID"].strip()
    if not did.startswith("did:web:"):
        raise RuntimeError("DMD_SIGNER_ISSUER_DID must be a did:web identifier")
    key = load_issuer_key(Path(environ["DMD_SIGNER_KEY_PATH"].strip()), did)
    engine = create_engine(
        environ["DMD_SIGNER_DATABASE_URL"].strip(),
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        connect_args={"connect_timeout": 3},
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    return Signer(key=key, sessions=sessions), Path(environ["DMD_SIGNER_SOCKET"].strip()), engine


def main(argv: list[str] | None = None) -> int:
    configure_logging(os.environ.get("DMD_SIGNER_LOG_LEVEL", "INFO"))
    try:
        signer, path, engine = build(os.environ)
    except (RuntimeError, IssuerKeyError) as exc:
        log.error("signer not started", extra={"event": {"error": str(exc)}})
        print(f"dmd-gateway-signer: {exc}", file=sys.stderr)
        return 2
    server = serve(signer, path)

    def _stop(*_: object) -> None:
        raise KeyboardInterrupt  # systemd stops the service with SIGTERM

    signal.signal(signal.SIGTERM, _stop)
    method = signer.key.verification_method
    log.info("signer listening", extra={"event": {"socket": str(path), "key": method}})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
