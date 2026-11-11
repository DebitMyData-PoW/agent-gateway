# Card payments (Stripe Checkout)

People can pay for a DID-LLM by debit or credit card: payment option `card_stripe`. The card is
entered on Stripe's own payment page, never on a DebitMyData page, so the gateway never sees or
stores a card number.

## How it works

1. A quote with `preferred_payment_option: card_stripe` creates a **Checkout Session** at
   Stripe for exactly 25.00 USD (2500 cents). Its page (`payment_url` in the quote) is made for
   that quote alone and closes when the quote ends. A card quote lasts **31 minutes**: Stripe
   keeps a payment page open for 30 minutes at least, and the quote outlives it, so no card is
   ever charged for an expired quote.
   The page always charges US dollars: Stripe's prices in the payer's own currency (Adaptive
   Pricing, which adds a 2–4% conversion fee for the payer) are switched off on every page, so
   everyone pays exactly 25.00 USD, as the payment policy says.
2. The Get a DID-LLM page sends the person to that page (**Pay $25.00 by card**). After paying,
   Stripe sends them back to the page (`?card=paid&payment=...`), which continues by itself:
   the payment is confirmed, then the owner signs, then the DID-LLM is issued.
3. The payment worker asks Stripe about each open card payment at every pass (every 10
   seconds). Once Stripe says the session is paid, the worker checks that the session, its
   PaymentIntent and its charge all carry this payment's ID, in US dollars, in the right mode
   (test or live), succeeded and captured. Only then is the payment `settled`.
4. A settled card payment is checked again every 15 minutes for 30 days. A **full refund**
   makes it `refunded`; a **dispute** (chargeback) makes it `disputed`. A registration not yet
   issued is then cancelled. For a DID-LLM already issued, the monitor raises the alert "A
   DID-LLM's card payment was refunded or disputed after the DID-LLM was issued": a person
   decides whether to suspend or revoke it (a dispute can still be won). Partial refunds are
   noted but change nothing.

An AI agent can request a card quote too: `next_action` is `pay_at_payment_url`, and it gives
`payment_url` to a person, who pays there. `GET /v1/payments/{payment_intent_id}` repeats the
link while the payment is open.

No webhook is used: the worker asks Stripe itself, so there is no webhook signing secret to
keep.

## The Stripe key

The gateway uses one **restricted** Stripe key, in the environment variable
`DMD_STRIPE_SECRET_KEY`. It may only:

| Stripe permission | Level | Used for |
| --- | --- | --- |
| Checkout Sessions | Write | creating the payment page of each quote, reading it back |
| PaymentIntents | Read | checking the payment of a paid page |
| Charges (Charges and Refunds) | Read | checking the charge: succeeded, refunded, disputed |

Everything else stays **None**. The key cannot refund, pay out, change the bank account or
read customers. The gateway refuses to start the card option with any other kind of key: a
full secret key (`sk_...`), a publishable key (`pk_...`), or a key of the other mode
(`rk_live_...` on a test rail, `rk_test_...` on the live rail).

The key is a secret: it goes **only** in the server's settings file
(`/etc/dmd-gateway/gateway.env`, root only, mode 600). Never in a repository, a chat, an
email or a ticket. If it may have leaked, delete it in the Stripe dashboard at once (Developers,
API keys) and create a new one; nothing else needs to change.

## 1. Staging: Stripe test mode

Staging uses Stripe's **test mode**: test card numbers only, no real money moves.

### In the Stripe dashboard

1. Sign in at <https://dashboard.stripe.com> and switch to **test mode** (the "Test mode"
   switch, or a sandbox: both give test keys).
2. **Developers** → **API keys** → **Create restricted key**. If Stripe asks how the key will
   be used, choose your own integration (not a third-party app).
3. Name: `DebitMyData Agent Gateway (staging)`. Set **Checkout Sessions: Write**,
   **PaymentIntents: Read**, **Charges: Read** (in some dashboards "Charges and Refunds"), and
   leave everything else on **None**.
4. **Create key**. Stripe shows the key (`rk_test_...`) once: copy it straight into the
   staging settings file (next step), not anywhere else.

### On the staging server

```bash
sudo nano /etc/dmd-gateway/gateway.env
```

Add these two lines at the end (paste the key between the single quotes), and press Enter
after the last one: the file must end with a line break, or a line added later would be glued
onto the key:

```bash
DMD_RAIL_CARD_STRIPE_ENABLED=true
DMD_STRIPE_SECRET_KEY='rk_test_...'
```

Save, then restart the API and the worker and check the card option is on:

```bash
sudo systemctl restart dmd-gateway-api dmd-gateway-worker
sudo journalctl -u dmd-gateway-worker -n 20 --no-pager | grep -i card_stripe
curl -s 'https://staging-api.debitmydata.io/v1/payment-options?service=did_registration' | grep -o '"card_stripe"'
```

The worker's log says `payment rail ready` for `card_stripe` (if it says `not ready`, the
message names the problem, for example a missing permission: HTTP 403). The last command prints
`"card_stripe"`.

### Test payments

On the staging Get a DID-LLM page: choose **Debit or credit card**, fill in the rest, **Get my
quote**, then **Pay $25.00 by card**. On Stripe's test page:

| Card number | What happens |
| --- | --- |
| 4242 4242 4242 4242 | Paid. Back on the page, "Paid ✓" within about 10 seconds, then step 3 (sign). |
| 4000 0025 0000 3155 | Stripe asks for 3D Secure: approve it on the test screen, then paid. |
| 4000 0000 0000 9995 | Declined (insufficient funds): nothing is charged; try another card. |
| 4000 0000 0000 0259 | Paid, then disputed: within 15 minutes the payment shows `disputed`. |

Any future expiry date, any 3-digit CVC, any name and postal code.

To test a refund: in the dashboard (test mode), **Payments** → the payment → **Refund** → full
amount. Within 15 minutes, `GET /v1/payments/{payment_intent_id}` shows `refunded`.

## 2. Production

The staging test passed and DebitMyData approved live card payments on 2026-11-11: since
0.32.2, `card_stripe` is approved in `config/payment_routes.yaml`. It stays off on a server
until that server has the live key and `DMD_RAIL_CARD_STRIPE_ENABLED=true`. Steps:

1. **Stripe account**: activate live payments (business details and the bank account for
   payouts), in the dashboard's live mode.
2. **Settings** → **Business** → **Public details**: a statement descriptor people recognise
   (for example `DEBITMYDATA`), and the support email. Clear statements prevent disputes.
3. **Settings** → **Customer emails**: send receipts for successful payments and refunds.
4. **Settings** → **Branding**: DebitMyData's logo and colours for the payment page.
5. Gateway 0.32.2 or later on the production server (`card_stripe` approved).
6. A restricted **live** key (`rk_live_...`), with the same three permissions, named
   `DebitMyData Agent Gateway (production)`, into the production settings file, with
   `DMD_RAIL_CARD_STRIPE_ENABLED=true`; restart the API and the worker.
7. One real 25.00 USD payment with a company card, then refund it from the dashboard, and
   check both on `GET /v1/payments/{payment_intent_id}`.

Card fees are paid by DebitMyData (the person pays exactly 25.00 USD): see
<https://stripe.com/pricing> for Stripe's current rate.

## Refunds and disputes

- **Refunds** are made by a person in the Stripe dashboard (the gateway's key cannot refund).
  The refund policy says when a card payment is refunded.
- **Disputes**: Stripe emails the account and shows them in the dashboard, with a deadline to
  answer. Answer with the registration ID, the DID-LLM, the date and the policies the payer
  accepted. The gateway watches for disputes for 30 days after payment; later ones only appear
  in Stripe.
- To stop card payments at once (for example during an incident), set
  `DMD_RAIL_CARD_STRIPE_KILL=true` and restart the API and the worker: no new card quotes, and
  open card payments are not checked until it is switched back on (Stripe's pages close by
  themselves when their quotes end).
