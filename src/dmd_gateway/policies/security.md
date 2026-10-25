---
title: Security
version: 2026-10-25
status: draft
summary: How the gateway protects payments and identities, how to check you are talking to it, and how to report a vulnerability.
---

# Security

## How to recognize the real gateway

- The gateway's only address is `{{ base_url }}`, over HTTPS.
- **DebitMyData never asks for a private key, a seed phrase, a password or access to your
  wallet** — not by email, chat, phone or through an agent. Anyone who asks is not
  DebitMyData.
- Pay only the amount, asset, network and destination in the quote you requested
  yourself from the gateway.
- Check credentials yourself: they are signed by `{{ issuer_did }}`, whose key is published
  at `{{ base_url }}/.well-known/did.json`.

## How the gateway protects payments and identities

- **No spending keys.** The gateway holds no key that can spend DebitMyData's funds. It
  watches DMD payments through a watch-only wallet and checks USDC payments on the chain;
  the funds go to wallets it cannot spend from.
- **Every payment is checked on the chain** by the gateway itself, with the required
  confirmations, before it counts. A payment report, a facilitator answer or an agent's
  claim is never enough on its own.
- **Exact quotes.** No amount is charged that was not quoted, and no payment is credited to
  more than one registration.
- **Proof of control.** A DID is only issued after each of its controllers has signed a
  one-time challenge that expires after a few minutes.
- **Records that cannot be rewritten.** Payments, proofs and the audit trail are written
  once and never changed.
- **Limits.** Requests are limited per address and in total, and each payment method can
  be switched off on its own at any time.
- **Separate systems.** The DMD nodes' controls are never reachable from the internet.

## Reporting a vulnerability

Please report vulnerabilities privately, never in public issues:

- on GitHub, with **Report a vulnerability** in the **Security** tab of
  [DebitMyData-PoW/agent-gateway](https://github.com/DebitMyData-PoW/agent-gateway); or
- by email to <info@debitmydata.com>, with the subject `Security: DebitMyData Agent Gateway`.

Include what you found, its possible impact, how to reproduce it, and how you would like to
be credited. We will acknowledge your report, keep you informed, and agree a disclosure date
with you once it is fixed.

**Rules.** Test only against your own data, preferably on the staging server. Do not run
denial-of-service, load or social-engineering tests, do not access or change data that is
not yours, and do not test against DebitMyData's production funds or nodes. If you find a
private key, password or other secret, report where it is, not the secret itself, and do
not use it. We will not take action against anyone who reports in good faith and follows
these rules.

## Contact

<info@debitmydata.com>
