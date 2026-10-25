---
title: Terms of service
version: 2026-10-25
status: draft
summary: The terms for using the DebitMyData Agent Gateway and registering a DebitMyData DID.
---

# Terms of service

These terms apply to the DebitMyData Agent Gateway (the "gateway"): its API at
`{{ base_url }}`, its MCP server, and the DIDs, credentials and entitlements it issues. The
gateway is operated by DebitMyData ("we", "us").

## 1. Who agrees to these terms

You agree to these terms when you request a quote, pay for a registration, or otherwise
use the gateway. If an agent, bot or other software uses the gateway, **the person or
organization that runs it** agrees to these terms and is responsible for what it does.

You must be able to enter into a binding agreement, and be of legal age where you live if
you are a person. You must not use the gateway if you are subject to sanctions that forbid
dealing with us, or if using it is unlawful where you are.

## 2. The service

The gateway lets agents and other participants discover DebitMyData services, get a quote,
pay in an approved asset, prove control of a key or wallet, and receive a DebitMyData DID
with a signed credential and entitlements. What a DID means is set out in the
[DID policy]({{ base_url }}/policies/did).

## 3. Prices and payment

- A DID registration costs a fixed 25.00 USD, paid as described in the
  [payment methods]({{ base_url }}/policies/payment-methods).
- Each payment needs a quote. The quoted amount, asset, network and destination apply
  until the quote expires. You are responsible for paying exactly what was quoted, on the
  quoted network, before it expires.
- A payment only counts once the gateway has verified it on the chain.
- Refunds follow the [refund policy]({{ base_url }}/policies/refunds). The registration fee
  is non-refundable.

## 4. Your responsibilities

- **Keys and wallets.** You alone hold and protect your private keys and wallets. We never
  ask for them and cannot recover them.
- **Accurate requests.** Check the asset, network, amount and address before paying.
  Blockchain payments cannot be reversed by us.
- **Official addresses only.** Use only the gateway's official address and the payment
  details in your quote. Treat any other request for payment or keys as fraud.
- **Lawful use.** You are responsible for complying with the laws that apply to you,
  including tax and sanctions laws.

## 5. Acceptable use

Do not:

- impersonate any person, organization or agent, or register a DID to mislead others;
- use the gateway for fraud, money laundering, terrorist financing or any unlawful purpose;
- overload the gateway, get around its request limits, or run load or security tests
  against it without our written permission (see the
  [security page]({{ base_url }}/policies/security) for how to report a vulnerability);
- try to access data, registrations or DIDs that are not yours;
- interfere with other users or with the DebitMyData network.

We may refuse quotes, suspend or revoke DIDs, and block access when these terms are broken,
as set out in the DID policy.

## 6. Availability and changes to the service

The gateway is in active development. We may change it, pause a payment method, or
suspend the service at any time, for example during maintenance or a network incident. We
try to give notice of planned changes, but cannot promise uninterrupted service.

## 7. The staging server

The staging server uses test networks whose coins have no value. It is for testing only:
its DIDs and credentials are not valid on the production service, and its data may be
deleted at any time.

## 8. Risks of blockchains

Blockchain networks can be slow, congested, reorganized or attacked, and their fees
change. The value of DMD and other assets can change quickly. Smart-contract wallets and
tokens can have defects. You accept these risks when you pay with them.

## 9. No warranty

The gateway, its DIDs and its credentials are provided **as is**, without warranties of any
kind, to the extent the law allows.

## 10. Limitation of liability

To the extent the law allows, we are not liable for indirect or consequential losses, lost
profits or lost data, and our total liability for a registration is limited to the fee paid
for it.

## 11. Changes to these terms

Each version of these terms has a date. Every quote names the version that applies to it
(`terms_version`), and that version applies to the registration made from the quote. New
versions apply to quotes made after they are published.

## 12. Governing law

_To be confirmed by DebitMyData before these terms take effect._

## 13. Contact

info@debitmydata.com
