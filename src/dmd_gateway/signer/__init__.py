"""The credential signer (ID-15): the only process that holds DebitMyData's issuer key.

In production the API never holds the key that signs DID credentials and donation receipts.
A separate service, ``dmd-gateway-signer``, runs under its own system user, reads the key,
and answers signing requests on a local Unix socket that only the gateway's user can reach.
It has no network access at all.

Before it signs, the signer checks the facts in the gateway's database itself, through a
read-only database role: a registration credential only for a settled payment and a verified
controller proof, once per registration; a donation receipt only for a settled donation, once.
So even a fully compromised API server can neither take the key away nor have it sign a
credential for something that was not paid.

    service.py   the signer service (``dmd-gateway-signer``)
    policy.py    what it agrees to sign
    client.py    ``RemoteIssuer``, the API's side of the socket
    wire.py      the message format: one JSON object per line
"""
