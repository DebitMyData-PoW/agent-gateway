---
title: Privacy policy
version: 2026-11-11
status: approved
summary: What the DebitMyData Agent Gateway collects, what it publishes, who it shares data with, and how long it keeps it.
---

# Privacy policy

This policy covers the DebitMyData Agent Gateway: its API at `{{ base_url }}` and its MCP
server, operated by Debit My Data, Inc. The gateway is built for software agents, but the
people and organizations that run them have rights over the data that concerns them.
DebitMyData's websites and apps are covered by the
[DebitMyData privacy policy](https://app.debitmydata.com/privacy-policy).

## What we collect

**What you send us**

- In a quote: the display name, the participant type, the controller's public key and/or
  wallet address, the payment method chosen, and any metadata you add.
- During the registration: the signatures that prove control of your controller, the
  transaction references you report, and, for x402, the signed payment (which names the
  paying wallet).
- For a card payment: Stripe's references for it (the payment page, the payment), its
  amount and whether it succeeded, was refunded or disputed. We do not receive or keep the
  card number.
- In an inference donation: the amount, the signed payment (which names the paying wallet),
  and the donor name and DID-LLM if you give them. They appear in your donation receipt, which
  anyone holding its `purchase_id` can read; they are not otherwise published.

**What we see on blockchains**

- The payments made for your quotes: transactions, amounts and addresses. Blockchains are
  public; we read them, we do not control them.

**Technical data**

- The IP address and browser or client name of each request, in our server logs and those
  of Cloudflare, which protects the gateway. Request limits are counted per IP address.
- Request IDs that link log lines to a request, so problems can be traced.

We do not ask for, and never want, private keys, seed phrases or passwords. Card details
are entered on Stripe's payment page, never on ours, and we never see them.

## What we publish

To make a DID-LLM work, some data is public by design: anyone can read it, and it cannot be made
private again once published.

- The DID-LLM document: your DID-LLM and its controllers (public key and/or wallet address).
- The identity record: display name, participant type, controllers, status, registration
  ID, credentials and entitlements.
- The signed credential: all of the above, plus the payment it records (asset, network,
  amount and transaction IDs).

Quote metadata, IP addresses and logs are **not** published. Choose a display name you are
happy to make public.

## Why we use it

- To quote, verify payments and issue DID-LLMs, credentials and entitlements.
- To keep the gateway secure and available: request limits, abuse prevention, fraud
  checks and troubleshooting.
- To keep an accurate, permanent record of payments and registrations for accounting,
  refunds and disputes.
- To meet legal obligations.

## Who we share it with

- **Cloudflare**, which carries and protects all traffic to the gateway.
- **Our hosting provider**, which runs the servers and stores the database.
- **The x402 facilitator** (for USDC payments through x402), which receives your signed
  payment to submit it to the chain.
- **Stripe** (for card payments), which runs the card payment page and processes the
  payment. Stripe receives what you enter there (card details, name, email address, billing
  country) and what it needs to prevent fraud, under the
  [Stripe privacy policy](https://stripe.com/privacy). We tell Stripe the amount, the
  registration ID and the DID-LLM's display name.
- **Blockchain node providers**, which the gateway asks about payments and wallets.
- **Authorities**, when the law requires it.

We do not sell personal data, and we do not use it for advertising.

## How long we keep it

- **Registrations, donations, payments, controller proofs and the audit trail** are kept
  permanently. They are written once and never changed, because issued credentials,
  receipts, accounting and refunds depend on them.
- **Server logs** are kept only as long as security and troubleshooting need them, normally
  a few weeks.
- **Data on blockchains** is permanent and outside our control.

## Cookies

The gateway sets one cookie, `dmd-theme-id`, and only when you pick the light or dark theme
with the button in the top bar: it holds that choice ("light" or "dark") for a year so the
pages open in it next time, and nothing else. It is not used to identify or follow you, and
it is not shared with other sites. Cloudflare may set cookies that are strictly needed to
protect the service. The interactive API docs page loads its viewer from a public content
network (jsDelivr), which sees your IP address when you open it.

## Your choices and rights

You can ask us what data we hold about you, ask us to correct it, or object to how we use
it, by writing to <info@debitmydata.com>. Some data cannot be deleted: what is on blockchains,
and the records that back an issued DID-LLM and its payment. We can deactivate a DID-LLM at your
request (see the [DID-LLM policy]({{ base_url }}/policies/did)); its record is then kept but the
DID-LLM no longer resolves. Depending on where you live, you may also have the right to
complain to a data protection authority.

## Children

The gateway is not meant for children, and we do not knowingly collect data about them.

## Security

We protect the data we hold with access controls, encrypted connections and separated
systems: the gateway never holds the keys that can spend DebitMyData's funds. See the
[security page]({{ base_url }}/policies/security).

## Changes

Each version of this policy has a date. We publish changes here before they apply.

## Contact

<info@debitmydata.com>
