---
title: Payment methods
version: 2026-10-25
status: draft
summary: How a DebitMyData DID registration is priced, quoted and paid, and which assets and networks are accepted.
---

# Payment methods

A DebitMyData DID registration costs a fixed **25.00 USD**. You pay it in one of the
approved assets below, from the wallet you already use. You never have to buy DMD to pay
with another approved asset.

## Always get a quote first

Every payment starts with a quote (`POST /v1/dids/registration-quotes`). The quote states:

- the fixed price, 25.00 USD;
- the asset, the network and, for tokens, the token contract;
- the **exact amount** to pay in that asset;
- where to pay: a DMD address made for this quote only, or the x402 payment terms;
- how many confirmations the payment needs;
- for an asset that is not a US dollar stablecoin, the price used and when it was taken;
- when the quote expires ({{ quote_minutes }} minutes after it was made);
- the terms version and the links to these policies.

Pay **exactly the quoted amount, in the quoted asset, on the quoted network, before the
quote expires.** Never pay an amount that was not quoted. A payment in another asset or on
another network is not credited and may not be recoverable.

The payment methods that are switched on right now are listed at
[`GET /v1/payment-options`]({{ base_url }}/v1/payment-options). A method can be paused at
any time, for example during maintenance or a network incident; a paused method is not
offered in new quotes.

## Approved methods

{{ payment_methods }}

### DMD, on the DebitMyData Proof-of-Work chain

- Each quote has its own DMD address. Send the quoted amount to it from a DMD wallet.
- The amount in DMD comes from the DMD/USD reference price, published at
  [`GET /v1/prices/dmd-usd`]({{ base_url }}/v1/prices/dmd-usd). Until DMD has a market
  price, this reference price is the cost of mining DMD: the electricity that the work of a
  block takes. It is recalculated every few minutes; no quote is made from a price older
  than 15 minutes. The quoted amount is rounded up to the next 0.00000001 DMD.
- The quoted amount must arrive in full: your wallet's network fee comes on top of it.
- If you sent too little, you can send the rest to the same address before the quote
  expires.
- You may report your transaction with `POST /v1/payments/{payment_intent_id}/references`.
  This only helps the gateway find it faster: every payment is checked on the chain.

### USDC through x402, on Base

- You pay with an x402-compatible wallet, such as a Coinbase agentic wallet. The
  registration request answers **402 Payment Required** with the payment terms; your
  wallet signs a USDC transfer of exactly the quoted amount (EIP-3009) and sends the
  request again with the signed payment.
- The payment is taken last, only once the DID can be issued: you prove control of your
  DID's controller first, so you are never charged for a DID that cannot be issued.
- The x402 facilitator submits the transfer and pays the network fee. The gateway then
  checks the transfer on the chain itself.

### Not yet available

Wrapped DMD on Base and USDC on Solana are planned. They are not accepted until each one
has passed its own review.

## When a payment counts

A payment counts once it is **settled**: the gateway has seen it on the chain itself, it
pays the quoted amount, and it has the required confirmations. You can follow it at
`GET /v1/payments/{payment_intent_id}`. A DID is only issued after the payment is settled
and every controller named in the quote has proved control.

Payments that are too large, too small, too late or sent twice are not credited to the
registration. See the [refund policy]({{ base_url }}/policies/refunds) for what happens to
them.

## Test networks

On the staging server, payments use test networks whose coins have no value: a private DMD
test chain and Base Sepolia. Test payments never count on the production service.

## Questions

Write to info@debitmydata.com with your `payment_intent_id`.
