"use strict";
// Guided staging registration: runs the API steps in order, passing each result to the next
// step, and keeps the test agent's private key in this page only.
(() => {
  const POLL_MS = 3000;
  const SETTLE_TIMEOUT_MS = 180000;
  const ISSUE_RETRIES = 20;
  const WARN_SECONDS = 60;

  const $ = (selector) => document.querySelector(selector);
  const stepList = $("#steps");
  const runAll = $("#run-all");
  const restart = $("#restart");
  const nameInput = $("#display-name");
  const resultBox = $("#result");
  const fundingBox = $("#funding");
  const runNote = $("#run-note");
  const countdown = $("#countdown");
  const announcer = $("#announcer");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let values = {};
  let deadlines = {};
  let plan = [];
  let next = 0;
  let busy = false;
  let stopAll = false;
  // USDC only: the staging test wallet, which must hold the test USDC the payment needs.
  // state: none (DMD) | loading | ok | short (not enough) | unknown (balance unreadable) |
  // unavailable (USDC cannot be tested on this server)
  let funding = { state: "none" };
  // Server time minus this computer's time, from each answer's Date header, so the countdown
  // is right even when the visitor's clock is not (+0.5 s: the header drops the milliseconds).
  let clockOffset = 0;
  const serverNow = () => Date.now() + clockOffset;

  // ------------------------------------------------------------------ HTTP

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
    return { method, path, body, headers, status: response.status, data, response };
  }

  class StepError extends Error {
    constructor(message, fix) {
      super(message);
      this.fix = fix; // "challenge" or "restart": what the visitor can do next
    }
  }

  function errorText(result) {
    const error = result.data && result.data.error;
    if (error && typeof error === "object") {
      return `${error.message || "Request refused"} (HTTP ${result.status}, ${error.code})`;
    }
    return `HTTP ${result.status}`;
  }

  function expect(result, statuses, fix) {
    if (!statuses.includes(result.status)) {
      const code = result.data && result.data.error && result.data.error.code;
      const challengeCodes = ["challenge_expired", "challenge_already_used", "unknown_challenge"];
      if (challengeCodes.includes(code)) fix = "challenge";
      if (code === "quote_expired" || code === "registration_closed") fix = "restart";
      throw new StepError(errorText(result), fix);
    }
    return result.data;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const displayName = () => nameInput.value.trim() || "Staging test agent";
  const short = (value) => (value && value.length > 30 ? `${value.slice(0, 26)}…` : value);

  // ------------------------------------------------------------------ steps

  const STEPS = {
    key: {
      title: "Create a test agent key",
      help: "Stands in for the agent's own key. Its private part stays in this page.",
      uses: () => ({}),
      run: async () => {
        const result = await call("POST", "/v1/staging/test-agent-keys");
        const data = expect(result, [201]);
        values.publicKey = data.controller_public_key;
        values.privateKey = data.private_key;
        return { result, shown: { controller_public_key: data.controller_public_key } };
      },
    },
    quote: {
      title: "Get a quote",
      help: "The price in the chosen payment option, and where to pay it.",
      uses: () => ({
        display_name: displayName(),
        controller_public_key: short(values.publicKey),
        preferred_payment_option: values.option,
      }),
      run: async () => {
        const result = await call("POST", "/v1/dids/registration-quotes", {
          display_name: displayName(),
          participant_type: "autonomous_agent",
          controller_public_key: values.publicKey,
          preferred_payment_option: values.option,
        });
        const data = expect(result, [200, 201]);
        values.registrationId = data.registration_intent_id;
        values.paymentId = data.payment_intent_id;
        deadlines.quote = { at: Date.parse(data.expires_at), label: "Quote valid for" };
        const option = data.selected_payment_option || {};
        return {
          result,
          shown: {
            registration_intent_id: data.registration_intent_id,
            payment_intent_id: data.payment_intent_id,
            amount: `${option.amount} ${option.asset || ""}`.trim(),
          },
        };
      },
    },
    pay: {
      title: "Pay with test coins",
      help: "The staging test wallet pays the exact quoted amount and mines the confirmations.",
      uses: () => ({ payment_intent_id: values.paymentId }),
      run: async () => {
        const result = await call("POST", "/v1/staging/test-payments", {
          payment_intent_id: values.paymentId,
        });
        const data = expect(result, [201], "restart");
        return {
          result,
          shown: { transaction: short(data.transaction_reference), blocks_mined: data.blocks_mined },
        };
      },
    },
    settle: {
      title: "Wait for the payment to settle",
      help: "The gateway checks the payment on chain. This usually takes 10 to 20 seconds.",
      uses: () => ({ payment_intent_id: values.paymentId }),
      run: async (progress) => {
        const started = Date.now();
        for (;;) {
          const result = await call("GET", `/v1/payments/${values.paymentId}`);
          const data = expect(result, [200]);
          if (data.status === "settled") {
            delete deadlines.quote; // paid in time: the quote's limit no longer matters
            return { result, shown: { status: data.status, confirmations: data.confirmations } };
          }
          const waiting = ["awaiting_payment", "detected", "confirming", "verified"];
          if (!waiting.includes(data.status)) {
            throw new StepError(`The payment is ${data.status}.`, "restart");
          }
          if (Date.now() - started > SETTLE_TIMEOUT_MS) {
            throw new StepError("The payment has not settled after 3 minutes.", "retry");
          }
          const seen = data.confirmations == null ? "" : `, ${data.confirmations} confirmations`;
          progress(`Status: ${data.status}${seen}. Checking again in a few seconds…`);
          await sleep(POLL_MS);
        }
      },
    },
    challenge: {
      title: "Request a controller challenge",
      help: "A message the agent must sign to prove it holds the key.",
      uses: () => ({ registration_intent_id: values.registrationId }),
      run: async () => {
        const result = await call(
          "POST",
          `/v1/dids/registrations/${values.registrationId}/controller-challenges`,
          {}
        );
        const data = expect(result, [200, 201], "restart");
        values.challengeId = data.challenge_id;
        deadlines.challenge = { at: Date.parse(data.expires_at), label: "Challenge valid for" };
        return { result, shown: { challenge_id: data.challenge_id } };
      },
    },
    sign: {
      title: "Sign the challenge",
      help: "Stands in for the agent signing with its own key.",
      uses: () => ({ challenge_id: values.challengeId, private_key: "(kept in this page)" }),
      run: async () => {
        const result = await call("POST", "/v1/staging/test-signatures", {
          challenge_id: values.challengeId,
          private_key: values.privateKey,
        });
        const data = expect(result, [200, 201]);
        values.signature = data.signature;
        return { result, shown: { signature: short(data.signature) } };
      },
    },
    proof: {
      title: "Send the proof",
      help: "The gateway checks the signature against the key in the quote.",
      uses: () => ({ challenge_id: values.challengeId, signature: short(values.signature) }),
      run: async () => {
        const result = await call(
          "POST",
          `/v1/dids/registrations/${values.registrationId}/controller-proofs`,
          { challenge_id: values.challengeId, signature: values.signature }
        );
        const data = expect(result, [200, 201]);
        if (data.status !== "verified") {
          throw new StepError(`The proof is ${data.status}.`, "challenge");
        }
        delete deadlines.challenge;
        return { result, shown: { status: data.status } };
      },
    },
    paysign: {
      title: "Sign the USDC payment",
      help: "The staging test wallet (see above) signs an x402 payment of the quoted test USDC.",
      uses: () => ({ payment_intent_id: values.paymentId }),
      run: async () => {
        const result = await call("POST", "/v1/staging/test-payments", {
          payment_intent_id: values.paymentId,
        });
        const data = expect(result, [201], "restart");
        values.paymentSignature = data.payment_signature;
        if (data.valid_until) {
          deadlines.payment = { at: Date.parse(data.valid_until), label: "Payment valid for" };
        }
        return { result, shown: { payment_signature: short(data.payment_signature) } };
      },
    },
    issue: {
      title: "Issue the DID-LLM",
      help: "The DID-LLM, its signed credential and the entitlement.",
      uses: () =>
        values.option === "x402_usdc_base"
          ? { registration_intent_id: values.registrationId, "PAYMENT-SIGNATURE": short(values.paymentSignature) }
          : { registration_intent_id: values.registrationId },
      run: async (progress) => {
        // The signed payment goes with the first request only: once the gateway has it, a 202
        // means it is confirming, and the next requests are sent without it.
        let headers = values.paymentSignature ? { "PAYMENT-SIGNATURE": values.paymentSignature } : {};
        for (let attempt = 0; ; attempt += 1) {
          const result = await call(
            "POST",
            "/v1/dids/registrations",
            { registration_intent_id: values.registrationId },
            headers
          );
          if (result.status === 402 && values.option === "x402_usdc_base") {
            throw new StepError(errorText(result), "payment");
          }
          if (result.status === 202 && attempt < ISSUE_RETRIES) {
            headers = {};
            const wait = Number(result.response.headers.get("Retry-After")) || 5;
            progress(`Payment received and confirming. Asking again in ${wait} seconds…`);
            await sleep(wait * 1000);
            continue;
          }
          const data = expect(result, [200, 201]);
          values.issued = data;
          deadlines = {};
          if (values.option === "x402_usdc_base") values.paymentTx = await paymentTransaction(data);
          return { result, shown: { did: data.did, payment_transaction: short(values.paymentTx) } };
        }
      },
    },
  };

  // The USDC transfer: in the answer that settled it, otherwise in the payment's status.
  async function paymentTransaction(issued) {
    const settled = issued.payment_response && issued.payment_response.transaction;
    if (settled) return settled;
    try {
      const status = await call("GET", `/v1/payments/${values.paymentId}`);
      return (status.data.transactions || [])[0] || null;
    } catch {
      return null;
    }
  }

  const PLANS = {
    dmd_pow_native: ["key", "quote", "pay", "settle", "challenge", "sign", "proof", "issue"],
    x402_usdc_base: ["key", "quote", "challenge", "sign", "proof", "paysign", "issue"],
  };

  // ------------------------------------------------------------------ rendering

  function element(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (name === "class") node.className = value;
      else node.setAttribute(name, value);
    }
    for (const child of children) {
      if (child != null) node.append(child);
    }
    return node;
  }

  function valueList(pairs) {
    const list = element("dl", { class: "values" });
    for (const [name, value] of Object.entries(pairs)) {
      if (value === undefined || value === null || value === "") continue;
      list.append(element("dt", {}, name), element("dd", {}, String(value)));
    }
    return list.childElementCount ? list : null;
  }

  function redact(body) {
    if (!body || typeof body !== "object") return body;
    const copy = Array.isArray(body) ? [...body] : { ...body };
    for (const key of ["private_key", "wallet_private_key"]) {
      if (key in copy) copy[key] = "(kept in this page only)";
    }
    return copy;
  }

  function exchange(result) {
    const sent = [`${result.method} ${result.path}`];
    for (const [name, value] of Object.entries(result.headers)) sent.push(`${name}: ${short(value)}`);
    if (result.body !== undefined) sent.push("", JSON.stringify(redact(result.body), null, 2));
    return element(
      "details",
      {},
      element("summary", {}, "What was sent and what came back"),
      element("pre", {}, sent.join("\n")),
      element("pre", {}, `HTTP ${result.status}\n${JSON.stringify(redact(result.data), null, 2)}`)
    );
  }

  function card(index) {
    return stepList.children[index];
  }

  function render() {
    stepList.replaceChildren();
    plan.forEach((id, index) => {
      const step = STEPS[id];
      const button = element("button", { type: "button", class: "execute" }, "Execute");
      button.addEventListener("click", () => runStep(index));
      stepList.append(
        element(
          "li",
          { class: "step waiting", "data-step": id },
          element(
            "div",
            { class: "step-head" },
            element("span", { class: "badge", "aria-hidden": "true" }, String(index + 1)),
            element("h2", {}, `Step ${index + 1}: ${step.title}`)
          ),
          element("p", { class: "help" }, step.help),
          element("div", { class: "uses" }),
          button,
          element("p", { class: "status" }),
          element("div", { class: "detail" })
        )
      );
    });
    refresh();
  }

  // A USDC test does not start until the test wallet can pay (or its balance is unknown).
  function waitingForFunds() {
    return next === 0 && ["loading", "short", "unavailable"].includes(funding.state);
  }

  function refresh() {
    const held = waitingForFunds();
    plan.forEach((id, index) => {
      const node = card(index);
      const isNext = index === next && !values.issued;
      node.classList.toggle("current", isNext);
      node.classList.toggle("waiting", index > next);
      const button = node.querySelector("button.execute");
      button.disabled = !isNext || busy || held;
      button.hidden = index < next;
      if (index >= next) {
        const uses = index === next ? valueList(STEPS[id].uses()) : null;
        node.querySelector(".uses").replaceChildren(
          ...(uses ? [element("p", { class: "help" }, "Filled in from the previous steps:"), uses] : [])
        );
      }
    });
    runAll.disabled = busy || Boolean(values.issued) || !plan.length || held;
    runNote.textContent = {
      loading: "Checking the test wallet's USDC…",
      short: "First add test USDC to the test wallet (see above).",
      unavailable: "USDC cannot be tested on this server right now.",
    }[held ? funding.state : ""] || "";
    nameInput.disabled = next > 1;
    for (const radio of document.querySelectorAll("input[name=option]")) {
      radio.disabled = next > 0 || radio.dataset.available !== "yes";
    }
  }

  function setStatus(index, text, kind) {
    const status = card(index).querySelector(".status");
    status.textContent = text;
    status.className = `status ${kind || ""}`.trim();
  }

  function focusNext() {
    // The button goes to the middle of the screen, clear of the countdown (at the bottom on
    // phones), with the values it will send just above it.
    const target = next < plan.length ? card(next) : resultBox;
    const button = target.querySelector("button.execute, a.big");
    (button || target).scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
    if (button) button.focus({ preventScroll: true });
  }

  function showFix(index, error) {
    const node = card(index);
    const box = element("div", { class: "fix" });
    if (error.fix === "challenge") {
      const again = element("button", { type: "button" }, "Get a new challenge");
      again.addEventListener("click", () => {
        next = plan.indexOf("challenge");
        delete deadlines.challenge;
        for (let i = next; i < plan.length; i += 1) resetCard(i);
        refresh();
        tick();
        focusNext();
      });
      box.append(again);
    } else if (error.fix === "payment") {
      const again = element("button", { type: "button" }, "Sign a new payment");
      again.addEventListener("click", () => {
        next = plan.indexOf("paysign");
        delete deadlines.payment;
        for (let i = next; i < plan.length; i += 1) resetCard(i);
        refresh();
        tick();
        focusNext();
      });
      box.append(again);
    } else if (error.fix === "restart") {
      const again = element("button", { type: "button" }, "Start over");
      again.addEventListener("click", () => start());
      box.append(again);
    }
    node.querySelector(".detail").append(box);
  }

  function resetCard(index) {
    const node = card(index);
    node.classList.remove("done", "failed");
    setStatus(index, "", "");
    node.querySelector(".detail").replaceChildren();
  }

  function showResult() {
    const issued = values.issued;
    const open = element(
      "a",
      { class: "big", href: issued.did_document_url, target: "_blank", rel: "noopener" },
      "Open your DID-LLM document"
    );
    resultBox.replaceChildren(
      element("h2", {}, "Your test DID-LLM is ready"),
      element("div", { class: "did" }, issued.did),
      open,
      element("br"),
      element(
        "a",
        { class: "plain", href: issued.credential_url, target: "_blank", rel: "noopener" },
        "View the signed credential"
      ),
      element("a", { class: "plain", href: "/docs/advanced" }, "Advanced API docs")
    );
    if (values.paymentTx) {
      const base = explorerBase();
      resultBox.insertBefore(
        element(
          "p",
          { class: "paid" },
          `Paid ${amount(funding.data ? funding.data.needed : "")} test USDC on Base Sepolia: `,
          element(
            "a",
            { href: `${base}/tx/${values.paymentTx}`, target: "_blank", rel: "noopener" },
            "see the payment on BaseScan"
          )
        ),
        open
      );
    }
    resultBox.hidden = false;
    announce("Your test DID-LLM is ready.");
  }

  // ------------------------------------------------------------------ running

  async function runStep(index) {
    if (busy || index !== next) return false;
    busy = true;
    stopAll = false;
    refresh();
    const node = card(index);
    node.querySelector(".detail").replaceChildren();
    setStatus(index, "Running…", "busy");
    try {
      const outcome = await STEPS[plan[index]].run((text) => setStatus(index, text, "busy"));
      node.classList.add("done");
      node.classList.remove("failed");
      setStatus(index, "Done", "ok");
      node.querySelector(".uses").replaceChildren();
      node.querySelector(".detail").append(
        ...[valueList(outcome.shown), exchange(outcome.result)].filter(Boolean)
      );
      next = index + 1;
      announce(`Step ${index + 1} done.`);
      if (values.issued) showResult();
      return true;
    } catch (error) {
      node.classList.add("failed");
      const message = error instanceof StepError ? error.message : `Could not reach the gateway: ${error.message}`;
      setStatus(index, message, "bad");
      if (error instanceof StepError) showFix(index, error);
      announce(`Step ${index + 1} failed: ${message}`);
      stopAll = true;
      return false;
    } finally {
      busy = false;
      refresh();
      tick();
      if (!stopAll || values.issued) focusNext();
    }
  }

  async function runEverything() {
    if (!chosenOption()) return;
    if (!plan.length) prepare();
    while (next < plan.length) {
      const ok = await runStep(next);
      if (!ok) return;
      await sleep(reducedMotion ? 0 : 400);
    }
  }

  // ------------------------------------------------------------------ USDC test wallet

  const amount = (text) =>
    text === "" || text == null
      ? ""
      : Number(text).toLocaleString("en-US", { maximumFractionDigits: 6 });

  function explorerBase() {
    try {
      return new URL(funding.data.explorer_url).origin;
    } catch {
      return "https://sepolia.basescan.org";
    }
  }

  async function loadFunding() {
    if (values.option !== "x402_usdc_base") {
      funding = { state: "none" };
      renderFunding();
      refresh();
      return;
    }
    funding = { state: "loading", data: funding.data };
    renderFunding();
    refresh();
    let found;
    try {
      const result = await call("GET", "/v1/staging/test-wallet");
      if (result.status !== 200) {
        found = { state: "unavailable", message: errorText(result) };
      } else {
        const enough = result.data.enough;
        found = { state: enough === true ? "ok" : enough === false ? "short" : "unknown", data: result.data };
      }
    } catch {
      found = { state: "unknown", data: null };
    }
    if (values.option !== "x402_usdc_base") return; // switched to DMD meanwhile
    funding = found;
    renderFunding();
    refresh();
  }

  function fundingSteps(data) {
    const address = element("code", { class: "addr" }, data.address);
    const copy = element("button", { type: "button", class: "secondary small" }, "Copy address");
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(data.address);
        copy.textContent = "Copied";
      } catch {
        const range = document.createRange();
        range.selectNodeContents(address);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
        copy.textContent = "Selected: press Ctrl+C";
      }
      setTimeout(() => (copy.textContent = "Copy address"), 2500);
    });
    return element(
      "ol",
      { class: "how" },
      element(
        "li",
        {},
        element(
          "a",
          { class: "button", href: data.faucet_url, target: "_blank", rel: "noopener" },
          "Open the Circle faucet"
        ),
        " (a new tab; the test USDC is free)."
      ),
      element(
        "li",
        {},
        "Choose ",
        element("strong", {}, data.asset),
        " and the network ",
        element("strong", {}, data.network_name),
        "."
      ),
      element("li", {}, "Paste the test wallet's address: ", address, " ", copy),
      element(
        "li",
        {},
        "Send, then come back to this page: the balance is checked again when you return. If one "
          + "request is not enough, request again when the faucet allows it."
      )
    );
  }

  function renderFunding() {
    if (funding.state === "none") {
      fundingBox.hidden = true;
      fundingBox.replaceChildren();
      return;
    }
    fundingBox.hidden = false;
    const data = funding.data;
    fundingBox.className = `funding ${funding.state}${data ? "" : " bare"}`;
    if (funding.state === "unavailable") {
      fundingBox.replaceChildren(
        element("h2", {}, "USDC cannot be tested on this server right now"),
        element("p", {}, funding.message || "")
      );
      return;
    }
    if (!data) {
      fundingBox.replaceChildren(
        element(
          "p",
          {},
          funding.state === "loading"
            ? "Checking the USDC test wallet…"
            : "The USDC test wallet could not be checked just now. You can still try the steps."
        )
      );
      return;
    }
    const need = amount(data.needed);
    const have = amount(data.balance);
    const back = data.paid_back
      ? "The payment goes back to the same wallet, so it only needs this test USDC once."
      : `Each USDC test spends ${need} test USDC from it.`;
    const check = element("button", { type: "button", class: "secondary small" }, "Check again");
    check.addEventListener("click", () => loadFunding());
    check.disabled = funding.state === "loading";
    const wallet = element(
      "a",
      { href: data.explorer_url, target: "_blank", rel: "noopener" },
      "See the wallet on BaseScan"
    );
    const tools = element("p", { class: "tools" }, check, " ", wallet);
    if (funding.state === "loading") {
      tools.prepend(element("span", { class: "help" }, "Checking… "));
    }
    // While checking again, keep showing what the last check found.
    const view =
      funding.state !== "loading"
        ? funding.state
        : data.enough === true
          ? "ok"
          : data.enough === false
            ? "short"
            : "unknown";
    fundingBox.className = `funding ${view}`;
    let parts;
    if (view === "ok") {
      parts = [
        element("h2", {}, "✓ The test wallet has enough test USDC"),
        element(
          "p",
          {},
          `This test pays ${need} test USDC (${data.network_name}, no real value) from the `
            + `staging test wallet, which holds ${have} USDC. ${back}`
        ),
        element(
          "details",
          {},
          element("summary", {}, "How to add more test USDC"),
          fundingSteps(data)
        ),
        tools,
      ];
    } else if (view === "short") {
      const missing = amount((Number(data.needed) - Number(data.balance || 0)).toFixed(6));
      parts = [
        element("h2", {}, `First: add ${missing} test USDC to the test wallet`),
        element(
          "p",
          {},
          `This test pays ${need} test USDC (${data.network_name}, no real value) from the `
            + `staging test wallet, which holds ${have} USDC now. ${back} Adding some is free `
            + "and takes about a minute:"
        ),
        fundingSteps(data),
        tools,
      ];
    } else {
      parts = [
        element("h2", {}, `This test needs ${need} test USDC in the test wallet`),
        element(
          "p",
          {},
          "Its balance could not be read just now; you can still try the steps. If the payment "
            + `fails for lack of funds, add test USDC: ${back}`
        ),
        fundingSteps(data),
        tools,
      ];
    }
    fundingBox.replaceChildren(...parts);
  }

  // ------------------------------------------------------------------ countdown

  function tick() {
    const active = Object.values(deadlines).filter((d) => Number.isFinite(d.at));
    if (!active.length) {
      countdown.hidden = true;
      countdown.classList.remove("warn", "expired");
      return;
    }
    const soonest = active.reduce((a, b) => (a.at <= b.at ? a : b));
    const seconds = Math.max(0, Math.floor((soonest.at - serverNow()) / 1000));
    const minutes = Math.floor(seconds / 60);
    countdown.hidden = false;
    countdown.querySelector(".label").textContent = seconds ? soonest.label : "Time is up";
    countdown.querySelector(".time").textContent = seconds
      ? `${minutes}:${String(seconds % 60).padStart(2, "0")}`
      : "Expired";
    countdown.classList.toggle("warn", seconds > 0 && seconds <= WARN_SECONDS);
    countdown.classList.toggle("expired", seconds === 0);
    if (seconds === WARN_SECONDS) announce(`One minute left: ${soonest.label.toLowerCase()} 1 minute.`);
  }

  function announce(text) {
    announcer.textContent = text;
  }

  // ------------------------------------------------------------------ setup

  function chosenOption() {
    const radio = document.querySelector("input[name=option]:checked");
    return radio ? radio.value : null;
  }

  function prepare() {
    values = { option: chosenOption() };
    deadlines = {};
    plan = PLANS[values.option] || [];
    next = 0;
    resultBox.hidden = true;
    resultBox.replaceChildren();
    render();
    tick();
    loadFunding();
  }

  function start() {
    nameInput.disabled = false;
    prepare();
    window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
    nameInput.focus();
  }

  async function loadOptions() {
    let enabled = [];
    try {
      const result = await call("GET", "/v1/payment-options?service=did_registration");
      enabled = (result.data.supported_payment_options || [])
        .filter((option) => option.status === "enabled")
        .map((option) => option.id);
    } catch {
      enabled = [];
    }
    for (const radio of document.querySelectorAll("input[name=option]")) {
      const available = enabled.includes(radio.value);
      radio.dataset.available = available ? "yes" : "no";
      const choice = radio.closest(".choice");
      choice.classList.toggle("unavailable", !available);
      choice.querySelector("small.off")?.remove();
      if (!available) {
        radio.checked = false;
        choice.append(element("small", { class: "off" }, "Not available on this server right now."));
      }
    }
    if (!chosenOption()) {
      const first = document.querySelector("input[name=option][data-available=yes]");
      if (first) first.checked = true;
    }
    prepare();
  }

  for (const radio of document.querySelectorAll("input[name=option]")) {
    radio.addEventListener("change", () => prepare());
  }
  runAll.addEventListener("click", () => runEverything());
  // Back from the faucet's tab: check the balance again.
  window.addEventListener("focus", () => {
    if (funding.state === "short" && next === 0) loadFunding();
  });
  restart.addEventListener("click", () => start());
  setInterval(tick, 1000);
  loadOptions();
})();
