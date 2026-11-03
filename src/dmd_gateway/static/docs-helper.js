"use strict";
// The API docs page: starts Swagger UI with the settings in #swagger-config, marks each
// step's endpoint "Step N", draws the steps as cards at the top of the page (the next one
// with what to paste into it and a button that opens its endpoint), and shows a panel with
// the next step (Go opens it) and the values each answer gives, each with its own Copy
// button and the step it goes into. Everything lives in this open page only: nothing is
// stored or sent anywhere.
(() => {
  const settings = JSON.parse(document.getElementById("swagger-config").textContent);
  const config = settings.swagger;
  const specPath = new URL(config.url, location.href).pathname;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const notX402 = (body) =>
    Boolean(body.selected_payment_option) && body.selected_payment_option.payment_protocol !== "x402";

  // What to keep from an answer, in the order of the steps: where it is in the JSON, what it
  // is, and where it goes. "staging" values only come from the staging test tools.
  const FIELDS = [
    { path: "controller_public_key", label: "Agent public key", goes: "Get a quote: controller_public_key" },
    {
      path: "controller_wallet_address",
      label: "Agent wallet address",
      goes: "Get a quote: controller_wallet_address, only to try a wallet too. With a key and a wallet, both are proven (steps 5 to 7 for each)",
    },
    {
      path: "private_key",
      label: "Private key of the test agent (staging only)",
      goes: "Step 6: private_key, when the challenge is for the agent key (ed25519:…)",
      staging: true,
    },
    {
      path: "wallet_private_key",
      label: "Private key of the test wallet (staging only)",
      goes: "Step 6: private_key, when the challenge is for the wallet (eip155:…)",
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
      key: "challenge_controller",
      path: "controller",
      label: "Challenge is for",
      goes: "The key or wallet that signs this challenge",
      when: (body) => Boolean(body.challenge_id && body.message),
    },
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
    progress(url, methods.get(url.href) || "GET", body);
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
    if (changed.length) {
      flash = new Set(changed);
      // With the steps' cards, the next card shows what to paste: the panel stays as it is.
      if (!opened && !document.getElementById("adv-steps")) setOpen(true);
      render();
    }
    renderSteps(); // after the values: the last step opens the DID document just kept
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

  const count = element("span", { class: "vp-count" }, "(0)");
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
  const nextBar = element("div", { class: "vp-next", "aria-live": "polite" });
  const panel = element(
    "aside",
    { class: "values-panel", "aria-label": "Next step and values from your responses" },
    nextBar,
    element("div", { class: "vp-head" }, toggle, clear),
    body
  );
  document.body.append(panel);

  // Back to the top: shown once the page is scrolled down, just above the panel.
  const toTop = element(
    "button",
    { type: "button", class: "to-top", "aria-label": "Back to the top of the page", title: "Back to the top" },
    "↑"
  );
  toTop.hidden = true;
  toTop.addEventListener("click", () => window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" }));
  document.body.append(toTop);
  const placeToTop = () => {
    toTop.style.bottom = `${panel.offsetHeight + 28}px`;
  };
  if (window.ResizeObserver) new ResizeObserver(placeToTop).observe(panel);
  placeToTop();
  const showToTop = () => {
    toTop.hidden = window.scrollY < 600;
  };
  window.addEventListener("scroll", showToTop, { passive: true });
  showToTop();

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
  // Start over: this page again at its plain address (no #/... of an endpoint), so every form,
  // answer and value is cleared; nothing is kept anywhere else.
  function startAgain() {
    window.location.replace(window.location.origin + window.location.pathname);
  }

  function startOver() {
    kept.clear();
    done.clear();
    notice = "";
    paying = "dmd";
    openedKey = null;
    focusStep = null;
    document.body.classList.remove("dmd-focusing");
    controllers = 1;
    rounds = ["key", "wallet"];
    quoteControllers.clear();
    render();
    renderSteps();
  }
  clear.addEventListener("click", startOver);

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
    count.textContent = `(${kept.size})`;
    empty.hidden = kept.size > 0;
    // Show the newest values: scroll the panel (not the page) to the first of them.
    const first = list.querySelector(".vp-row.new");
    if (first && !body.hidden) body.scrollTop = first.offsetTop - list.offsetTop;
    flash = new Set();
  }

  // ------------------------------------------------------------------ steps

  // Each step: its number and title, and either its endpoint (section, operation id, method,
  // a pattern for its path) or what to do outside this page. USDC has its own order. With a
  // key and a wallet in the quote, the steps from the challenge to the proof come twice, one
  // round for each controller, one after the other: 5a to 7a, then 5b to 7b.
  const steps = settings.steps || [];
  const byNumber = new Map(steps.map((step) => [step.number, step]));
  const allSteps = steps.map((step) => step.number);
  const lastNumber = allSteps[allSteps.length - 1];
  const challengeStep = steps.find((step) => step.pattern && step.pattern.includes("controller-challenges"));
  const proofStep = steps.find((step) => step.pattern && step.pattern.includes("controller-proofs"));
  const roundSteps = new Set(
    challengeStep && proofStep ? allSteps.filter((n) => n >= challengeStep.number && n <= proofStep.number) : []
  );
  const ROUND_LABELS = { key: "the agent key", wallet: "the wallet" };
  const other = (kind) => (kind === "key" ? "wallet" : "key");
  const cap = (text) => text.charAt(0).toUpperCase() + text.slice(1);
  let paying = "dmd"; // or "x402", which has its own order
  let controllers = 1; // 2 when the quote names a key and a wallet
  let rounds = ["key", "wallet"]; // the controller of each round, in the order the gateway picks them
  const done = new Set(); // "5", or "5a" and "5b" when there are two rounds
  const methods = new Map(); // request URL -> method, to recognise each step's answers
  const quoteControllers = new Map(); // quote request URL -> how many controllers it names
  let notice = ""; // what just changed, shown with the next step
  // The Go button: grey once the next step is opened (it is being done), red again with a
  // short pulse when an answer moves on to the step after it.
  let openedKey = null; // the step opened with Go, its card's button or its chip
  let lastNextKey = null;
  let goHere = false;
  let goReady = false;
  // While a step is open, the rest of the page is dimmed ("focus"): its form, Execute and the
  // answer stay bright, with the Go button that appears under them once it is done.
  let focusStep = null;
  const listeners = new Set(); // Swagger UI parts drawn by this page, redrawn when steps change
  const notify = () => {
    for (const listener of listeners) listener();
  };

  // The steps in the order they are done: one entry per step, and per round.
  function entries() {
    const base = paying === "x402" && settings.x402_order ? settings.x402_order : allSteps;
    const list = [];
    for (const number of base) {
      if (controllers === 2 && roundSteps.has(number)) {
        if (number !== challengeStep.number) continue; // the rounds are added at the challenge
        const inRound = base.filter((n) => roundSteps.has(n));
        for (const [round, letter] of [[1, "a"], [2, "b"]]) {
          for (const n of inRound) list.push({ key: `${n}${letter}`, label: `${n}${letter}`, step: byNumber.get(n), round });
        }
        continue;
      }
      list.push({ key: String(number), label: String(number), step: byNumber.get(number), round: 0 });
    }
    return list;
  }

  function nextIndex(list) {
    let last = -1;
    list.forEach((entry, index) => {
      if (done.has(entry.key)) last = index;
    });
    return last + 1;
  }

  function nextEntry() {
    const list = entries();
    const index = nextIndex(list);
    return index < list.length ? list[index] : null;
  }

  const forRound = (entry) => (entry.round ? ` · for ${ROUND_LABELS[rounds[entry.round - 1]]}` : "");

  function setControllers(count) {
    if (count === controllers || !roundSteps.size) return;
    for (const number of roundSteps) {
      if (count === 2 && done.delete(String(number))) done.add(`${number}a`);
      if (count === 1) {
        const first = done.delete(`${number}a`);
        done.delete(`${number}b`);
        if (first) done.add(String(number));
      }
    }
    controllers = count;
  }

  // How many controllers a quote request names: read from the request as it is sent, and
  // kept as a number only.
  function countControllers(body) {
    let fields = null;
    try {
      if (typeof body === "string") {
        try {
          fields = JSON.parse(body);
        } catch {
          fields = Object.fromEntries(new URLSearchParams(body));
        }
      } else if (body instanceof URLSearchParams || (typeof FormData !== "undefined" && body instanceof FormData)) {
        fields = Object.fromEntries(body);
      } else if (body && typeof body === "object") {
        fields = body;
      }
    } catch {
      return 0;
    }
    if (!fields || typeof fields !== "object") return 0;
    return ["controller_public_key", "controller_wallet_address"].filter(
      (name) => typeof fields[name] === "string" && fields[name].trim() !== ""
    ).length;
  }

  // The entry an answer belongs to: for a step done in both rounds, the round being worked on.
  function entryFor(step, list) {
    let frontier = nextIndex(list);
    while (frontier < list.length && !list[frontier].step.pattern) frontier++;
    let chosen = -1;
    list.forEach((entry, index) => {
      if (entry.step === step && index <= frontier) chosen = index;
    });
    return chosen >= 0 ? chosen : list.findIndex((entry) => entry.step === step);
  }

  function markDone(list, position) {
    // What had to be done outside this page before it is done too.
    for (const entry of list.slice(0, position)) if (!entry.step.pattern) done.add(entry.key);
    done.add(list[position].key);
  }

  function progress(url, method, answer) {
    if (answer.selected_payment_option && settings.x402_order) {
      paying = answer.selected_payment_option.payment_protocol === "x402" ? "x402" : "dmd";
    }
    for (const step of steps) {
      if (!step.pattern || step.method !== method || !new RegExp(step.pattern).test(url.pathname)) continue;
      if (step.done_when && read(answer, step.done_when.field) !== step.done_when.value) continue;
      if (step.pattern.includes("registration-quotes")) {
        const count = quoteControllers.get(url.href);
        if (count) setControllers(count);
        rounds = ["key", "wallet"];
      }
      let list = entries();
      let position = entryFor(step, list);
      if (position < 0) continue;
      const entry = list[position];
      if (step === challengeStep && typeof answer.controller === "string") {
        // The gateway says which controller this challenge is for: the other round is the other.
        const kind = answer.controller.startsWith("eip155:") ? "wallet" : "key";
        rounds = entry.round === 2 ? [other(kind), kind] : [kind, other(kind)];
      }
      const again = step.again_when;
      if (again && read(answer, again.field) === again.value) {
        // Proven, and the quote names another controller: its round comes next.
        if (entry.round === 2) {
          // The same controller proven twice: the other one's round again.
          const start = list.findIndex((item) => item.round === 2);
          for (const item of list.slice(start)) done.delete(item.key);
          notice = `One controller is still to prove: do step ${list[start].label} again, with controller_type left empty.`;
          continue;
        }
        if (controllers !== 2) {
          setControllers(2);
          list = entries();
          position = list.findIndex((item) => item.step === step && item.round === 1);
        }
        markDone(list, position);
        notice = `✓ ${cap(ROUND_LABELS[rounds[0]])} is proven. Now the same steps for ${ROUND_LABELS[rounds[1]]}, from step ${challengeStep.number}b.`;
        continue;
      }
      notice = "";
      // A new quote or a new challenge starts what follows it again.
      if (step.restarts) for (const item of list.slice(position + 1)) done.delete(item.key);
      markDone(list, position);
    }
  }

  // With two controllers, what the challenge needs: nothing to choose, the gateway picks.
  function roundTip(entry) {
    if (!entry.round || entry.step !== challengeStep) return "";
    const n = challengeStep.number;
    const last = proofStep.number;
    if (entry.round === 1) {
      return `Your quote names an agent key and a wallet, so steps ${n} to ${last} are done once for each: ${n}a to ${last}a for ${ROUND_LABELS[rounds[0]]}, then ${n}b to ${last}b for ${ROUND_LABELS[rounds[1]]}. Leave controller_type empty: the gateway picks the right one each time.`;
    }
    return `Nothing to change from step ${n}a: leave controller_type empty and click Execute again. The gateway picks ${ROUND_LABELS[rounds[1]]}, the controller still to prove.`;
  }

  // The value kept for a field of the next step, and what it is when that helps.
  function source(field) {
    const keptValue = (key) => (kept.has(key) ? kept.get(key).value : null);
    switch (field) {
      case "private_key": {
        const controller = keptValue("challenge_controller") || "";
        const wallet = controller.startsWith("eip155:");
        const value = keptValue(wallet ? "wallet_private_key" : "private_key");
        return value && { value, note: wallet ? "the test wallet's key" : "the test agent's key" };
      }
      case "PAYMENT-SIGNATURE": {
        const value = paying === "x402" ? keptValue("payment_signature") : null;
        return value && { value };
      }
      case "recipient":
        return keptValue("selected_payment_option.recipient") && { value: keptValue("selected_payment_option.recipient") };
      case "amount": {
        const value = keptValue("selected_payment_option.amount");
        return value && { value, note: kept.get("selected_payment_option.amount").unit };
      }
      case "preferred_payment_option": {
        const value = (settings.payment_options || {})[paying];
        return value && { value, note: "as picked in “How the agent pays” at the top of the page" };
      }
      case "message":
        return keptValue("message") && { value: keptValue("message"), note: "sign it exactly as it is" };
      default:
        return keptValue(field) && { value: keptValue(field) };
    }
  }

  function fillRows(step) {
    const rows = [];
    for (const field of step.needs || []) {
      const found = source(field);
      if (!found) continue;
      const value = element("span", { class: "vp-fill-value", title: found.value }, found.value);
      const button = element("button", { type: "button", class: "vp-copy" }, "Copy");
      button.addEventListener("click", () => copy(button, found.value, value));
      rows.push(
        element(
          "div",
          { class: "vp-fill-row" },
          element("code", { class: "vp-field" }, field),
          element("span", { class: "vp-fill-main" }, value, found.note ? element("small", {}, found.note) : null),
          button
        )
      );
    }
    if (!rows.length) return null;
    const title = step.pattern ? "Paste into this step:" : "For this step:";
    return element("div", { class: "vp-fill" }, element("div", { class: "vp-fill-title" }, title), ...rows);
  }

  function goTo(step) {
    if (!step || !step.operation_id) return;
    try {
      window.ui.layoutActions.show(["operations-tag", step.tag], true);
      window.ui.layoutActions.show(["operations", step.tag, step.operation_id], true);
    } catch (error) {
      console.warn("Values panel:", error);
    }
    const id = `operations-${step.tag}-${step.operation_id}`;
    let tries = 0;
    // Wait until Swagger UI has drawn the opened step, so the page does not move under it.
    const find = () => {
      const block = document.getElementById(id);
      if (!block || !block.querySelector(".opblock-body")) {
        if (tries++ < 90) requestAnimationFrame(find);
        else if (block) jump(block);
        return;
      }
      requestAnimationFrame(() => jump(block));
    };
    const jump = (block) => {
      block.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
      if (block.animate && !reducedMotion) {
        block.animate(
          [{ boxShadow: "0 0 0 5px #f0a040" }, { boxShadow: "0 0 0 0 rgba(240,160,64,0)" }],
          { duration: 2200, easing: "ease-out" }
        );
      }
      // A long way may take longer than the smooth scroll: finish it where the step starts.
      setTimeout(() => {
        if (Math.abs(block.getBoundingClientRect().top) > 40) block.scrollIntoView({ block: "start" });
      }, reducedMotion ? 0 : 1200);
    };
    requestAnimationFrame(find);
  }

  // The button that does a step: Go / Open for an endpoint, Done or Open for the others.
  function finishOutside(entry) {
    done.add(entry.key);
    renderSteps();
  }

  function renderSteps() {
    if (!steps.length) {
      nextBar.hidden = true;
      return;
    }
    const entry = nextEntry();
    const parts = [];
    goHere = Boolean(entry) && openedKey === entry.key;
    goReady = Boolean(entry) && lastNextKey !== null && lastNextKey !== entry.key;
    lastNextKey = entry ? entry.key : null;
    if (!entry) {
      parts.push(element("div", { class: "vp-next-line" }, element("span", { class: "vp-next-text" }, "✓ All steps done")));
    } else {
      const step = entry.step;
      let action;
      if (step.pattern) {
        action = element("button", { type: "button", class: `vp-go${goState()}` }, "Go ↓");
        if (goHere) action.title = "You opened this step: fill it in and click Execute. Go takes you back to it.";
        action.addEventListener("click", () => openStep(entry));
      } else if (step.number === lastNumber && kept.has("did_document_url")) {
        action = element(
          "a",
          { class: "vp-go", href: kept.get("did_document_url").value, target: "_blank", rel: "noopener" },
          "Open"
        );
        action.addEventListener("click", () => finishOutside(entry));
      } else {
        action = element("button", { type: "button", class: "vp-go" }, "Done ✓");
        action.addEventListener("click", () => finishOutside(entry));
      }
      parts.push(
        element(
          "div",
          { class: "vp-next-line" },
          element("span", { class: "vp-next-text" }, goHere ? "Now: " : "Next: ", element("strong", {}, `Step ${entry.label}`), ` · ${step.title}${forRound(entry)}`),
          action
        )
      );
      // What to paste first, right under the step, to copy it quickly; then the explanations.
      const fill = fillRows(step);
      if (fill) parts.push(fill);
      if (notice) parts.push(element("div", { class: "vp-notice" }, notice));
      const tip = roundTip(entry) || step.outside || step.help;
      if (tip) parts.push(element("div", { class: "vp-next-help" }, ...rich(tip)));
    }
    const chips = element("div", { class: "vp-chips" });
    for (const item of entries()) {
      const isDone = done.has(item.key);
      const state = isDone ? " done" : entry && entry.key === item.key ? " next" : "";
      const name = `Step ${item.label}: ${item.step.title}${forRound(item)}`;
      const chip = element(
        "button",
        {
          type: "button",
          class: `vp-chip${state}${item.step.pattern ? "" : " outside"}`,
          title: `${name}${isDone ? " (done)" : ""}`,
          "aria-label": `${name}${isDone ? ", done" : ""}`,
        },
        isDone ? `${item.label} ✓` : item.label
      );
      chip.addEventListener("click", () => {
        if (entry && item.key === entry.key) return openStep(item);
        exitFocus();
        goTo(item.step);
      });
      chips.append(chip);
    }
    parts.push(chips);
    if (focusStep) {
      const unfocus = element("button", { type: "button", class: "vp-unfocus" }, "Show the whole page (Esc)");
      unfocus.addEventListener("click", exitFocus);
      parts.push(unfocus);
    }
    nextBar.replaceChildren(...parts);
    renderCards(entry);
    notify();
    markSoon();
  }

  // " here" while the next step is open (grey), " ready" when it has just become the next one.
  const goState = () => (goHere ? " here" : goReady ? " ready" : "");

  function openStep(entry) {
    openedKey = entry.key;
    setFocus(entry.step.pattern ? entry.step : null);
    goTo(entry.step);
    renderSteps();
  }

  function setFocus(step) {
    focusStep = step;
    document.body.classList.toggle("dmd-focusing", Boolean(step));
  }

  function exitFocus() {
    if (!focusStep) return;
    setFocus(null);
    renderSteps();
  }

  const focusedBlock = () =>
    focusStep && document.getElementById(`operations-${focusStep.tag}-${focusStep.operation_id}`);

  // In the open step, the fields it needs stay bright: the required ones and the ones to paste
  // ("needs"); the optional ones are dimmed with the rest. Swagger UI draws its fields again at
  // times (another content type, the step closed and opened), so they are marked again then.
  function markNeeded() {
    const block = focusedBlock();
    if (!block) return;
    const needs = new Set(focusStep.needs || []);
    for (const row of block.querySelectorAll("tr[data-param-name], tr[data-property-name]")) {
      const name = row.getAttribute("data-param-name") || row.getAttribute("data-property-name");
      const required = Boolean(row.querySelector(".parameter__name.required"));
      row.classList.toggle("dmd-need", required || needs.has(name));
    }
  }
  let marking = false;
  const markSoon = () => {
    if (marking || !focusStep) return;
    marking = true;
    requestAnimationFrame(() => {
      marking = false;
      markNeeded();
    });
  };

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") exitFocus();
  });
  // A click anywhere else (another endpoint, the cards, the top of the page) shows it all again.
  document.addEventListener(
    "click",
    (event) => {
      if (!focusStep || !(event.target instanceof Element)) return;
      const block = focusedBlock();
      if ((block && block.contains(event.target)) || event.target.closest(".values-panel")) return;
      exitFocus();
    },
    true
  );

  // The step an endpoint of Swagger UI is for (its method and path template).
  const stepFor = (method, path) =>
    steps.find((step) => step.pattern && step.method === String(method).toUpperCase() && new RegExp(step.pattern).test(path));

  function useSteps(React) {
    const [, update] = React.useReducer((n) => n + 1, 0);
    React.useEffect(() => {
      listeners.add(update);
      return () => listeners.delete(update);
    }, []);
  }

  // Under Execute and Clear, once this endpoint's step is done: the button to the next step.
  function nextButton(React, method, path) {
    const step = stepFor(method, path);
    const entry = nextEntry();
    if (!step || !entry || entry.step === step) return null;
    if (!entries().some((item) => item.step === step && done.has(item.key))) return null;
    const e = React.createElement;
    const next = entry.step;
    if (next.pattern) {
      return e(
        "button",
        { type: "button", className: "btn dmd-next-go", onClick: () => openStep(entry) },
        `Go to step ${entry.label}: ${next.title}${forRound(entry)} ↓`
      );
    }
    if (next.number === lastNumber && kept.has("did_document_url")) {
      return e(
        "a",
        {
          className: "btn dmd-next-go done",
          href: kept.get("did_document_url").value,
          target: "_blank",
          rel: "noopener",
          onClick: () => {
            setFocus(null);
            finishOutside(entry);
          },
        },
        "Open your DID document ↗"
      );
    }
    // Outside this page (paying, signing): its card at the top says what to do.
    return e(
      "button",
      {
        type: "button",
        className: "btn dmd-next-go",
        onClick: () => {
          exitFocus();
          const card = document.querySelector("#adv-steps .adv-step.current");
          if (card) card.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
        },
      },
      `Next: step ${entry.label}, ${next.title} ↑`
    );
  }

  // ------------------------------------------------------------------ the steps' cards

  // A step's text: `code` and **bold** become elements; the text is never read as HTML.
  function rich(text) {
    const nodes = [];
    let last = 0;
    for (const match of String(text || "").matchAll(/`([^`]+)`|\*\*([^*]+)\*\*/g)) {
      if (match.index > last) nodes.push(text.slice(last, match.index));
      nodes.push(match[1] !== undefined ? element("code", {}, match[1]) : element("strong", {}, match[2]));
      last = match.index + match[0].length;
    }
    if (last < String(text || "").length) nodes.push(text.slice(last));
    return nodes;
  }

  const cardsBox = document.getElementById("adv-steps");
  const orderNote = element("p", { class: "adv-order-note" });
  const cardList = element("ol", { class: "adv-steps" });
  const orderChoices = [];
  if (cardsBox && steps.length) {
    const pills = [];
    for (const [value, label, open] of [
      ["dmd", "DMD, the DebitMyData coin", true],
      ["x402", "USDC through x402", settings.x402_open !== false],
    ]) {
      if (value === "x402" && !settings.x402_order) continue;
      const radio = element("input", { type: "radio", name: "adv-order", value });
      if (!open) radio.disabled = true;
      radio.addEventListener("change", () => {
        paying = value;
        renderSteps();
      });
      orderChoices.push(radio);
      pills.push(
        element(
          "label",
          { class: `adv-pill${open ? "" : " off"}` },
          radio,
          ` ${label}`,
          open ? null : element("small", {}, " (not open on this server yet)")
        )
      );
    }
    const restart = element("button", { type: "button", class: "adv-button secondary small" }, "Start over");
    restart.title = "Clear every form, answer and value, and open this page again";
    restart.addEventListener("click", startAgain);
    cardsBox.append(
      element(
        "div",
        { class: "adv-toolbar" },
        element("fieldset", { class: "adv-choice" }, element("legend", {}, "How the agent pays"), ...pills),
        restart
      ),
      orderNote,
      cardList
    );
  }

  function cardAction(entry, isNext) {
    const step = entry.step;
    const size = isNext ? "adv-button" : "adv-button secondary small";
    if (step.pattern) {
      const open = element("button", { type: "button", class: `${size}${isNext ? goState() : ""}` }, isNext ? `Open step ${entry.label} ↓` : "Open ↓");
      open.setAttribute("aria-label", `Open step ${entry.label}: ${step.title}`);
      open.addEventListener("click", () => (isNext ? openStep(entry) : goTo(step)));
      return open;
    }
    if (!isNext) return null;
    if (step.number === lastNumber && kept.has("did_document_url")) {
      const link = element(
        "a",
        { class: "adv-button big", href: kept.get("did_document_url").value, target: "_blank", rel: "noopener" },
        "Open your DID document"
      );
      link.addEventListener("click", () => finishOutside(entry));
      return link;
    }
    const finished = element("button", { type: "button", class: size }, "Done ✓");
    finished.addEventListener("click", () => finishOutside(entry));
    return finished;
  }

  function renderCards(next) {
    if (!cardsBox || !steps.length) return;
    const x402 = paying === "x402";
    for (const radio of orderChoices) radio.checked = (radio.value === "x402") === x402;
    const note = settings.order_notes && settings.order_notes[x402 ? "x402" : "dmd"];
    orderNote.replaceChildren(...rich(note));
    orderNote.hidden = !note;
    const cards = [];
    for (const item of entries()) {
      const step = item.step;
      const isDone = done.has(item.key);
      const isNext = Boolean(next) && next.key === item.key;
      const where = step.pattern
        ? element(
            "span",
            { class: "adv-endpoint" },
            element("span", { class: `adv-method ${step.method.toLowerCase()}` }, step.method),
            element("code", {}, step.label || "")
          )
        : element("span", { class: "adv-outside" }, step.number === lastNumber ? "In your browser" : "Outside this page");
      const card = element(
        "li",
        { class: `adv-step ${isDone ? "done" : isNext ? "current" : "waiting"}${item.round ? " round" : ""}` },
        element(
          "div",
          { class: "adv-step-head" },
          element("span", { class: "adv-num", "aria-hidden": "true" }, isDone ? "✓" : item.label),
          element(
            "div",
            { class: "adv-step-title" },
            element(
              "h3",
              {},
              `Step ${item.label}: ${step.title}`,
              item.round ? element("span", { class: "adv-round" }, forRound(item)) : null,
              isDone ? element("span", { class: "adv-done" }, " · done") : null
            ),
            where
          ),
          cardAction(item, isNext)
        )
      );
      if (isNext) {
        card.setAttribute("aria-current", "step");
        const details = element("div", { class: "adv-step-body" }, element("p", {}, ...rich(step.about)));
        if (notice) details.append(element("p", { class: "adv-notice" }, notice));
        const tip = roundTip(item) || (step.pattern && step.help);
        if (tip) details.append(element("p", { class: "adv-tip" }, ...rich(tip)));
        const fill = fillRows(step);
        if (fill) details.append(fill);
        card.append(details);
      }
      cards.push(card);
    }
    if (!next) {
      const again = element("button", { type: "button", class: "adv-button secondary small" }, "Start over");
      again.addEventListener("click", startAgain);
      cards.push(element("li", { class: "adv-step all-done" }, element("p", {}, "✓ All steps done. To register another DID: ", again)));
    }
    cardList.replaceChildren(...cards);
  }

  // The full written guide (the API description) goes into its folded section at the top;
  // its links to endpoints open them on this page.
  function copyGuide(tries = 0) {
    const target = document.querySelector("#adv-guide .adv-guide-body");
    const source = document.querySelector(".swagger-ui .info .description");
    if (!target) return;
    if (!source) {
      if (tries < 120) requestAnimationFrame(() => copyGuide(tries + 1));
      return;
    }
    const guide = source.cloneNode(true);
    guide.removeAttribute("class");
    for (const link of guide.querySelectorAll('a[href^="#/"]')) link.removeAttribute("target");
    target.replaceChildren(guide);
  }

  document.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest('.adv a[href^="#/"]') : null;
    if (!link) return;
    const [, tag, operation] = link.getAttribute("href").split("/");
    if (!tag || !operation) return;
    event.preventDefault();
    goTo({ tag: decodeURIComponent(tag), operation_id: decodeURIComponent(operation) });
  });

  // "Step N" before the path of each step's endpoint, through Swagger UI's plugin system so
  // it stays when Swagger UI draws the list again.
  const badges = new Map(
    steps.filter((step) => step.operation_id).map((step) => [`${step.tag}/${step.operation_id}`, step.number])
  );
  const StepBadges = () => ({
    wrapComponents: {
      OperationSummaryPath: (Original, system) => (props) => {
        const React = system.React;
        useSteps(React);
        const operation = props.operationProps;
        const tag = operation && operation.get("tag");
        const id = operation && operation.get("operationId");
        const number = operation && badges.get(`${tag}/${id}`);
        const path = React.createElement(Original, props);
        if (!number) return path;
        const focused = Boolean(focusStep) && focusStep.tag === tag && focusStep.operation_id === id;
        return React.createElement(
          React.Fragment,
          null,
          React.createElement(
            "span",
            { className: "vp-step-badge", title: `Step ${number} of the DID registration` },
            `Step ${number}`
          ),
          focused ? React.createElement("span", { className: "dmd-focus-mark" }) : null,
          path
        );
      },
      clear: (Original, system) => (props) => {
        const React = system.React;
        useSteps(React);
        const original = React.createElement(Original, props);
        const next = nextButton(React, props.method, props.path);
        return next ? React.createElement(React.Fragment, null, original, next) : original;
      },
    },
  });

  renderSteps();

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
    plugins: [StepBadges],
    onComplete: () => {
      copyGuide();
      new MutationObserver(markSoon).observe(document.getElementById("swagger-ui"), { childList: true, subtree: true });
    },
    requestInterceptor: (request) => {
      try {
        const url = new URL(request.url, location.href);
        const method = String(request.method || "GET").toUpperCase();
        methods.set(url.href, method);
        if (method === "POST" && url.pathname.endsWith("/v1/dids/registration-quotes")) {
          quoteControllers.set(url.href, countControllers(request.body));
        }
      } catch {
        // an address Swagger UI could not have sent either
      }
      return request;
    },
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
