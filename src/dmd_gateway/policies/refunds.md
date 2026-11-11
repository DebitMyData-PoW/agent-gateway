---
title: Refund policy
version: 2026-11-10
status: approved
summary: The DID-LLM registration fee and inference donations are non-refundable; payments that are not credited to a registration are returned.
---

# Refund policy

## The registration fee is non-refundable

The 25.00 USD DID-LLM registration fee is **non-refundable** once your payment has settled for
a registration. This includes a DID-LLM that is later suspended or revoked under the
[DID-LLM policy]({{ base_url }}/policies/did).

A paid registration does not expire. If you have paid but not yet proved control of your
DID-LLM's controller, you can finish the proof at any time and receive your DID-LLM. With USDC
through x402, you are only charged once the DID-LLM can be issued.

## Inference donations are non-refundable

An inference donation is voluntary and you choose its amount, so it is **non-refundable**
once settled. A donation whose payment was refused or failed was never taken: nothing is
charged and nothing needs refunding. If you believe a donation was taken by mistake, for
example by a wallet you no longer control, write to us with its `purchase_id`.

## BottyBotty purchases are non-refundable

A BottyBotty tier is paid once and belongs to the DID-LLM for good, so it is **non-refundable**
once the purchase is completed, including an upgrade (which costs only the difference in
price). This includes a DID-LLM that is later suspended or revoked under the
[DID-LLM policy]({{ base_url }}/policies/did). A purchase that was paid but could not be completed
(for example because the DID-LLM was revoked before completion) is refunded as described below.

## Payments that are returned

Some payments are not credited to a registration. These are returned, because they are not
the registration fee:

| Situation | What is returned |
| --- | --- |
| You paid more than the quoted amount | The amount above the quote |
| You paid less, and the quote expired before the rest arrived | Everything you paid for that quote |
| Your payment arrived after the quote expired | That payment |
| You paid a quote that was already paid | The second payment |
| DebitMyData declined the registration before issuing the DID-LLM | Everything you paid for it |

The gateway records the first four automatically: `GET /v1/payments/{payment_intent_id}`
shows them as `refund_due`.

## How a refund is made

- **Same asset, same network, same amount of the asset.** A refund returns the asset you
  sent, not its value in US dollars at another time.
- **To an address you control.** For DMD, send us a DMD address of your own to return the
  payment to: the address a payment came from may belong to an exchange or a shared
  wallet. For USDC through x402, the refund goes back to the wallet that signed the
  payment, unless you ask for another address of your own.
- **Network fee.** The network fee of the refund transaction is taken from the amount
  returned. An amount smaller than that fee cannot be returned.
- **Timing.** Refunds are sent by DebitMyData staff, normally within 10 business days of
  your request.

To ask for a refund, write to <info@debitmydata.com> with:

- the `payment_intent_id` of the quote;
- the transaction ID of your payment;
- the address to return it to (DMD), or confirmation of the paying wallet (USDC).

We may ask you to prove that you control the paying wallet or the DID-LLM's controller.

## Payments the gateway may not see

A payment is only found automatically when it matches its quote: the right asset, the right
network, the right address. The gateway also keeps watching a quote's address for 24 hours
after the quote closes. A payment sent on another network, in another asset, to an
address that was not quoted, or much later, may not be detected and may not be
recoverable. If this happens, write to us with the transaction ID; we will look into it,
but cannot promise to recover it.

## Reversed payments

Blockchain payments cannot be charged back. If a payment that paid for a DID-LLM is later
reversed, for example by a chain reorganization or a double spend, the DID-LLM may be suspended
until the registration is paid again.

## Test networks

Payments on the staging server use test networks whose coins have no value. They are not
refunded.
