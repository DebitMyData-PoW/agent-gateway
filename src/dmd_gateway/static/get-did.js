"use strict";
// Get a DebitMyData DID: a registration for people, step by step. The page fills each step in
// from the previous answers; the visitor pays and signs in their own wallet. It never asks for
// a private key or a recovery phrase, and never sends money. It keeps the registration's IDs
// (no secrets) in this browser, so that a reload continues where it stopped.
(() => {
  const config = JSON.parse(document.getElementById("page-config").textContent);
  const $ = (selector) => document.querySelector(selector);
  const POLL_MS = 10000;
  const WARN_SECONDS = 60;
  const STORE = "dmd-get-did";
  // "Not now" to the app: when, kept in this browser; the offer comes back after DECLINE_DAYS.
  const DECLINED = "dmd-get-did-app-not-now";
  const DECLINE_DAYS = 30;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const WALLET_ADDRESS = /^0x[0-9a-fA-F]{40}$/;
  const PUBLIC_KEY = /^ed25519:z[1-9A-HJ-NP-Za-km-z]{40,60}$/;
  // DMD addresses (mainnet dm1…, test networks td1…, regtest dmrt1…): people paste the wallet
  // they pay from, but a DID is controlled by an Ethereum-style wallet or an agent's key.
  const DMD_ADDRESS = /^(dm|td|dmrt)1[02-9ac-hj-np-z]{6,}$/i;
  const DMD_HINT =
    "This is a DMD address: you don't need one here. At step 2 the page gives you the amount and the address to pay, from any DMD wallet. Here, give the wallet the DID will belong to: an Ethereum-style wallet (0x…), or choose “My agent's public key”.";
  // A wallet is not required: the DID needs an owner, a wallet of any kind (0x…) or an agent's key.
  const NO_OWNER =
    "Every DID needs an owner before step 2 opens: type the 0x… address of any Ethereum-style wallet (MetaMask, Coinbase Wallet, Rabby, Trust Wallet, a hardware wallet…; it needs no USDC or other balance), or choose “My agent's public key” above.";

  const form = $("#details");
  const nameInput = $("#name");
  const kindSelect = $("#kind");
  const walletBox = $("#wallet-box");
  const walletAddress = $("#wallet-address");
  const walletLabel = $("#wallet-address-label");
  const keyBox = $("#key-box");
  const publicKey = $("#public-key");
  const optionsBox = $("#options");
  const terms = $("#terms");
  const detailsError = $("#details-error");
  const quoteButton = $("#get-quote");
  const steps = [1, 2, 3, 4].map((n) => $(`#s${n}`));
  const countdown = $("#countdown");
  const announcer = $("#announcer");
  const resumeBox = $("#resume");

  let state = null; // the registration in progress (saved in this browser)
  let wallet = null; // { provider, address, name } once a wallet is connected
  const wallets = new Map(); // wallets found in this browser (EIP-6963)
  let clockOffset = 0;
  let deadlines = {};
  let pollTimer = null;
  let busy = false;

  // ------------------------------------------------------------------ helpers

  function element(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (value === undefined || value === null || value === false) continue;
      if (name === "class") node.className = value;
      else node.setAttribute(name, value === true ? "" : value);
    }
    for (const child of children) if (child != null && child !== false) node.append(child);
    return node;
  }

  async function call(method, path, body, headers = {}) {
    const init = { method, headers: { Accept: "application/json", ...headers } };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    const serverDate = Date.parse(response.headers.get("Date") || "");
    if (Number.isFinite(serverDate)) clockOffset = serverDate + 500 - Date.now();
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { text };
    }
    return { status: response.status, data };
  }

  function problem(result) {
    const error = result.data && result.data.error;
    if (error && typeof error === "object") return error.message || `The request was refused (${error.code}).`;
    // A field the gateway refused (422): say which and why, rather than "try again".
    const detail = result.data && result.data.detail;
    if (Array.isArray(detail) && detail.length) {
      const message = String(detail[0].msg || "").replace(/^Value error, /, "");
      if (/checksum/.test(message))
        return "The capital letters in this address don't match its checksum, so it was probably mistyped. Copy the address from your wallet instead of typing it.";
      return message ? `The gateway refused it: ${message}.` : `The request was refused (${result.status}).`;
    }
    if (result.status === 429) return "Too many requests: wait a minute and try again.";
    return `The gateway answered ${result.status}. Try again in a moment.`;
  }

  const errorCode = (result) => result.data && result.data.error && result.data.error.code;
  const short = (text) => (text && text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text);

  function amountText(amount) {
    const [whole, fraction] = String(amount).split(".");
    return `${Number(whole).toLocaleString("en-US")}${fraction ? `.${fraction}` : ""}`;
  }

  function copyButton(value, label = "Copy") {
    const button = element("button", { type: "button", class: "secondary small" }, label);
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select and copy it";
      }
      setTimeout(() => (button.textContent = label), 2000);
    });
    return button;
  }

  // The DMD payment link that DMD wallets open (DMD Core registers "debitmydata:" with the
  // system): the address and the exact amount, in coins with at most 8 decimals.
  function paymentLink(recipient, amount) {
    if (!DMD_ADDRESS.test(recipient || "") || !/^\d{1,10}(\.\d{1,8})?$/.test(String(amount))) return null;
    return `debitmydata:${recipient}?amount=${amount}`;
  }

  function paymentQr(link) {
    let code;
    try {
      code = DmdQr.svg(link, "QR code of the payment: the address and the exact amount, for your DMD wallet");
    } catch {
      return null; // the page still shows the amount and the address to copy
    }
    const open = element("a", { class: "button secondary", href: link }, "Open in my DMD wallet");
    return element(
      "div",
      { class: "pay-qr" },
      code,
      element(
        "div",
        { class: "open" },
        open,
        element(
          "p",
          { class: "small" },
          "Scan the code with your DMD wallet app, or tap the button on the device where your DMD wallet is installed. Your wallet fills in the address and the amount: check both against this page before you send."
        )
      )
    );
  }

  function announce(text) {
    announcer.textContent = text;
  }

  function save() {
    try {
      if (state) localStorage.setItem(STORE, JSON.stringify(state));
      else localStorage.removeItem(STORE);
    } catch {
      // private window or storage blocked: the page still works, without resuming
    }
  }

  function saved() {
    try {
      const value = JSON.parse(localStorage.getItem(STORE) || "null");
      return value && value.registrationId ? value : null;
    } catch {
      return null;
    }
  }

  // ------------------------------------------------------------------ steps and countdown

  function show(number) {
    // A finished step keeps what it showed, without its buttons.
    steps.slice(0, number - 1).forEach((step) => step.querySelectorAll(".act").forEach((node) => (node.hidden = true)));
    steps.forEach((step, index) => {
      step.classList.toggle("current", index + 1 === number);
      step.classList.toggle("done", index + 1 < number);
      step.classList.toggle("waiting", index + 1 > number);
    });
    const target = steps[number - 1];
    requestAnimationFrame(() => target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" }));
  }

  function body(number) {
    return steps[number - 1].querySelector(".body");
  }

  function tick() {
    const active = Object.values(deadlines).filter((d) => Number.isFinite(d.at));
    if (!active.length) {
      countdown.hidden = true;
      countdown.classList.remove("warn", "expired");
      return;
    }
    const soonest = active.reduce((a, b) => (a.at <= b.at ? a : b));
    const seconds = Math.max(0, Math.floor((soonest.at - Date.now() - clockOffset) / 1000));
    countdown.hidden = false;
    countdown.querySelector(".label").textContent = seconds ? soonest.label : "Time is up";
    countdown.querySelector(".time").textContent = seconds
      ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
      : "Expired";
    countdown.classList.toggle("warn", seconds > 0 && seconds <= WARN_SECONDS);
    countdown.classList.toggle("expired", seconds === 0);
  }

  // ------------------------------------------------------------------ wallets (EIP-6963)

  window.addEventListener("eip6963:announceProvider", (event) => {
    const detail = event.detail;
    if (!detail || !detail.provider || !detail.info) return;
    wallets.set(detail.info.uuid || detail.info.name, detail);
    renderWallets();
  });
  window.dispatchEvent(new Event("eip6963:requestProvider"));

  function walletList() {
    const list = [...wallets.values()];
    if (!list.length && window.ethereum) list.push({ info: { name: "Browser wallet" }, provider: window.ethereum });
    return list;
  }

  async function connect(entry) {
    const accounts = await entry.provider.request({ method: "eth_requestAccounts" });
    if (!accounts || !accounts.length) throw new Error("The wallet gave no account.");
    wallet = { provider: entry.provider, address: accounts[0], name: entry.info.name };
    return wallet;
  }

  function renderWallets() {
    if (!walletBox || (state && state.registrationId)) return;
    const holder = walletBox.querySelector(".wallets-holder");
    holder.replaceChildren();
    if (wallet) {
      const disconnect = element("button", { type: "button", class: "secondary small" }, "Use another wallet");
      disconnect.addEventListener("click", () => {
        wallet = null;
        walletAddress.value = "";
        walletAddress.disabled = false;
        renderWallets();
      });
      holder.append(
        element("div", { class: "connected" }, "✓ Connected: ", element("strong", { class: "mono" }, short(wallet.address)), ` (${wallet.name})`, disconnect)
      );
      return;
    }
    const list = walletList();
    walletLabel.textContent = list.length ? "Or type the wallet's address (0x…)" : "The wallet's address (0x…)";
    if (!list.length) {
      holder.append(
        element(
          "p",
          { class: "small" },
          "No wallet extension in this browser, and you don't need one: type your wallet's 0x… address below. At step 3 you sign one message in your wallet app and paste the signature here."
        )
      );
      return;
    }
    const row = element("div", { class: "wallets" });
    for (const entry of list) {
      const icon = entry.info.icon && /^data:image\//.test(entry.info.icon) ? element("img", { src: entry.info.icon, alt: "" }) : null;
      const button = element("button", { type: "button", class: "secondary" }, icon, `Connect ${entry.info.name}`);
      button.addEventListener("click", async () => {
        detailsError.hidden = true;
        try {
          await connect(entry);
          walletAddress.value = wallet.address;
          walletAddress.disabled = true;
          renderWallets();
        } catch (error) {
          detailsError.textContent = walletError(error);
          detailsError.hidden = false;
        }
      });
      row.append(button);
    }
    holder.append(row);
  }

  function walletError(error) {
    if (error && error.code === 4001) return "You cancelled it in your wallet.";
    return `The wallet answered: ${(error && error.message) || error}`;
  }

  function toHex(text) {
    return `0x${[...new TextEncoder().encode(text)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
  }

  // ------------------------------------------------------------------ step 1: details and quote

  function controllerChoice() {
    return form.querySelector("input[name=controller]:checked").value;
  }

  function syncController() {
    const kind = controllerChoice();
    walletBox.hidden = kind !== "wallet";
    keyBox.hidden = kind !== "key";
  }

  async function loadOptions() {
    const result = await call("GET", "/v1/payment-options?service=did_registration").catch(() => null);
    const options = (result && result.data && result.data.supported_payment_options) || [];
    const fee = result && result.data && result.data.commercial_fee;
    if (fee && fee.amount) $("#price").textContent = `$${Number(fee.amount).toLocaleString("en-US")} ${fee.currency || "USD"}, paid once`;
    optionsBox.replaceChildren();
    let first = true;
    // USDC first, then the DebitMyData coin (DMD), then the others, as DebitMyData lists them.
    const rank = (option) => (option.asset === "USDC" ? 0 : option.asset === "DMD" ? 1 : 2);
    const open = options.filter((option) => option.status === "enabled").sort((a, b) => rank(a) - rank(b));
    for (const option of open) {
      // USDC is paid by an x402 client when registering: not on this page yet.
      const onPage = option.payment_protocol !== "x402";
      const radio = element("input", { type: "radio", name: "option", value: option.id, disabled: !onPage, checked: onPage && first });
      if (onPage) first = false;
      const help = onPage
        ? option.asset === "DMD"
          ? "Send the coins from any DMD wallet: the exact amount and the address to pay come at step 2."
          : `Pay in ${option.asset}.`
        : "Coming to this page soon. Agents can already pay with it through the API.";
      optionsBox.append(
        element(
          "label",
          { class: `choice${onPage ? "" : " unavailable"}` },
          radio,
          element("strong", {}, option.name || option.id),
          onPage ? null : element("span", { class: "tag soon" }, "Soon"),
          element("small", {}, help)
        )
      );
      if (option.asset === "DMD") optionsBox.append(dmdHelpToggle());
    }
    if (first) optionsBox.append(element("p", { class: "error" }, "No payment option is open on this page right now. Please come back later."));
  }

  // Under the DMD option: a discreet "?" that opens the short guide to DMD below the options.
  function dmdHelpToggle() {
    const panel = $("#get-dmd");
    const icon = element("span", { class: "icon", "aria-hidden": "true" }, "?");
    const toggle = element("button", { type: "button", class: "help-toggle", "aria-expanded": "false", "aria-controls": "get-dmd" }, icon, "What is DMD, and how do I get it?");
    toggle.addEventListener("click", () => {
      const opening = panel.hidden;
      panel.hidden = !opening;
      toggle.setAttribute("aria-expanded", String(opening));
      if (opening) panel.scrollIntoView({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
    });
    return element("p", { class: "option-help" }, toggle);
  }

  // Says what is wrong with a typed 0x address, so it can be fixed.
  function addressProblem(address) {
    if (!address.startsWith("0x")) return "An Ethereum-style address starts with 0x (a zero and a small x), then 40 characters: 0–9 and a–f.";
    const rest = address.slice(2);
    const wrong = [...new Set(rest.replace(/[0-9a-fA-F]/g, ""))].map((c) => (/\s/.test(c) ? "a space" : `“${c}”`));
    if (wrong.length) return `This address has characters an Ethereum-style address can't have: ${wrong.join(", ")}. After 0x it uses only 0–9 and a–f.`;
    return `This address has ${rest.length} characters after 0x: an Ethereum-style address has exactly 40 (0–9 and a–f).`;
  }

  function readDetails() {
    const name = nameInput.value.trim();
    if (!name) return { error: "Give the DID a name." };
    const option = form.querySelector("input[name=option]:checked");
    if (!option) return { error: "Choose how to pay." };
    const request = { display_name: name, participant_type: kindSelect.value, preferred_payment_option: option.value };
    let controller;
    if (controllerChoice() === "wallet") {
      const address = (wallet ? wallet.address : walletAddress.value).trim();
      if (!address) return { error: NO_OWNER, focus: walletAddress };
      if (DMD_ADDRESS.test(address)) return { error: "Use an Ethereum-style address (0x…) here: a DMD address can't hold a DID (see the note above).", focus: walletAddress };
      if (!WALLET_ADDRESS.test(address)) return { error: addressProblem(address), focus: walletAddress };
      request.controller_wallet_address = address;
      controller = { kind: "wallet", value: address };
    } else {
      const key = publicKey.value.trim();
      if (!key) return { error: "Every DID needs an owner: paste the agent's public key (ed25519:z6Mk…), or choose “My wallet” above.", focus: publicKey };
      if (!PUBLIC_KEY.test(key)) return { error: "The public key should look like ed25519:z6Mk… (from your agent).", focus: publicKey };
      request.controller_public_key = key;
      controller = { kind: "key", value: key };
    }
    if (!terms.checked) return { error: "Please read and accept the terms first." };
    return { request, controller };
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const details = readDetails();
    detailsError.hidden = !details.error;
    if (details.error) {
      detailsError.textContent = details.error;
      if (details.focus && !details.focus.disabled) details.focus.focus();
      return;
    }
    busy = true;
    quoteButton.disabled = true;
    quoteButton.textContent = "Getting your quote…";
    try {
      const key = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 12)}-quote`;
      const result = await call("POST", "/v1/dids/registration-quotes", details.request, { "Idempotency-Key": key });
      if (result.status !== 201 && result.status !== 200) throw new Error(problem(result));
      const quote = result.data;
      const option = quote.selected_payment_option || {};
      state = {
        registrationId: quote.registration_intent_id,
        paymentId: quote.payment_intent_id,
        name: details.request.display_name,
        recipient: option.recipient,
        amount: option.amount,
        asset: option.asset,
        quoteExpiresAt: quote.expires_at,
        controller: details.controller,
        walletName: wallet ? wallet.name : null,
      };
      save();
      lockDetails();
      startPayment();
    } catch (error) {
      detailsError.textContent = error.message;
      detailsError.hidden = false;
      quoteButton.disabled = false;
      quoteButton.textContent = "Get my quote";
    } finally {
      busy = false;
    }
  });

  function lockDetails() {
    for (const field of form.querySelectorAll("input,select,button")) field.disabled = true;
    form.hidden = true;
    const who = state.controller.kind === "wallet" ? `wallet ${short(state.controller.value)}` : `key ${short(state.controller.value)}`;
    body(1).replaceChildren(
      element("p", { class: "summary" }, element("strong", {}, state.name || "Your DID"), ` · controlled by your ${who} · paid in ${state.asset || "DMD"}`),
      element("p", { class: "small" }, "Your registration ID (support can find your registration with it): ", element("code", {}, state.registrationId), " ", copyButton(state.registrationId))
    );
  }

  // ------------------------------------------------------------------ step 2: pay

  function startPayment() {
    show(2);
    if (state.quoteExpiresAt) deadlines.quote = { at: Date.parse(state.quoteExpiresAt), label: "Pay within" };
    tick();
    const status = element("p", { class: "status busy" }, "Waiting for your payment…");
    const check = element("button", { type: "button", class: "secondary small act" }, "Check now");
    check.addEventListener("click", () => checkPayment(status));
    const link = paymentLink(state.recipient, state.amount);
    const parts = [
      element("p", {}, "Send exactly"),
      element("div", { class: "copyline" }, element("span", { class: "amount" }, `${amountText(state.amount)} ${state.asset}`), copyButton(state.amount, "Copy amount")),
      element("p", {}, "to this address:"),
      element("div", { class: "copyline" }, element("code", {}, state.recipient), copyButton(state.recipient, "Copy address")),
      link && paymentQr(link),
      element(
        "ul",
        { class: "rules" },
        element("li", {}, "In one payment, from any DMD wallet."),
        element("li", {}, "Exactly this amount: with less, the DID cannot be issued."),
        element("li", {}, "Before the time runs out (see the countdown).")
      ),
      element("div", { class: "actions" }, status, check),
    ];
    if (config.staging) {
      const testPay = element("button", { type: "button", class: "secondary act" }, "Pay with test coins (staging only)");
      testPay.addEventListener("click", async () => {
        testPay.disabled = true;
        const result = await call("POST", "/v1/staging/test-payments", { payment_intent_id: state.paymentId });
        if (result.status !== 201) {
          status.textContent = problem(result);
          status.className = "status bad";
          testPay.disabled = false;
        }
        checkPayment(status);
      });
      parts.push(element("div", { class: "actions" }, testPay));
    }
    body(2).replaceChildren(...parts.filter(Boolean));
    checkPayment(status);
  }

  async function checkPayment(status) {
    clearTimeout(pollTimer);
    let result;
    try {
      result = await call("GET", `/v1/payments/${state.paymentId}`);
    } catch {
      status.textContent = "Could not reach the gateway; trying again…";
      pollTimer = setTimeout(() => checkPayment(status), POLL_MS);
      return;
    }
    if (result.status !== 200) {
      status.textContent = problem(result);
      status.className = "status bad";
      pollTimer = setTimeout(() => checkPayment(status), POLL_MS);
      return;
    }
    const payment = result.data;
    const needed = payment.required_confirmations;
    const seen = payment.confirmations || 0;
    const again = () => (pollTimer = setTimeout(() => checkPayment(status), POLL_MS));
    status.className = "status busy";
    if (payment.status !== "created" && payment.status !== "awaiting_payment") {
      // Paid, or too late: the QR code and the wallet button go, so nobody pays twice.
      const payLink = body(2).querySelector(".pay-qr");
      if (payLink) payLink.remove();
    }
    switch (payment.status) {
      case "created":
      case "awaiting_payment":
        status.textContent = "Waiting for your payment… (checked every 10 seconds)";
        return again();
      case "detected":
      case "confirming":
      case "verified":
      case "reorged":
        delete deadlines.quote;
        tick();
        status.textContent = `Payment received: ${seen} of ${needed || "the needed"} confirmations (about 2.5 minutes each)…`;
        return again();
      case "overpaid":
        delete deadlines.quote;
        tick();
        status.textContent = `You sent more than the amount; it is confirming (${seen} of ${needed || "?"}). For the difference, contact support with your registration ID.`;
        return again();
      case "settled":
        delete deadlines.quote;
        tick();
        status.textContent = "Paid ✓";
        status.className = "status ok";
        announce("Payment settled.");
        return startProof();
      case "underpaid":
        delete deadlines.quote;
        tick();
        status.className = "status bad";
        status.textContent = `The payment received (${payment.amount_received} ${state.asset}) is less than the amount due (${payment.amount_due} ${state.asset}), so the DID cannot be issued. Contact support with your registration ID for a refund (see the refund policy).`;
        return;
      case "expired": {
        delete deadlines.quote;
        tick();
        status.className = "status bad";
        const refund = payment.refund_due ? " A refund is due for a payment that arrived late: contact support with your registration ID." : " If you sent coins after the time ran out, contact support with your registration ID.";
        status.textContent = `The quote expired before a payment arrived.${refund}`;
        return offerRestart(body(2));
      }
      default:
        status.className = "status bad";
        status.textContent = `The payment is ${payment.status}. Contact support with your registration ID.`;
        return offerRestart(body(2));
    }
  }

  function offerRestart(container) {
    const restart = element("button", { type: "button" }, "Start a new registration");
    restart.addEventListener("click", startOver);
    container.append(element("div", { class: "actions" }, restart));
  }

  function startOver() {
    clearTimeout(pollTimer);
    state = null;
    save();
    location.reload();
  }

  // ------------------------------------------------------------------ step 3: prove control

  async function startProof() {
    show(3);
    const container = body(3);
    container.replaceChildren(element("p", { class: "status busy" }, "Getting the message to sign…"));
    const result = await call("POST", `/v1/dids/registrations/${state.registrationId}/controller-challenges`, {}).catch(() => null);
    if (!result || (result.status !== 201 && result.status !== 200)) {
      container.replaceChildren(element("p", { class: "status bad" }, result ? problem(result) : "Could not reach the gateway."));
      const retry = element("button", { type: "button" }, "Try again");
      retry.addEventListener("click", startProof);
      container.append(element("div", { class: "actions" }, retry));
      return;
    }
    const challenge = result.data;
    deadlines.challenge = { at: Date.parse(challenge.expires_at), label: "Sign within" };
    tick();
    const status = element("p", { class: "status" });
    const isWallet = challenge.controller.startsWith("eip155:");
    const parts = [
      element(
        "p",
        {},
        isWallet
          ? `Sign this message with your wallet ${short(state.controller.value)} to prove the DID is yours. Signing is free: it sends nothing.`
          : "Sign this message with your agent's key (Ed25519) to prove the DID is yours."
      ),
      element("pre", { class: "message" }, challenge.message),
      element("div", { class: "actions" }, copyButton(challenge.message, "Copy the message")),
    ];
    if (isWallet) {
      const sign = element("button", { type: "button", class: "act" }, "Sign with my wallet");
      sign.addEventListener("click", () => signWithWallet(challenge, sign, status));
      parts.push(element("div", { class: "actions" }, sign));
    } else {
      parts.push(
        element("p", { class: "small" }, `The signature can be base64url, base64 or hex (${challenge.signature_encoding || "base64url"}).`),
        element("div", { class: "copyline act" }, element("span", { class: "small" }, "Challenge ID, for your agent's tools:"), element("code", {}, challenge.challenge_id), copyButton(challenge.challenge_id))
      );
    }
    const pasted = element("textarea", { rows: 3, placeholder: isWallet ? "0x…" : "The signature", "aria-label": "Signature" });
    const send = element("button", { type: "button", class: isWallet ? "secondary act" : "act" }, "Send the signature");
    send.addEventListener("click", () => sendProof(challenge, pasted.value.trim(), status));
    const paste = element("div", { class: isWallet ? "" : "act" }, pasted, element("div", { class: "actions" }, send));
    parts.push(isWallet ? element("details", { class: "paste act" }, element("summary", {}, "Or paste a signature made in another wallet"), paste) : paste);
    parts.push(status);
    container.replaceChildren(...parts);
  }

  async function signWithWallet(challenge, button, status) {
    status.className = "status busy";
    status.textContent = "Confirm the signature in your wallet…";
    button.disabled = true;
    try {
      const address = state.controller.value;
      if (!wallet || wallet.address.toLowerCase() !== address.toLowerCase()) {
        const list = walletList();
        if (!list.length) throw new Error("No browser wallet found here: paste a signature made in your wallet app instead.");
        const preferred = list.find((entry) => entry.info.name === state.walletName) || list[0];
        await connect(preferred);
      }
      if (wallet.address.toLowerCase() !== address.toLowerCase()) {
        throw new Error(`Your wallet is on another account (${short(wallet.address)}). Switch to ${short(address)} in your wallet and try again.`);
      }
      const signature = await wallet.provider.request({ method: "personal_sign", params: [toHex(challenge.message), wallet.address] });
      await sendProof(challenge, signature, status);
    } catch (error) {
      status.className = "status bad";
      status.textContent = error instanceof Error && !("code" in error) ? error.message : walletError(error);
    } finally {
      button.disabled = false;
    }
  }

  async function sendProof(challenge, signature, status) {
    if (!signature) {
      status.className = "status bad";
      status.textContent = "Paste the signature first.";
      return;
    }
    status.className = "status busy";
    status.textContent = "Checking the signature…";
    const result = await call("POST", `/v1/dids/registrations/${state.registrationId}/controller-proofs`, { challenge_id: challenge.challenge_id, signature });
    const code = errorCode(result);
    if ((result.status === 201 || result.status === 200) && result.data.status === "verified") {
      delete deadlines.challenge;
      tick();
      status.className = "status ok";
      status.textContent = "Signature verified ✓";
      announce("Signature verified.");
      return register();
    }
    status.className = "status bad";
    const messages = {
      invalid_signature: "That signature does not match. The message is used up: get a new one and sign it again.",
      challenge_expired: "The time to sign ran out: get a new message.",
      challenge_already_used: "This message was already used: get a new one.",
    };
    status.textContent = messages[code] || problem(result);
    if (messages[code]) {
      const again = element("button", { type: "button", class: "act" }, "Get a new message");
      again.addEventListener("click", startProof);
      status.after(element("div", { class: "actions" }, again));
    }
  }

  // ------------------------------------------------------------------ step 4: the DID

  async function register() {
    show(4);
    const container = body(4);
    container.replaceChildren(element("p", { class: "status busy" }, "Issuing your DID…"));
    const result = await call("POST", "/v1/dids/registrations", { registration_intent_id: state.registrationId }).catch(() => null);
    if (!result || (result.status !== 201 && result.status !== 200)) {
      container.replaceChildren(element("p", { class: "status bad" }, result ? problem(result) : "Could not reach the gateway."));
      const retry = element("button", { type: "button" }, "Try again");
      retry.addEventListener("click", register);
      container.append(element("div", { class: "actions" }, retry));
      return;
    }
    const issued = result.data;
    steps[3].classList.add("done");
    steps[3].classList.remove("current");
    deadlines = {};
    tick();
    state = null;
    save();
    const open = element("a", { class: "big", href: issued.did_document_url, target: "_blank", rel: "noopener" }, "Open your DID document");
    container.replaceChildren(
      element(
        "div",
        { class: "result" },
        element("h2", {}, "✓ Your DID is ready"),
        element("div", { class: "did" }, issued.did),
        element("div", { class: "actions" }, copyButton(issued.did, "Copy the DID")),
        open,
        element(
          "div",
          { class: "links" },
          element("a", { href: issued.credential_url, target: "_blank", rel: "noopener" }, "View the signed credential"),
          element("a", { href: "/docs/advanced" }, "API docs")
        )
      )
    );
    announce("Your DID is ready.");
    requestAnimationFrame(() => open.focus({ preventScroll: true }));
  }

  // ------------------------------------------------------------------ continuing after a reload

  async function resume(previous) {
    const result = await call("GET", `/v1/dids/registrations/${previous.registrationId}`).catch(() => null);
    if (!result || result.status !== 200) return; // unknown or unreachable: start fresh, keep it saved
    const registration = result.data;
    const payment = (registration.payment && registration.payment.status) || "";
    const finished = ["expired", "failed", "refunded", "reversed"].includes(payment) || registration.next_action === "request_new_quote";
    if (finished && !registration.did) {
      state = null;
      save();
      resumeBox.hidden = false;
      resumeBox.replaceChildren(element("p", {}, `Your last quote (${short(previous.registrationId)}) ended: ${payment || registration.status}. If you sent coins for it, contact support with this ID: `, element("code", {}, previous.registrationId)));
      return;
    }
    resumeBox.hidden = false;
    const go = element("button", { type: "button" }, registration.did ? "Show my DID" : "Continue");
    const fresh = element("button", { type: "button", class: "secondary" }, "Start a new one");
    go.addEventListener("click", () => {
      resumeBox.hidden = true;
      state = previous;
      lockDetails();
      if (registration.did || registration.next_action === "register_did") return register();
      if (payment === "settled") return startProof();
      return startPayment();
    });
    fresh.addEventListener("click", () => {
      state = null;
      save();
      resumeBox.hidden = true;
    });
    resumeBox.replaceChildren(
      element("p", {}, registration.did ? "Your DID was issued: " : "You have a registration in progress: ", element("strong", {}, previous.name || ""), ` (${short(previous.registrationId)}).`),
      element("div", { class: "actions" }, go, fresh)
    );
  }

  // ------------------------------------------------------------------ start

  for (const radio of form.querySelectorAll("input[name=controller]")) radio.addEventListener("change", syncController);
  for (const kind of ["input", "change"]) form.addEventListener(kind, () => (detailsError.hidden = true));
  const walletHint = $("#wallet-hint");
  walletAddress.addEventListener("input", () => {
    const dmd = DMD_ADDRESS.test(walletAddress.value.trim());
    walletHint.textContent = dmd ? DMD_HINT : "";
    walletHint.hidden = !dmd;
  });
  syncController();
  setTimeout(renderWallets, 300); // wallets announce themselves shortly after the page loads
  renderWallets();
  setInterval(tick, 1000);
  // The explanation's cards rise into view as the page scrolls (not for people who prefer less
  // motion), and lift when tapped, as they do under the mouse on a computer.
  const cards = document.querySelectorAll(".stats > li, .features > li, .how > li");
  if ("IntersectionObserver" in window && !reducedMotion) {
    document.documentElement.classList.add("reveal-on");
    const seen = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          entry.target.classList.add("in");
          seen.unobserve(entry.target);
        }
      },
      { rootMargin: "0px 0px -8% 0px" }
    );
    for (const item of document.querySelectorAll(".stats > li, .features > li, .how > li, .intro .section-title, .intro .lead")) {
      item.classList.add("reveal");
      seen.observe(item);
    }
  }
  for (const card of cards) {
    card.addEventListener("click", () => {
      for (const other of cards) if (other !== card) other.classList.remove("lift");
      card.classList.toggle("lift");
    });
  }
  loadOptions();
  const previous = saved();
  if (previous) resume(previous);
  // The installable app (on its own address): keeps the page for opening without a network.
  if (config.app && "serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  offerTheApp();

  // "Get the app" window, centred on the screen. It offers itself on phones and tablets a moment
  // after the page opens, and on any device when asked (the footer's "Get the app" button, or a
  // link ending in #get-the-app). Whether the app is installed is checked again at every visit:
  // the browser's own install prompt can stay away for months. Never offered by itself inside
  // the app, nor when the browser says the app is installed (Chrome on Android does; Safari on
  // an iPhone can't tell). Only "Not now" is remembered, in this browser (not a cookie: nothing
  // is sent to us), for DECLINE_DAYS; the cross, a tap outside or Back only close it until the
  // page is opened again. Asking for it from the footer forgets an earlier "Not now".
  function offerTheApp() {
    const box = $("#install");
    if (!box) return;
    const inApp = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
    const touch = window.matchMedia("(pointer: coarse)").matches;
    const iphone = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const device = iphone ? "iphone" : touch ? "android" : "computer";
    const canTell = "getInstalledRelatedApps" in navigator;
    const title = $("#install-title");
    const offerTitle = title.textContent;
    const appName = ($('meta[name="apple-mobile-web-app-title"]') || {}).content || "the app";
    const how = $("#install-how");
    const go = $("#install-go");
    const later = $("#install-later");
    let prompt = null; // the browser's own install dialog, when it offers one
    let installed = false;
    let ready = false; // the page has been open a moment
    let closed = false; // closed (or "Not now") on this page: not offered again by itself

    const isInstalled = () => installed || inApp;
    const fill = () => {
      const done = isInstalled();
      title.textContent = done ? (inApp ? "You're using the app" : "The app is already installed") : offerTitle;
      if (inApp) how.textContent = `This is the ${appName} app.`;
      else if (done) how.textContent = `Open ${appName} from your ${device === "computer" ? "computer's apps" : "home screen"}.`;
      else if (prompt) how.textContent = "Install it: it opens full screen, like any app. Free, no app store.";
      else if (device === "computer")
        how.textContent = "In Chrome or Edge, click the install icon at the right of the address bar. In Safari on a Mac, choose File, then Add to Dock.";
      else how.textContent = "Add it to your home screen in two taps:";
      const byHand = !done && !prompt;
      $("#install-iphone").hidden = !(byHand && device === "iphone");
      $("#install-android").hidden = !(byHand && device === "android");
      $("#install-note").hidden = done || canTell;
      go.hidden = done || !prompt;
      later.textContent = done ? "OK" : "Not now";
    };
    const open = () => {
      fill();
      if (box.open) return;
      if (typeof box.showModal === "function") box.showModal();
      else box.setAttribute("open", "");
    };
    const close = () => {
      closed = true;
      if (!box.open) return;
      if (typeof box.close === "function") box.close();
      else box.removeAttribute("open");
    };
    // By itself: phones and tablets, a moment after the page opens.
    const offer = () => {
      if (!ready || closed || !touch || isInstalled() || declinedLately()) return;
      // A registration in progress comes first.
      if (!$("#countdown").hidden || !$("#resume").hidden) return;
      open();
    };
    const check = (canTell ? navigator.getInstalledRelatedApps().then((apps) => apps.some((app) => app.platform === "webapp"), () => false) : Promise.resolve(false)).then((yes) => {
      installed = yes;
    });
    // Asked for: always opens, and forgets an earlier "Not now".
    const ask = () => {
      closed = false;
      try {
        localStorage.removeItem(DECLINED);
      } catch {
        // storage refused: nothing was remembered either
      }
      check.then(open);
    };

    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      prompt = event;
      if (box.open) fill();
      else offer();
    });
    go.addEventListener("click", async () => {
      if (!prompt) return;
      const asked = prompt;
      prompt = null;
      asked.prompt();
      const choice = await asked.userChoice;
      if (choice.outcome === "accepted") installed = true;
      close(); // "Cancel" in the browser's dialog only closes ours
    });
    window.addEventListener("appinstalled", () => {
      installed = true;
      close();
    });
    // "Not now": the only answer remembered.
    later.addEventListener("click", () => {
      if (!isInstalled()) {
        try {
          localStorage.setItem(DECLINED, String(Date.now()));
        } catch {
          // storage refused (private browsing): offered again next visit
        }
      }
      close();
    });
    $("#install-close").addEventListener("click", close);
    box.addEventListener("cancel", (event) => {
      event.preventDefault(); // Escape, or the phone's Back
      close();
    });
    box.addEventListener("click", (event) => {
      if (event.target === box) close(); // a tap outside the window
    });
    // The footer's "Get the app" button (and any link to #get-the-app) opens it here.
    document.addEventListener("click", (event) => {
      const link = event.target.closest && event.target.closest('a[href$="#get-the-app"]');
      if (!link) return;
      event.preventDefault();
      ask();
    });
    if (location.hash === "#get-the-app") {
      history.replaceState(null, "", location.pathname + location.search);
      ask();
    }
    check.then(() => {
      // A moment after the page opens, not while it is still appearing.
      setTimeout(() => {
        ready = true;
        offer();
      }, 2500);
    });
  }

  function declinedLately() {
    try {
      const at = Number(localStorage.getItem(DECLINED));
      return at > 0 && Date.now() - at < DECLINE_DAYS * 24 * 60 * 60 * 1000;
    } catch {
      return false;
    }
  }
})();
