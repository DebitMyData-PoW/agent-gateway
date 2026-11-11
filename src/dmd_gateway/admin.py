"""dmd-gateway-admin: operator commands, run on the gateway's server.

    dmd-gateway-admin did show ID
    dmd-gateway-admin did suspend ID --reason "..." --by NAME
    dmd-gateway-admin did reactivate ID --reason "..." --by NAME
    dmd-gateway-admin did revoke ID --reason "..." --by NAME     permanent: asks to confirm
    dmd-gateway-admin names check     the issued DIDs whose name the quote check would refuse

ID is the DID, its did.json address, the identity ID (dmd_identity_...) or the registration ID
(dmd_did_intent_...). On the servers, run it as root (``sudo dmd-gateway-admin ...``): a small
wrapper loads the gateway's settings and runs it as the gateway user.

Every change is written to the audit trail with the reason and your name, and the monitor
sends an alert for it within two minutes. Never put secrets or personal data in a reason.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dmd_gateway.db.models import AgentIdentity, Credential, Entitlement
from dmd_gateway.db.session import build_engine, session_factory, transaction
from dmd_gateway.identity.names import NameRefused, brand_owners, check_display_name, is_brand_owner
from dmd_gateway.services.identity_status import (
    STATUS_CHANGED,
    StatusChangeError,
    change_status,
    find_identity,
    history,
)
from dmd_gateway.settings import get_settings

DONE = {"suspended": "Suspended", "active": "Reactivated", "revoked": "Revoked"}
PUBLIC = {
    "suspended": "Its DID-LLM document now answers 410 Gone until it is reactivated.",
    "active": "Its DID-LLM document resolves again.",
    "revoked": (
        "Its DID-LLM document answers 410 Gone for good, and its credentials show as revoked."
    ),
}


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="dmd-gateway-admin", description="DebitMyData Agent Gateway operator commands"
    )
    groups = top.add_subparsers(dest="group", required=True)
    did = groups.add_parser("did", help="show, suspend, reactivate or revoke a DID-LLM")
    commands = did.add_subparsers(dest="command", required=True)
    show = commands.add_parser(
        "show", help="a DID-LLM's status, entitlements, credentials, history"
    )
    show.add_argument(
        "id", help="the DID-LLM, its did.json address, identity ID or registration ID"
    )
    for name, text in (
        ("suspend", "suspend a DID-LLM (temporary: it stops resolving until reactivated)"),
        ("reactivate", "make a suspended DID-LLM active again"),
        ("revoke", "revoke a DID-LLM for good, with its credentials and entitlements"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "id", help="the DID-LLM, its did.json address, identity ID or registration ID"
        )
        command.add_argument(
            "--reason", required=True, help="why (3 to 300 characters, no secrets)"
        )
        command.add_argument("--by", required=True, help="your name, kept in the audit trail")
        if name == "revoke":
            command.add_argument("--yes", action="store_true", help="do not ask to confirm")
    names = groups.add_parser("names", help="check the names of the DID-LLMs already issued")
    names.add_subparsers(dest="command", required=True).add_parser(
        "check", help="list the active and suspended DID-LLMs whose name a quote would refuse today"
    )
    return top


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = get_settings()
    engine = build_engine(settings)
    try:
        return run(
            args, session_factory(engine), brand=brand_owners(settings.brand_name_controllers)
        )
    finally:
        engine.dispose()


def run(
    args: argparse.Namespace,
    factory: sessionmaker[Session],
    *,
    out: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
    now: datetime | None = None,
    brand: frozenset[str] = frozenset(),
) -> int:
    if args.group == "names":
        with transaction(factory) as session:
            return _check_names(session, brand, out)
    try:
        if args.command == "show":
            with transaction(factory) as session:
                _show(session, args.id, out)
            return 0
        if args.command == "revoke" and not args.yes:
            with transaction(factory) as session:
                identity = find_identity(session, args.id)
                code = identity.id[-6:]
                out(f"Revoke {identity.display_name!r}, now {identity.status}:")
                out(f"  {identity.public_identifier}")
                out(
                    "This is permanent: the DID-LLM, its credentials and its entitlements can "
                    "never be"
                )
                out("made active again.")
            try:
                typed = ask(f"Type {code} to confirm: ")
            except EOFError:
                typed = ""
            if typed.strip() != code:
                out("Not confirmed: nothing was changed.")
                return 1
        with transaction(factory) as session:
            change = change_status(
                session,
                args.id,
                args.command,
                reason=args.reason,
                operator=args.by,
                now=now or datetime.now(UTC),
            )
    except StatusChangeError as exc:
        out(str(exc))
        return 1
    out(f"{DONE[change.new]}: {change.display_name!r} ({change.did})")
    out(
        f"  was {change.old}, now {change.new}; entitlements changed: "
        f"{change.entitlements_changed}; credentials revoked: {change.credentials_revoked}."
    )
    out(f"  {PUBLIC[change.new]}")
    out(f"  Audit event {change.audit_event_id}. The monitor sends an alert within 2 minutes.")
    return 0


def _show(session: Session, text: str, out: Callable[[str], None]) -> None:
    identity: AgentIdentity = find_identity(session, text)
    controller = identity.controller_public_key or ""
    if identity.controller_wallet_address:
        wallet = f"{identity.controller_network}:{identity.controller_wallet_address}"
        controller = f"{controller} and {wallet}" if controller else wallet
    entitlements = session.scalars(
        select(Entitlement).where(Entitlement.subject_id == identity.id).order_by(Entitlement.id)
    ).all()
    credentials = session.scalars(
        select(Credential)
        .where(Credential.identity_id == identity.id)
        .order_by(Credential.issued_at)
    ).all()
    rows = [
        ("DID-LLM", identity.public_identifier),
        ("Identity", identity.id),
        ("Name", identity.display_name),
        ("Status", identity.status),
        ("Controller", controller),
        ("Registration", identity.registration_intent_id),
        ("Issued", _when(identity.created_at)),
        ("Entitlements", ", ".join(f"{e.entitlement} ({e.status})" for e in entitlements) or "-"),
        (
            "Credentials",
            ", ".join(f"{c.id} ({'revoked' if c.revoked_at else 'active'})" for c in credentials)
            or "-",
        ),
    ]
    for label, value in rows:
        out(f"{label:<13} {value}")
    out("History")
    for event in history(session, identity):
        if event.action == STATUS_CHANGED:
            data = event.data
            word = DONE.get(str(data.get("to")), str(data.get("to"))).lower()
            line = f"{word} (was {data.get('from')}) by {event.actor_id}: {data.get('reason')}"
        else:
            line = f"{event.action} ({event.actor_type})"
        out(f"  {_when(event.occurred_at)}  {line}")


def _check_names(session: Session, brand: frozenset[str], out: Callable[[str], None]) -> int:
    identities = session.scalars(
        select(AgentIdentity)
        .where(AgentIdentity.status != "revoked")
        .order_by(AgentIdentity.created_at)
    ).all()
    refused = []
    for identity in identities:
        owner = is_brand_owner(
            brand, identity.controller_public_key, identity.controller_wallet_address
        )
        try:
            check_display_name(identity.display_name, brand_owner=owner)
        except NameRefused as exc:
            refused.append((identity, str(exc)))
    if not refused:
        out(f"Checked {len(identities)} DID-LLMs: every name passes.")
        return 0
    out(f"Checked {len(identities)} DID-LLMs: {len(refused)} would be refused today.")
    for identity, reason in refused:
        out(f"  {identity.id} ({identity.status}) {identity.display_name!r}")
        out(f"    {reason}")
    out('To suspend one: dmd-gateway-admin did suspend <identity> --reason "..." --by "<you>"')
    return 0


def _when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
