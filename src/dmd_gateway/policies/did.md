---
title: DID policy
version: 2026-10-25
status: draft
summary: What a DebitMyData DID is and is not, how control is proven, what is public, and how a DID can be suspended or revoked.
---

# DID policy

## What you receive

When your registration is paid and your controller is proven, DebitMyData issues:

- **a DID**, a decentralized identifier of the form
  `did:web:{{ host }}:agents:<id>`. Its DID document is published at
  `{{ base_url }}/agents/<id>/did.json`, so any standard DID resolver can read it;
- **a signed credential**, a W3C Verifiable Credential that is also your receipt: it names
  the DID, its controllers, the registration and the payment. It is signed by DebitMyData
  (`{{ issuer_did }}`, key published at `{{ base_url }}/.well-known/did.json`), so anyone
  can check it without asking us;
- **the `dmd_did_holder` entitlement**, which other DebitMyData services can rely on.

## What a DID means, and what it does not

A DebitMyData DID shows that a participant registered with DebitMyData, paid the
registration fee, and proved control of the key or wallet named in its DID document.

It does **not** show:

- who a person or organization really is. DebitMyData does not verify identities, and the
  display name is chosen by the registrant;
- that DebitMyData endorses the participant, its owner or what it does;
- any ownership of DMD, any investment or any right to profits.

## Controllers and proof of control

A DID's controller is an Ed25519 public key, an EVM wallet (an ordinary wallet or a smart
wallet), or both. Before the DID is issued, each one must sign a one-time challenge: a short
message that names the gateway, the registration and the controller, and is valid for
{{ challenge_minutes }} minutes.

- You keep your private keys. **DebitMyData never asks for a private key, a seed phrase or
  access to your wallet**, and no DebitMyData service needs one. Anyone who asks for them
  is not DebitMyData.
- A wrong signature uses up its challenge; ask for a new one.
- Keep your keys backed up. Changing a DID's controller is not available yet: if you lose
  your key or wallet, contact us; you may need a new registration.

## What is public

These are published so that anyone can resolve and check a DID:

- the DID document: the DID, its controllers (public key and/or wallet address) and a link
  to its record;
- the identity record: display name, participant type, controllers, status, registration
  ID, credentials and entitlements;
- the credential: all of the above, plus the payment it records (asset, network, amount
  and transaction IDs).

Do not put personal information in a display name unless you want it to be public. The
metadata you may send with a quote is not published. See the
[privacy policy]({{ base_url }}/policies/privacy).

## Status: active, suspended, revoked

A DID is **active** when issued. DebitMyData may change its status:

- **Suspended** (temporary), for example while a reversed payment is unpaid, while a
  reported misuse or compromised key is investigated, or at the holder's request.
- **Revoked** (permanent), for example for fraud, impersonation, abuse of DebitMyData
  services, a legal obligation, or at the holder's request after a key is compromised.

A DID that is not active answers **410 Gone** when resolved. Its history is kept: records
are never deleted or rewritten. Where we can, we tell the holder why a DID was suspended or
revoked and how to contest it, by writing to info@debitmydata.com.

## One DID per registration

A DID belongs to the registration it was issued for and cannot be sold or transferred. Each
registration produces one DID; to register another participant, request a new quote.

## Acceptable use

Do not use a DebitMyData DID to impersonate another person, organization or agent, to
mislead people about who controls an agent, or for anything unlawful. See the
[terms]({{ base_url }}/policies/terms).

## Test DIDs

DIDs issued by the staging server are test DIDs. They are not valid on the production
service, and staging data may be deleted at any time.
