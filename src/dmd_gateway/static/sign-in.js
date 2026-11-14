"use strict";
// Sign in to an app with a DID-LLM (api/oauth_browser.py): name the DID-LLM, then sign one
// message with its own wallet or key, as when it was issued. The page never asks for a private
// key or a recovery phrase; signing sends nothing. It only calls this server, then goes back
// to the app.
(() => {
  const config = JSON.parse(document.getElementById("page-config").textContent);
  const $ = (selector) => document.querySelector(selector);
  const base = `/authorize/${encodeURIComponent(config.signIn)}`;
  const findForm = $("#find");
  const didInput = $("#did");
  const findButton = $("#find-go");
  const findError = $("#find-error");
  const signBox = $("#sign");
  const announcer = $("#announcer");
  const wallets = new Map(); // wallets found in this browser (EIP-6963)

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

  async function call(method, path, body) {
    const init = { method, headers: { Accept: "application/json" }, cache: "no-store" };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, init);
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        data = { text };
      }
      return { status: response.status, data };
    } catch {
      return null;
    }
  }

  function problem(result) {
    if (!result) return "Could not reach DebitMyData. Check your connection and try again.";
    const error = result.data && result.data.error;
    if (error && typeof error === "object") return error.message || `Refused (${error.code}).`;
    if (result.status === 422) return "Check the DID-LLM: it looks like did:web:…:agents:… or dmd_identity_….";
    if (result.status === 429) return "Too many tries: wait a minute and try again.";
    return `DebitMyData answered ${result.status}. Try again in a moment.`;
  }

  const code = (result) => result && result.data && result.data.error && result.data.error.code;
  const short = (text) => (text && text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text);

  function status(node, kind, text) {
    node.className = `status ${kind}`;
    node.textContent = text;
  }

  function copyButton(value, label) {
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

  window.addEventListener("eip6963:announceProvider", (event) => {
    const detail = event.detail;
    if (detail && detail.provider && detail.info) wallets.set(detail.info.uuid || detail.info.name, detail);
  });
  window.dispatchEvent(new Event("eip6963:requestProvider"));

  function walletList() {
    const list = [...wallets.values()];
    if (!list.length && window.ethereum) list.push({ info: { name: "Browser wallet" }, provider: window.ethereum });
    return list;
  }

  function toHex(text) {
    return `0x${[...new TextEncoder().encode(text)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
  }

  // ------------------------------------------------------------------ back to the app

  async function finish(note) {
    status(note, "busy", "Signed in: going back to the app…");
    const result = await call("POST", `${base}/finish`);
    if (result && result.status === 200 && result.data.redirect) {
      announcer.textContent = "Signed in. Going back to the app.";
      window.location.assign(result.data.redirect);
      return true;
    }
    status(note, "bad", code(result) === "sign_in_open" ? "Not signed yet: sign the message first." : problem(result));
    return false;
  }

  async function deny() {
    const result = await call("POST", `${base}/deny`);
    if (result && result.status === 200 && result.data.redirect) window.location.assign(result.data.redirect);
    else status(findError, "bad", problem(result));
  }

  async function sendSignature(signature, note) {
    if (!signature) return status(note, "bad", "Paste the signature first.");
    if (signature.startsWith("DebitMyData sign-in")) return status(note, "bad", "That is the message: sign it, then paste the signature you get.");
    status(note, "busy", "Checking the signature…");
    const result = await call("POST", `${base}/signature`, { signature });
    if (result && result.status === 200) return finish(note);
    status(note, "bad", problem(result));
  }

  async function signWithWallet(entry, address, message, button, note) {
    button.disabled = true;
    status(note, "busy", "Confirm the signature in your wallet…");
    try {
      const accounts = await entry.provider.request({ method: "eth_requestAccounts" });
      const account = (accounts && accounts[0]) || "";
      if (account.toLowerCase() !== address.toLowerCase()) {
        throw new Error(`Your wallet is on another account (${short(account)}). Switch to ${short(address)} and try again.`);
      }
      const signature = await entry.provider.request({ method: "personal_sign", params: [toHex(message), account] });
      await sendSignature(signature, note);
    } catch (error) {
      const text = error && error.code === 4001 ? "You cancelled it in your wallet." : (error && error.message) || String(error);
      status(note, "bad", text);
    } finally {
      button.disabled = false;
    }
  }

  function pasteBox(note, placeholder, folded, hint) {
    const pasted = element("textarea", { rows: 3, placeholder, "aria-label": "Signature" });
    const send = element("button", { type: "button", class: folded ? "secondary" : "" }, "Send the signature");
    send.addEventListener("click", () => sendSignature(pasted.value.trim(), note));
    const box = element("div", {}, hint && element("p", { class: "small" }, hint), pasted, element("div", { class: "actions" }, send));
    return folded ? element("details", { class: "paste" }, element("summary", {}, folded), box) : box;
  }

  // ------------------------------------------------------------------ the message to sign

  function render(data) {
    const note = element("p", { class: "status", role: "alert" });
    const parts = [
      element("div", { class: "who" }, element("b", {}, data.display_name || "Your DID-LLM"), data.did),
      element("p", {}, "Sign this message to sign in. It is free and sends nothing. Sign within 15 minutes."),
      element("pre", { class: "message" }, data.message),
      element("div", { class: "actions" }, copyButton(data.message, "Copy the message")),
    ];
    if (data.controller.type === "wallet") {
      const address = data.controller.value.split(":").pop();
      const list = walletList();
      if (list.length) {
        const row = element("div", { class: "wallets" });
        for (const entry of list) {
          const icon = entry.info.icon && /^data:image\//.test(entry.info.icon) ? element("img", { src: entry.info.icon, alt: "" }) : null;
          const button = element("button", { type: "button" }, icon, `Sign with ${entry.info.name}`);
          button.addEventListener("click", () => signWithWallet(entry, address, data.message, button, note));
          row.append(button);
        }
        parts.push(element("p", { class: "small" }, `Sign with the owner wallet ${short(address)}:`), row);
      } else {
        parts.push(element("p", { class: "small" }, `No wallet extension in this browser: sign the message in the owner wallet's app (${short(address)}), for example with “Sign message”, and paste the signature below.`));
      }
      parts.push(pasteBox(note, "0x…", list.length ? "Or paste a signature made in another wallet" : null, null));
    } else {
      const command = `python3 did-owner-key.py sign-in ~/did-keys ${config.authServer}${base}`;
      const signed = element("button", { type: "button" }, "Continue");
      signed.addEventListener("click", () => finish(note));
      parts.push(
        element("p", { class: "small" }, "Sign it with the owner key. With ", element("a", { href: config.ownerKeyGuide, target: "_blank", rel: "noopener" }, "the owner-key tool"), " (~/did-keys is the folder of your key files; the tool shows the message and asks before signing):"),
        element("div", { class: "copyline" }, element("code", {}, command), copyButton(command, "Copy")),
        element("p", {}, "Once the tool says you are signed in, click Continue."),
        element("div", { class: "actions" }, signed),
        pasteBox(note, "The signature", "Or sign it with other software and paste the signature", "Ed25519 signature of the message: base64url, base64 or hex.")
      );
    }
    parts.push(note);
    // The DID-LLM is chosen: its form gives way to the signing, with a way back and out.
    const row = element("div", { class: "actions" });
    const other = (data.controllers || []).find((kind) => kind !== data.controller.type);
    if (other) {
      const swap = element("button", { type: "button", class: "secondary small" }, other === "wallet" ? "Use the owner wallet instead" : "Use the owner key instead");
      swap.addEventListener("click", () => find(null, other));
      row.append(swap);
    }
    const change = element("button", { type: "button", class: "secondary small" }, "Use another DID-LLM");
    change.addEventListener("click", () => {
      signBox.hidden = true;
      findForm.hidden = false;
      didInput.focus();
    });
    const cancel = element("button", { type: "button", class: "secondary small" }, "Cancel");
    cancel.addEventListener("click", deny);
    row.append(change, cancel);
    parts.push(row);
    signBox.replaceChildren(...parts);
    findForm.hidden = true;
    signBox.hidden = false;
    announcer.textContent = "The message to sign is ready.";
  }

  async function find(event, controller) {
    if (event) event.preventDefault();
    const did = didInput.value.trim();
    if (!did) return status(findError, "bad", "Enter your DID-LLM first.");
    findButton.disabled = true;
    status(findError, "busy", "Looking it up…");
    const result = await call("POST", `${base}/did`, controller ? { did, controller } : { did });
    findButton.disabled = false;
    if (!result || result.status !== 200) {
      const unknown = code(result) === "unknown_did";
      status(findError, "bad", problem(result));
      if (unknown && config.getDid) findError.append(" ", element("a", { href: config.getDid }, "Get a DID-LLM"));
      return;
    }
    findError.textContent = "";
    render(result.data);
  }

  // Signed already (with the owner-key tool) and the page reloaded: just go back to the app.
  async function resume() {
    const result = await call("GET", `${base}/request`);
    if (!result || result.status !== 200 || result.data.status !== "approved") return;
    const note = element("p", { class: "status", role: "alert" });
    const go = element("button", { type: "button" }, "Continue");
    go.addEventListener("click", () => finish(note));
    signBox.replaceChildren(element("div", { class: "who" }, element("b", {}, "Signed"), result.data.did || ""), element("p", {}, "The message is signed. Continue to go back to the app."), element("div", { class: "actions" }, go), note);
    findForm.hidden = true;
    signBox.hidden = false;
  }

  findForm.addEventListener("submit", find);
  $("#deny").addEventListener("click", deny);
  didInput.focus();
  resume();
})();
