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
  function startOver() {
    kept.clear();
    done.clear();
    notice = "";
    order = allSteps;
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
  // a pattern for its path) or what to do outside this page. USDC has its own order.
  const steps = settings.steps || [];
  const byNumber = new Map(steps.map((step) => [step.number, step]));
  const allSteps = steps.map((step) => step.number);
  let order = allSteps;
  const done = new Set();
  const methods = new Map(); // request URL -> method, to recognise each step's answers
  let notice = ""; // why a step is to be done again

  function nextStep() {
    let last = -1;
    order.forEach((number, index) => {
      if (done.has(number)) last = index;
    });
    return last + 1 < order.length ? byNumber.get(order[last + 1]) : null;
  }

  function progress(url, method, answer) {
    if (answer.selected_payment_option && settings.x402_order) {
      order = answer.selected_payment_option.payment_protocol === "x402" ? settings.x402_order : allSteps;
    }
    for (const step of steps) {
      if (!step.pattern || step.method !== method || !new RegExp(step.pattern).test(url.pathname)) continue;
      if (step.done_when && read(answer, step.done_when.field) !== step.done_when.value) continue;
      const position = order.indexOf(step.number);
      if (position < 0) continue;
      const again = step.again_when;
      if (again && read(answer, again.field) === again.value) {
        // Proven, but another controller is left: from its challenge on, everything again.
        const challenge = order
          .slice(0, position)
          .reverse()
          .find((number) => byNumber.get(number).restarts && !byNumber.get(number).pattern.includes("quotes"));
        if (challenge) for (const number of order.slice(order.indexOf(challenge))) done.delete(number);
        notice = again.note;
        continue;
      }
      notice = "";
      // A new quote or a new challenge starts what follows it again.
      if (step.restarts) for (const number of order.slice(position + 1)) done.delete(number);
      // What had to be done outside this page before it is done too.
      for (const number of order.slice(0, position)) if (!byNumber.get(number).pattern) done.add(number);
      done.add(step.number);
    }
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
        const value = order === settings.x402_order ? keptValue("payment_signature") : null;
        return value && { value };
      }
      case "recipient":
        return keptValue("selected_payment_option.recipient") && { value: keptValue("selected_payment_option.recipient") };
      case "amount": {
        const value = keptValue("selected_payment_option.amount");
        return value && { value, note: kept.get("selected_payment_option.amount").unit };
      }
      case "controller_type": {
        // Only when the quote's other controller is left: the gateway picks it anyway.
        const proven = keptValue("challenge_controller");
        if (!notice || !proven) return null;
        const wallet = !proven.startsWith("eip155:");
        return { value: wallet ? "eip155" : "ed25519", note: `${wallet ? "the wallet" : "the public key"}, or leave the field empty` };
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

  function renderSteps() {
    if (!steps.length) {
      nextBar.hidden = true;
      return;
    }
    const step = nextStep();
    const parts = [];
    if (!step) {
      parts.push(element("div", { class: "vp-next-line" }, element("span", { class: "vp-next-text" }, "✓ All steps done")));
    } else {
      let action;
      if (step.pattern) {
        action = element("button", { type: "button", class: "vp-go" }, "Go ↓");
        action.addEventListener("click", () => goTo(step));
      } else if (step.number === allSteps[allSteps.length - 1] && kept.has("did_document_url")) {
        action = element(
          "a",
          { class: "vp-go", href: kept.get("did_document_url").value, target: "_blank", rel: "noopener" },
          "Open"
        );
        action.addEventListener("click", () => {
          done.add(step.number);
          renderSteps();
        });
      } else {
        action = element("button", { type: "button", class: "vp-go" }, "Done ✓");
        action.addEventListener("click", () => {
          done.add(step.number);
          renderSteps();
        });
      }
      parts.push(
        element(
          "div",
          { class: "vp-next-line" },
          element("span", { class: "vp-next-text" }, "Next: ", element("strong", {}, `Step ${step.number}`), ` · ${step.title}`),
          action
        )
      );
      if (notice) parts.push(element("div", { class: "vp-notice" }, notice));
      const tip = step.outside || step.help;
      if (tip) parts.push(element("div", { class: "vp-next-help" }, tip));
      const fill = fillRows(step);
      if (fill) parts.push(fill);
    }
    const chips = element("div", { class: "vp-chips" });
    for (const number of order) {
      const item = byNumber.get(number);
      const state = done.has(number) ? " done" : step && step.number === number ? " next" : "";
      const chip = element(
        "button",
        {
          type: "button",
          class: `vp-chip${state}${item.pattern ? "" : " outside"}`,
          title: `Step ${number}: ${item.title}${done.has(number) ? " (done)" : ""}`,
          "aria-label": `Step ${number}: ${item.title}${done.has(number) ? ", done" : ""}`,
        },
        done.has(number) ? `${number} ✓` : String(number)
      );
      chip.addEventListener("click", () => goTo(item));
      chips.append(chip);
    }
    parts.push(chips);
    nextBar.replaceChildren(...parts);
    renderCards(step);
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
  const lastNumber = allSteps[allSteps.length - 1];
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
        order = value === "x402" ? settings.x402_order : allSteps;
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
    restart.addEventListener("click", startOver);
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

  function cardAction(step, isNext) {
    const size = isNext ? "adv-button" : "adv-button secondary small";
    if (step.pattern) {
      const open = element("button", { type: "button", class: size }, isNext ? `Open step ${step.number} ↓` : "Open ↓");
      open.setAttribute("aria-label", `Open step ${step.number}: ${step.title}`);
      open.addEventListener("click", () => goTo(step));
      return open;
    }
    if (!isNext) return null;
    if (step.number === lastNumber && kept.has("did_document_url")) {
      const link = element(
        "a",
        { class: "adv-button big", href: kept.get("did_document_url").value, target: "_blank", rel: "noopener" },
        "Open your DID document"
      );
      link.addEventListener("click", () => {
        done.add(step.number);
        renderSteps();
      });
      return link;
    }
    const finished = element("button", { type: "button", class: size }, "Done ✓");
    finished.addEventListener("click", () => {
      done.add(step.number);
      renderSteps();
    });
    return finished;
  }

  function renderCards(next) {
    if (!cardsBox || !steps.length) return;
    const x402 = order === settings.x402_order;
    for (const radio of orderChoices) radio.checked = (radio.value === "x402") === x402;
    const note = settings.order_notes && settings.order_notes[x402 ? "x402" : "dmd"];
    orderNote.replaceChildren(...rich(note));
    orderNote.hidden = !note;
    const cards = [];
    for (const number of order) {
      const step = byNumber.get(number);
      const isDone = done.has(number);
      const isNext = Boolean(next) && next.number === number;
      const where = step.pattern
        ? element(
            "span",
            { class: "adv-endpoint" },
            element("span", { class: `adv-method ${step.method.toLowerCase()}` }, step.method),
            element("code", {}, step.label || "")
          )
        : element("span", { class: "adv-outside" }, number === lastNumber ? "In your browser" : "Outside this page");
      const card = element(
        "li",
        { class: `adv-step ${isDone ? "done" : isNext ? "current" : "waiting"}` },
        element(
          "div",
          { class: "adv-step-head" },
          element("span", { class: "adv-num", "aria-hidden": "true" }, isDone ? "✓" : String(number)),
          element(
            "div",
            { class: "adv-step-title" },
            element("h3", {}, `Step ${number}: ${step.title}`, isDone ? element("span", { class: "adv-done" }, " · done") : null),
            where
          ),
          cardAction(step, isNext)
        )
      );
      if (isNext) {
        card.setAttribute("aria-current", "step");
        const details = element("div", { class: "adv-step-body" }, element("p", {}, ...rich(step.about)));
        if (notice) details.append(element("p", { class: "adv-notice" }, notice));
        if (step.pattern && step.help) details.append(element("p", { class: "adv-tip" }, ...rich(step.help)));
        const fill = fillRows(step);
        if (fill) details.append(fill);
        card.append(details);
      }
      cards.push(card);
    }
    if (!next) {
      cards.push(element("li", { class: "adv-step all-done" }, element("p", {}, "✓ All steps done. ", element("strong", {}, "Start over"), " to register another DID.")));
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
        const operation = props.operationProps;
        const number = operation && badges.get(`${operation.get("tag")}/${operation.get("operationId")}`);
        const path = React.createElement(Original, props);
        if (!number) return path;
        return React.createElement(
          React.Fragment,
          null,
          React.createElement(
            "span",
            { className: "vp-step-badge", title: `Step ${number} of the DID registration` },
            `Step ${number}`
          ),
          path
        );
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
    onComplete: () => copyGuide(),
    requestInterceptor: (request) => {
      try {
        methods.set(new URL(request.url, location.href).href, String(request.method || "GET").toUpperCase());
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
