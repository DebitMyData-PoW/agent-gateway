# PayPal payments (PayPal Checkout)

People can pay for a DID-LLM with PayPal: payment option `paypal`. They sign in to PayPal on
PayPal's own payment page, never on a DebitMyData page, and pay with their PayPal balance, a
bank account or a card saved in PayPal. The gateway never sees their PayPal login or what
they paid with.

## How it works

1. A quote with `preferred_payment_option: paypal` creates a PayPal **order** for exactly
   25.00 USD, with the payment's ID as its reference and invoice number (PayPal refuses a
   second payment for the same invoice number). Its page (`payment_url` in the quote, on
   `www.paypal.com`) is made for that quote alone. A PayPal quote lasts **31 minutes**, like
   a card quote: time to sign in to PayPal and pay. Payments that take days to arrive
   (eCheck) are not offered.
2. The Get a DID-LLM page sends the person to that page (**Pay $25.00 with PayPal**). When
   they approve the payment, PayPal sends them back to the page
   (`?paypal=paid&payment=...`), which continues by itself.
3. Approving moves no money yet. The payment worker reads the order at every pass (every 10
   seconds) and, once it is approved, **captures** it (takes the payment), but only while the
   quote is still open: an order approved after the quote ended is never captured, so nobody
   pays for an expired quote. The capture cannot happen twice (PayPal's `PayPal-Request-Id`).
4. The worker then checks that the order and its capture carry this payment's ID, for exactly
   25.00 USD, completed. Only then is the payment `settled`. If PayPal holds the capture (a
   review), it counts once PayPal completes it. If PayPal refuses the payment (what the payer
   chose, or its own checks: `INSTRUMENT_DECLINED`, `TRANSACTION_REFUSED`...), the worker
   logs `paypal refused a capture` with PayPal's reason and tries again only after 1, 2, 4,
   then 8 minutes, five times at most for one order: PayPal counts every attempt and refuses
   an order for good after too many (`MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED`). Meanwhile
   the payer can open PayPal's page again (the Get a DID-LLM page offers the link) and choose
   another way to pay.
5. A settled PayPal payment is checked again every 15 minutes for 30 days. A **full refund**
   makes it `refunded`; a **dispute** or chargeback makes it `disputed`. A registration not
   yet issued is then cancelled. For a DID-LLM already issued, the monitor raises the alert
   "A DID-LLM's card or PayPal payment was refunded or disputed after it was issued": a person
   decides whether to suspend or revoke it (a dispute can still be won). Partial refunds are
   noted but change nothing.

An AI agent can request a PayPal quote too: `next_action` is `pay_at_payment_url`, and it gives
`payment_url` to a person, who pays there. No webhook is used: the worker asks PayPal itself.

## The PayPal app's credentials

The gateway uses one PayPal **REST app** of DebitMyData's PayPal business account: its
**client ID** and **secret**, in the environment variables `DMD_PAYPAL_CLIENT_ID` and
`DMD_PAYPAL_CLIENT_SECRET`. Staging uses a **Sandbox** app (sandbox accounts, no real money);
production a **Live** app. The gateway refuses to start the PayPal option without both, and
says which one is missing, never its value.

The secret is a secret: it goes **only** in the server's settings file
(`/etc/dmd-gateway/gateway.env`, root only, mode 600). Never in a repository, a chat, an email
or a ticket. If it may have leaked, generate a new secret on the app's page in PayPal's
developer dashboard, put it on the server, then delete the old one.

### Who can create the app

Anyone who can sign in to DebitMyData's PayPal business account can create **Sandbox** apps.
**Live** apps need the **API access** permission on that user. Only the account owner (Preska)
can give it: **Settings** (gear) → **Account access** → **Manage users** → the user →
**Permissions**. No other permission is needed (no balance, payouts or withdrawals).

### Creating the app (Sandbox, then Live)

1. Open <https://developer.paypal.com> (the `</>` icon in PayPal's business dashboard leads
   there) and click **Log in to Dashboard** with the same PayPal login.
2. **Apps & Credentials**. The switch at the top chooses **Sandbox** or **Live**.
3. **Create App**: name `DebitMyData Agent Gateway`, type **Merchant** (on Sandbox, keep the
   default sandbox business account), then **Create App**.
4. The app's page shows the **Client ID**, and the **Secret** behind *Show*. Copy them
   straight into the server's settings file (below), nowhere else.
5. On the app's page, under **Features**: tick **Customer disputes** (with it the gateway
   also reads PayPal's disputes; without it, it sees a dispute only once PayPal holds the
   payment for it). Untick what the gateway doesn't use, above all **Payouts**: it would let
   anyone with the app's secret send money out of the account. **Subscriptions**, **Payment
   links and buttons** and the rest stay unticked too. There is no box for PayPal payments
   themselves: every app can take them. Then **Save Changes**.

## 1. Staging: PayPal's sandbox

### On the staging server

```bash
sudo nano /etc/dmd-gateway/gateway.env
```

Add these three lines at the end (the Sandbox app's values between the single quotes), and
press Enter after the last one: the file must end with a line break:

```bash
DMD_RAIL_PAYPAL_ENABLED=true
DMD_PAYPAL_CLIENT_ID='...'
DMD_PAYPAL_CLIENT_SECRET='...'
```

Save, then restart the API and the worker and check the PayPal option is on:

```bash
sudo systemctl restart dmd-gateway-api dmd-gateway-worker
sudo journalctl -u dmd-gateway-worker -n 20 --no-pager | grep -i paypal
curl -s 'https://staging-api.debitmydata.io/v1/payment-options?service=did_registration' | grep -o '"paypal"'
```

The worker's log says `payment rail ready` for `paypal` (if it says `not ready`, the message
names the problem, for example `PayPal refused the app's client ID and secret (HTTP 401)`: a
Live app's values on staging, or a typo). The last command prints `"paypal"`.

### Test payments

PayPal's sandbox has its own fake accounts: in the developer dashboard, **Testing Tools** →
**Sandbox Accounts**. The **Personal** one is the buyer: its **View/Edit Account** shows its
email and password, and it has sandbox money.

1. On the staging Get a DID-LLM page: choose **PayPal**, fill in the rest, **Continue to
   payment**, then **Pay $25.00 with PayPal**.
2. On PayPal's sandbox page (`www.sandbox.paypal.com`), sign in with the sandbox **Personal**
   account and approve. Back on the page, "Paid with PayPal ✓" within about 20 seconds, then
   step 3 (sign).
3. **Leaving without paying**: on PayPal's page, *Cancel and return*. The page says you came
   back without paying; the quote stays open until its countdown ends.
4. **A late approval**: get a quote, wait until the countdown ends, then approve on PayPal's
   page: the payment shows `expired`, and the sandbox buyer is not charged.
5. **A refund**: sign in at <https://www.sandbox.paypal.com> with the sandbox **Business**
   account, open the payment under **Activity**, **Refund** the full amount. Within 15
   minutes, `GET /v1/payments/{payment_intent_id}` shows `refunded`.

## 2. Production

`paypal` is approved in `config/payment_routes.yaml`; it stays off on a server until that
server has the Live app's credentials and `DMD_RAIL_PAYPAL_ENABLED=true`. Steps:

1. **Test on staging first** (section 1).
2. In PayPal's business account (the owner, or a user with these permissions):
   - **Account settings** → **Business information**: the customer service email, and the
     name on buyers' statements (for example `DEBITMYDATA`). Clear statements prevent
     disputes.
   - **Account settings** → **Website payments** → **Payment preferences**: payments sent in
     another currency are accepted and converted automatically, never "ask me". Otherwise
     PayPal holds such payments until someone accepts each one by hand.
3. A **Live** app (it needs the API access permission), its client ID and secret into the
   production settings file with `DMD_RAIL_PAYPAL_ENABLED=true`, as on staging; restart the
   API and the worker.
4. One real 25.00 USD payment with a company PayPal account, then refund it from PayPal's
   dashboard, and check both on `GET /v1/payments/{payment_intent_id}`.

PayPal's fees are paid by DebitMyData (the person pays exactly 25.00 USD): see
<https://www.paypal.com/us/business/paypal-business-fees> for PayPal's current rates.

## Refunds and disputes

- **Refunds** are made by a person in PayPal's dashboard (**Activity** → the payment →
  **Refund**): the gateway cannot refund. The refund policy says when a PayPal payment is
  refunded.
- **Disputes**: PayPal emails the account and lists them in the **Resolution Center**, with a
  deadline to answer. Answer with the registration ID, the DID-LLM, the date and the policies
  the payer accepted. The gateway watches for disputes for 30 days after payment; later ones
  only appear in PayPal.
- To stop PayPal payments at once (for example during an incident), set
  `DMD_RAIL_PAYPAL_KILL=true` and restart the API and the worker: no new PayPal quotes, and
  open PayPal payments are not captured until it is switched back on (their quotes end by
  themselves, and an approval is never captured after its quote ended).
