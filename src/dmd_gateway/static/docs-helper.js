"use strict";
// The API docs page: starts Swagger UI with the settings in #swagger-config, and keeps the
// values each answer gives in a panel, each with its own Copy button and the step it goes
// into. The values live in this open page only: nothing is stored or sent anywhere.
(() => {
  const config = JSON.parse(document.getElementById("swagger-config").textContent);
  const specPath = new URL(config.url, location.href).pathname;

  const notX402 = (body) =>
    Boolean(body.selected_payment_option) && body.selected_payment_option.payment_protocol !== "x402";

  // What to keep from an answer, in the order of the steps: where it is in the JSON, what it
  // is, and where it goes. "staging" values only come from the staging test tools.
  const FIELDS = [
    { path: "controller_public_key", label: "Agent public key", goes: "Get a quote: controller_public_key" },
    { path: "controller_wallet_address", label: "Agent wallet address", goes: "Get a quote: controller_wallet_address" },
    {
      path: "private_key",
      label: "Test agent key (staging only)",
      goes: "Test signature: private_key, when the challenge's controller starts with ed25519:",
      staging: true,
    },
    {
      path: "wallet_private_key",
      label: "Test wallet key (staging only)",
      goes: "Test signature: private_key, when the challenge's controller starts with eip155:",
      staging: true,
    },
    { path: "registration_intent_id", label: "Registration ID", goes: "Challenge, proof and registration: registration_intent_id" },
    { path: "payment_intent_id", label: "Payment ID", goes: "Payment status: payment_intent_id" },
    {
      path: "selected_payment_option.recipient",
      label: "Pay to this address",
      goes: "In your wallet, outside this page: send the exact amount below to this address",
      when: notX402,
    },
    {
      path: "selected_payment_option.amount",
      label: "Exact amount to pay",
      unit: (body) => body.selected_payment_option.asset,
      goes: "In your wallet: send exactly this amount, no more, no less",
      when: notX402,
    },
    {
      key: "quote_expires",
      path: "expires_at",
      label: "Quote valid until",
      deadline: true,
      when: (body) => Boolean(body.selected_payment_option),
    },
    { path: "challenge_id", label: "Challenge ID", goes: "Proof: challenge_id" },
    {
      path: "message",
      label: "Message to sign",
      goes: "Sign it with the agent's key or wallet, outside this page; then send the signature in the proof",
      when: (body) => Boolean(body.challenge_id),
      long: true,
    },
    {
      key: "challenge_expires",
      path: "expires_at",
      label: "Challenge valid until",
      deadline: true,
      when: (body) => Boolean(body.challenge_id && body.message),
    },
    { path: "signature", label: "Signature", goes: "Proof: signature" },
    {
      path: "payment_signature",
      label: "USDC payment signature",
      goes: "Registration: the PAYMENT-SIGNATURE field",
      long: true,
    },
    {
      key: "payment_signature_expires",
      path: "valid_until",
      label: "Payment signature valid until",
      deadline: true,
      when: (body) => Boolean(body.payment_signature),
    },
    { path: "did", label: "Your DID" },
    { path: "did_document_url", label: "DID document", link: true, goes: "Open it in your browser" },
    { path: "credential_url", label: "Signed credential", link: true },
    { path: "payment_response.transaction", label: "USDC payment transaction" },
  ];

  const kept = new Map(); // key -> { field, value, unit, at }
  let opened = false; // the visitor opened or closed the panel themselves
  let flash = new Set();

  // ------------------------------------------------------------------ collecting

  function read(body, path) {
    let value = body;
    for (const part of path.split(".")) {
      if (!value || typeof value !== "object") return undefined;
      value = value[part];
    }
    return value;
  }

  function collect(response) {
    const url = new URL(response.url, location.href);
    if (url.origin !== location.origin || url.pathname === specPath || !response.ok) return;
    let body = response.body;
    if (typeof body === "string") {
      try {
        body = JSON.parse(body);
      } catch {
        return;
      }
    }
    if (!body || typeof body !== "object" || Array.isArray(body)) return;
    const staging = url.pathname.startsWith("/v1/staging/");
    const changed = [];
    for (const field of FIELDS) {
      if ((field.staging && !staging) || (field.when && !field.when(body))) continue;
      const value = read(body, field.path);
      if ((typeof value !== "string" && typeof value !== "number") || value === "") continue;
      const key = field.key || field.path;
      const previous = kept.get(key);
      if (previous && previous.value === String(value)) continue;
      kept.set(key, { field, value: String(value), unit: field.unit ? field.unit(body) : "" });
      changed.push(key);
    }
    if (!changed.length) return;
    flash = new Set(changed);
    if (!opened) setOpen(true);
    render();
  }

  // ------------------------------------------------------------------ panel

  function element(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (name === "class") node.className = value;
      else node.setAttribute(name, value);
    }
    for (const child of children) if (child != null) node.append(child);
    return node;
  }

  const count = element("span", { class: "vp-count" }, "0");
  const arrow = element("span", { class: "vp-arrow", "aria-hidden": "true" }, "▸");
  const toggle = element(
    "button",
    { type: "button", class: "vp-toggle", "aria-expanded": "false", "aria-controls": "vp-body" },
    arrow,
    " Values from your responses ",
    count
  );
  const clear = element("button", { type: "button", class: "vp-clear" }, "Clear");
  const list = element("div", { class: "vp-list" });
  const empty = element(
    "p",
    { class: "vp-empty" },
    "Click Execute on an endpoint: the values you need next appear here, each with its own "
      + "Copy button and the step it goes into. They stay in this page only."
  );
  const body = element("div", { class: "vp-body", id: "vp-body", hidden: "" }, empty, list);
  const panel = element(
    "aside",
    { class: "values-panel", "aria-label": "Values from your responses" },
    element("div", { class: "vp-head" }, toggle, clear),
    body
  );
  document.body.append(panel);

  function setOpen(open) {
    body.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    arrow.textContent = open ? "▾" : "▸";
    panel.classList.toggle("open", open);
  }

  toggle.addEventListener("click", () => {
    opened = true;
    setOpen(body.hidden);
  });
  clear.addEventListener("click", () => {
    kept.clear();
    render();
  });

  async function copy(button, value, target) {
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = "Copied";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(target);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      button.textContent = "Selected: press Ctrl+C";
    }
    setTimeout(() => (button.textContent = "Copy"), 2000);
  }

  function timeLeft(at) {
    const seconds = Math.round((Date.parse(at) - Date.now()) / 1000);
    if (!Number.isFinite(seconds)) return "";
    if (seconds <= 0) return "expired";
    if (seconds < 60) return `${seconds} s left`;
    return `${Math.floor(seconds / 60)} min left`;
  }

  function row(key, { field, value, unit }) {
    const shown = element(
      field.long ? "pre" : "code",
      { class: "vp-value" },
      field.deadline ? new Date(value).toLocaleTimeString() : value
    );
    const actions = element("div", { class: "vp-actions" });
    if (field.deadline) {
      actions.append(element("span", { class: "vp-left", "data-at": value }, timeLeft(value)));
    } else {
      const button = element("button", { type: "button", class: "vp-copy" }, "Copy");
      button.addEventListener("click", () => copy(button, value, shown));
      actions.append(button);
      if (unit) actions.prepend(element("span", { class: "vp-unit" }, unit));
      if (field.link && /^https?:\/\//.test(value)) {
        actions.append(element("a", { href: value, target: "_blank", rel: "noopener" }, "Open"));
      }
    }
    return element(
      "div",
      { class: `vp-row${flash.has(key) ? " new" : ""}`, "data-key": key },
      element("div", { class: "vp-top" }, element("span", { class: "vp-label" }, field.label), actions),
      shown,
      field.goes ? element("div", { class: "vp-goes" }, `Goes into: ${field.goes}`) : null
    );
  }

  function render() {
    const rows = [];
    for (const field of FIELDS) {
      const key = field.key || field.path;
      if (kept.has(key)) rows.push(row(key, kept.get(key)));
    }
    list.replaceChildren(...rows);
    count.textContent = String(kept.size);
    empty.hidden = kept.size > 0;
    // Show the newest values: scroll the panel (not the page) to the first of them.
    const first = list.querySelector(".vp-row.new");
    if (first && !body.hidden) body.scrollTop = first.offsetTop - list.offsetTop;
    flash = new Set();
  }

  setInterval(() => {
    for (const left of list.querySelectorAll(".vp-left")) {
      left.textContent = timeLeft(left.dataset.at);
      left.classList.toggle("expired", left.textContent === "expired");
    }
  }, 5000);

  // ------------------------------------------------------------------ Swagger UI

  window.ui = SwaggerUIBundle({
    ...config,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
    responseInterceptor: (response) => {
      try {
        collect(response);
      } catch (error) {
        console.warn("Values panel:", error);
      }
      return response;
    },
  });
})();
