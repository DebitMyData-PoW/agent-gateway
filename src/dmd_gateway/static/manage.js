"use strict";
// Manage a DID-LLM: change its owners or revoke it for good. The page prepares the change (or
// the revocation) and shows each message an owner signs: wallets sign here, in the browser's
// wallet or in a wallet app (the signature pasted back), keys with the owner-key tool. Nothing
// changes without the owners' signatures. The page never asks for a private key or a recovery
// phrase, and never sends money. It only calls this server.
(() => {
  const config = JSON.parse(document.getElementById("page-config").textContent);
  const $ = (selector) => document.querySelector(selector);
  const DID = /^did:web:[^\s/?#]+:agents:[0-9A-HJKMNP-TV-Z]{26}$/;
  const IDENTITY = /^dmd_identity_[0-9A-HJKMNP-TV-Z]{26}$/;
  const WALLET = /^(0x[0-9a-fA-F]{40}|eip155:[0-9]{1,12}:0x[0-9a-fA-F]{40})$/;
  const PUBLIC_KEY = /^ed25519:z[1-9A-HJ-NP-Za-km-z]{40,60}$/;
  const PRODUCTION_API = "https://api.debitmydata.io"; // the owner-key tool's default gateway
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const findForm = $("#find");
  const didInput = $("#did");
  const findButton = $("#find-go");
  const found = $("#found");
  const choose = $("#choose");
  const changeForm = $("#change");
  const revokeForm = $("#revoke");
  const newWallet = $("#new-wallet");
  const newKey = $("#new-key");
  const changeError = $("#change-error");
  const revokeError = $("#revoke-error");
  const steps = [1, 2, 3].map((n) => $(`#s${n}`));
  const announcer = $("#announcer");

  let identity = null; // the DID-LLM's public record (GET /v1/dids/...)
  const wallets = new Map(); // wallets found in this browser (EIP-6963)

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

  async function call(method, path, body) {
    const init = { method, headers: { Accept: "application/json" }, cache: "no-store" };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
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
    if (!result) return "Could not reach the gateway. Check your connection and try again.";
    const error = result.data && result.data.error;
    if (error && typeof error === "object") return error.message || `The request was refused (${error.code}).`;
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

  const errorCode = (result) => result && result.data && result.data.error && result.data.error.code;
  const short = (text) => (text && text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text);
  const announce = (text) => (announcer.textContent = text);
  const path = (value) => encodeURIComponent(value);
  const addressOf = (account) => String(account || "").split(":").pop();
  const isWallet = (owner) => String(owner).startsWith("eip155:");
  const ownerText = (owner) => (isWallet(owner) ? `wallet ${short(addressOf(owner))}` : `key ${short(owner)}`);
  const when = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  // The owner-key tool talks to the production gateway unless told otherwise (staging).
  const toolGateway = () => (config.apiBase && config.apiBase !== PRODUCTION_API ? `DMD_GATEWAY_API=${config.apiBase} ` : "");

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

  function show(number) {
    steps.forEach((step, index) => {
      step.classList.toggle("current", index + 1 === number);
      step.classList.toggle("done", index + 1 < number);
      step.classList.toggle("waiting", index + 1 > number);
    });
    const target = steps[number - 1];
    requestAnimationFrame(() => target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" }));
  }

  function status(node, kind, text) {
    node.className = `status ${kind}`;
    node.textContent = text;
  }

  function refuse(box, text, field) {
    box.textContent = text;
    box.hidden = false;
    if (field) field.focus();
  }

  // ------------------------------------------------------------------ wallets (EIP-6963)

  window.addEventListener("eip6963:announceProvider", (event) => {
    const detail = event.detail;
    if (!detail || !detail.provider || !detail.info) return;
    wallets.set(detail.info.uuid || detail.info.name, detail);
  });
  window.dispatchEvent(new Event("eip6963:requestProvider"));

  function walletList() {
    const list = [...wallets.values()];
    if (!list.length && window.ethereum) list.push({ info: { name: "Browser wallet" }, provider: window.ethereum });
    return list;
  }

  function walletError(error) {
    if (error && error.code === 4001) return "You cancelled it in your wallet.";
    return `The wallet answered: ${(error && error.message) || error}`;
  }

  function toHex(text) {
    return `0x${[...new TextEncoder().encode(text)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
  }

  function walletIcon(entry) {
    return entry.info.icon && /^data:image\//.test(entry.info.icon) ? element("img", { src: entry.info.icon, alt: "" }) : null;
  }

  // Signs ``message`` with the wallet ``account`` (eip155:<chain>:0x...) in a browser wallet.
  async function walletSignature(entry, account, message) {
    const accounts = await entry.provider.request({ method: "eth_requestAccounts" });
    const address = (accounts && accounts[0]) || "";
    if (address.toLowerCase() !== addressOf(account).toLowerCase()) {
      throw new Error(`Your wallet is on another account (${short(address)}). Switch to ${short(addressOf(account))} in your wallet and try again.`);
    }
    return entry.provider.request({ method: "personal_sign", params: [toHex(message), address] });
  }

  // Buttons that sign with each browser wallet, and a box to paste a signature made elsewhere.
  function walletSigning(account, message, send, note) {
    const list = walletList();
    const parts = [];
    if (list.length) {
      const row = element("div", { class: "wallets" });
      for (const entry of list) {
        const button = element("button", { type: "button" }, walletIcon(entry), `Sign with ${entry.info.name}`);
        button.addEventListener("click", async () => {
          button.disabled = true;
          status(note, "busy", "Confirm the signature in your wallet…");
          try {
            await send(await walletSignature(entry, account, message));
          } catch (error) {
            status(note, "bad", error instanceof Error && !("code" in error) ? error.message : walletError(error));
          } finally {
            button.disabled = false;
          }
        });
        row.append(button);
      }
      parts.push(row);
    } else {
      parts.push(element("p", { class: "small" }, `No wallet extension in this browser: sign the message in the app of the wallet ${short(addressOf(account))}, for example with “Sign message”, and paste the signature below.`));
    }
    parts.push(pasteBox(send, note, "0x…", list.length ? "Or paste a signature made in another wallet" : null));
    return parts;
  }

  // folded: the summary of a closed box (another way to sign), or null for an open box.
  function pasteBox(send, note, placeholder, folded, hint = null) {
    const pasted = element("textarea", { rows: 3, placeholder, "aria-label": "Signature" });
    const button = element("button", { type: "button", class: folded ? "secondary" : "" }, "Send the signature");
    button.addEventListener("click", async () => {
      const value = pasted.value.trim();
      if (!value) return status(note, "bad", "Paste the signature first.");
      if (value.startsWith("DebitMyData ")) return status(note, "bad", "That is the message to sign, not the signature: sign it, then paste the signature you get.");
      button.disabled = true;
      try {
        await send(value);
      } finally {
        button.disabled = false;
      }
    });
    const box = element("div", {}, hint && element("p", { class: "small" }, hint), pasted, element("div", { class: "actions" }, button));
    return folded ? element("details", { class: "paste" }, element("summary", {}, folded), box) : box;
  }

  // ------------------------------------------------------------------ step 1: the DID-LLM

  async function find(event) {
    if (event) event.preventDefault();
    const value = didInput.value.trim();
    found.replaceChildren();
    choose.hidden = true;
    if (!DID.test(value) && !IDENTITY.test(value)) {
      found.append(element("p", { class: "error" }, "That is not a DID-LLM: it starts with did:web: and ends with :agents: and 26 letters and digits."));
      return;
    }
    findButton.disabled = true;
    const note = element("p", { class: "status busy" }, "Looking for it…");
    found.append(note);
    const record = await call("GET", `/v1/dids/${path(value)}`).catch(() => null);
    findButton.disabled = false;
    if (!record || record.status !== 200) {
      status(note, "bad", record && record.status === 404 ? "No DID-LLM with this name on this gateway: check it." : problem(record));
      return;
    }
    identity = record.data;
    const owners = [identity.controller_wallet && ownerText(identity.controller_wallet), identity.controller_public_key && ownerText(identity.controller_public_key)].filter(Boolean);
    found.replaceChildren(
      element(
        "div",
        { class: "found-did" },
        element("p", {}, element("strong", {}, identity.display_name), ` · ${identity.status}`),
        element("p", { class: "small mono" }, identity.did),
        element("p", { class: "small" }, `Its owner${owners.length > 1 ? "s" : ""}: ${owners.join(" and ")}.`)
      )
    );
    if (identity.status === "revoked") {
      found.append(element("p", { class: "status bad" }, "This DID-LLM is revoked: nothing can change it any more."));
      return;
    }
    openChoose();
  }

  // ------------------------------------------------------------------ step 2: what to do

  function openChoose() {
    choose.hidden = false;
    changeError.hidden = true;
    revokeError.hidden = true;
    newWallet.value = identity.controller_wallet || "";
    newKey.value = identity.controller_public_key || "";
    $("#reason").value = "";
    $("#confirm-word").value = "";
    // Only an active DID-LLM changes owners; a suspended one can still be revoked by its owner.
    const active = identity.status === "active";
    const changeChoice = choose.querySelector("input[name=action][value=change]");
    changeChoice.disabled = !active;
    changeChoice.checked = active;
    choose.querySelector("input[name=action][value=revoke]").checked = !active;
    if (!active) found.append(element("p", { class: "small" }, `This DID-LLM is ${identity.status}: its owners cannot change until it is active again. Its owner can still revoke it.`));
    const both = identity.controller_wallet && identity.controller_public_key;
    $("#revoke-signer").hidden = !both;
    if (both) {
      $("#revoke-wallet").textContent = short(addressOf(identity.controller_wallet));
      $("#revoke-key").textContent = short(identity.controller_public_key);
    }
    renderUseWallet();
    syncAction();
    show(2);
  }

  function action() {
    return choose.querySelector("input[name=action]:checked").value;
  }

  function syncAction() {
    changeForm.hidden = action() !== "change";
    revokeForm.hidden = action() !== "revoke";
  }

  // "Use <wallet>": fills the new owner wallet from a browser wallet's current account.
  function renderUseWallet() {
    const holder = $("#use-wallet");
    holder.replaceChildren();
    const list = walletList();
    if (!list.length) return;
    holder.className = "use-wallet";
    for (const entry of list) {
      const button = element("button", { type: "button", class: "secondary small" }, walletIcon(entry), `Use ${entry.info.name}`);
      button.addEventListener("click", async () => {
        changeError.hidden = true;
        try {
          const accounts = await entry.provider.request({ method: "eth_requestAccounts" });
          if (!accounts || !accounts.length) throw new Error("The wallet gave no account.");
          newWallet.value = accounts[0];
        } catch (error) {
          refuse(changeError, error instanceof Error && !("code" in error) ? error.message : walletError(error));
        }
      });
      holder.append(button);
    }
  }

  function sameWallet(a, b) {
    if (!a || !b) return !a && !b;
    const network = (value) => (value.startsWith("eip155:") ? value.split(":").slice(0, 2).join(":") : "eip155:8453");
    return addressOf(a).toLowerCase() === addressOf(b).toLowerCase() && network(a) === network(b);
  }

  async function prepareChange(event) {
    event.preventDefault();
    changeError.hidden = true;
    const wallet = newWallet.value.trim();
    const key = newKey.value.trim();
    if (!wallet && !key) return refuse(changeError, "A DID-LLM keeps at least one owner: give a wallet or a key.", newWallet);
    if (wallet && !WALLET.test(wallet)) return refuse(changeError, "The wallet is an Ethereum-style address: 0x and 40 letters and digits (or eip155:<chain>:0x…).", newWallet);
    if (key && !PUBLIC_KEY.test(key)) return refuse(changeError, "The key is an Ed25519 public key: ed25519:z6Mk… (the owner-key tool prints it).", newKey);
    if (sameWallet(wallet, identity.controller_wallet) && (key || null) === (identity.controller_public_key || null)) {
      return refuse(changeError, "These are already its owners: change the wallet or the key first.", newWallet);
    }
    const body = {};
    if (wallet) body.new_controller_wallet_address = wallet;
    if (key) body.new_controller_public_key = key;
    const button = $("#change-go");
    button.disabled = true;
    const result = await call("POST", `/v1/dids/${path(identity.did)}/owner-changes`, body).catch(() => null);
    button.disabled = false;
    if (!result || result.status !== 201) return refuse(changeError, problem(result));
    renderChange(result.data);
    announce("The owner change is ready to sign.");
  }

  // ------------------------------------------------------------------ step 3: the change

  function renderChange(change) {
    show(3);
    const container = steps[2].querySelector(".body");
    const note = element("p", { class: "status" });
    const list = (owners) => owners.map((owner) => `${owner.type} ${owner.value}`).join(", ") || "-";
    const parts = [
      element("dl", { class: "owners-change" }, element("dt", {}, "Owners now"), element("dd", {}, list(change.previous_owners)), element("dt", {}, "New owners"), element("dd", {}, list(change.new_owners))),
    ];
    if (change.status === "applied") return changed(container, change);
    if (change.status !== "open") {
      parts.push(element("p", { class: "status bad" }, `This change is ${change.status}: prepare a new one.`), againButton());
      return container.replaceChildren(...parts);
    }
    parts.push(element("p", {}, `Sign before ${when(change.expires_at)} (15 minutes). Signing is free: it sends nothing. One current owner approves, and each new owner accepts.`));
    const send = (item) => async (signature) => {
      status(note, "busy", "Checking the signature…");
      const result = await call("POST", `/v1/dids/${path(change.did)}/owner-changes/${change.change_id}/signatures`, { challenge_id: item.challenge_id, signature }).catch(() => null);
      if (result && result.status === 201) {
        announce("Signature verified.");
        return renderChange(result.data);
      }
      const messages = {
        invalid_signature: "That signature does not match this owner. The message is used up: if it was needed, prepare a new change.",
        challenge_expired: "The 15 minutes ran out: prepare a new change.",
        owner_change_closed: "This change is closed: prepare a new one.",
        challenge_already_used: "This message was already signed: click Continue to see where the change stands.",
      };
      status(note, "bad", messages[errorCode(result)] || problem(result));
    };
    const approvals = change.messages.filter((item) => item.role === "approve");
    const items = element("ol", { class: "messages" });
    let keys = false;
    for (const item of change.messages) {
      const needed = item.status === "open" && (item.role === "accept" || change.waiting_for.approval);
      const role = item.role === "approve" ? `Approve, by a current owner${approvals.length > 1 ? " (one is enough)" : ""}` : "Accept, by a new owner";
      const state =
        item.status === "verified"
          ? element("span", { class: "state ok" }, "Signed ✓")
          : item.status === "open" && !needed
            ? element("span", { class: "state" }, "Not needed: approved already")
            : item.status !== "open"
              ? element("span", { class: "state bad" }, item.status)
              : element("span", { class: "state" }, "Waiting for its signature");
      const entry = element("li", {}, element("p", { class: "who" }, element("span", { class: "role" }, role), element("span", { class: "signer" }, ownerText(item.signer)), state));
      if (needed) {
        if (isWallet(item.signer)) {
          entry.append(element("p", { class: "small" }, `Sign with the wallet ${short(addressOf(item.signer))}:`), ...walletSigning(item.signer, item.message, send(item), note));
        } else {
          keys = true;
          entry.append(
            element("p", { class: "small" }, "Signed with the owner-key tool: see below."),
            element(
              "details",
              { class: "paste" },
              element("summary", {}, "Or sign it with other software and paste the signature"),
              pasteBox(send(item), note, "The signature", null, "Ed25519 signature of the message: base64url, base64 or hex.")
            )
          );
        }
      }
      entry.append(element("details", {}, element("summary", {}, "The message"), element("pre", { class: "message" }, item.message), element("div", { class: "actions" }, copyButton(item.message, "Copy the message"))));
      items.append(entry);
    }
    parts.push(items);
    const again = element("button", { type: "button", class: keys ? "" : "secondary" }, "Continue");
    again.addEventListener("click", () => refresh(change, again, note));
    if (keys) {
      const command = `${toolGateway()}python3 did-owner-key.py sign-change ~/did-keys ${change.did} ${change.change_id}`;
      parts.push(
        element("p", { class: "small" }, "The keys sign with ", element("a", { href: config.ownerKeyGuide, target: "_blank", rel: "noopener" }, "the owner-key tool"), " (~/did-keys is the folder of your key files: the tool signs with the ones this change needs):"),
        element("div", { class: "copyline" }, element("code", {}, command), copyButton(command)),
        element("div", { class: "elsewhere" }, element("p", {}, "Once the tool says it signed, click Continue: nothing needs pasting here."), element("div", { class: "actions" }, again))
      );
    } else {
      parts.push(element("div", { class: "actions" }, again));
    }
    parts.push(note);
    container.replaceChildren(...parts);
  }

  async function refresh(change, button, note) {
    button.disabled = true;
    status(note, "busy", "Checking…");
    const result = await call("GET", `/v1/dids/${path(change.did)}/owner-changes/${change.change_id}`).catch(() => null);
    button.disabled = false;
    if (!result || result.status !== 200) return status(note, "bad", problem(result));
    renderChange(result.data);
    if (result.data.status === "open") {
      const after = steps[2].querySelector(".body .status");
      if (after) status(after, "", "Still waiting for the signatures marked above.");
    }
  }

  function againButton() {
    const again = element("button", { type: "button" }, "Prepare a new one");
    again.addEventListener("click", () => {
      steps[2].querySelector(".body").replaceChildren(element("p", { class: "small locked" }, "Opens once the change or the revocation is ready."));
      find();
    });
    return element("div", { class: "actions" }, again);
  }

  function changed(container, change) {
    steps[2].classList.add("done");
    steps[2].classList.remove("current");
    choose.hidden = true;
    const document_ = element("a", { class: "big", href: identity.did_document_url, target: "_blank", rel: "noopener" }, "Open its DID-LLM document");
    container.replaceChildren(
      element(
        "div",
        { class: "result" },
        element("h2", {}, "✓ Your DID-LLM has its new owners"),
        element("div", { class: "did" }, change.did),
        element("p", {}, `Its owners now: ${change.new_owners.map((owner) => ownerText(owner.value)).join(" and ")}. Only they can sign for it. Every app or agent signed in before must sign in again. Its credential, entitlements and directory listing stay.`),
        document_,
        element("div", { class: "links" }, element("a", { href: `?did=${path(change.did)}` }, "Manage it again"), element("a", { href: config.getDid }, "Get a DID-LLM"))
      )
    );
    announce("Your DID-LLM has its new owners.");
    requestAnimationFrame(() => document_.focus({ preventScroll: true }));
  }

  // ------------------------------------------------------------------ revocation

  function revokeSigner() {
    if (identity.controller_wallet && identity.controller_public_key) return revokeForm.querySelector("input[name=revoke-signer]:checked").value;
    return identity.controller_wallet ? "eip155" : "ed25519";
  }

  async function prepareRevoke(event) {
    event.preventDefault();
    revokeError.hidden = true;
    const reason = $("#reason").value.trim().replace(/\s+/g, " ");
    const [low, high] = config.reasonLength;
    if (reason && (reason.length < low || reason.length > high)) return refuse(revokeError, `Give a reason of ${low} to ${high} characters, or none.`, $("#reason"));
    if ($("#confirm-word").value.trim() !== "REVOKE") return refuse(revokeError, "Type REVOKE (in capitals) to confirm: it cannot be undone.", $("#confirm-word"));
    show(3);
    const container = steps[2].querySelector(".body");
    const note = element("p", { class: "status" });
    if (revokeSigner() === "ed25519") {
      // The owner-key tool makes the message, shows it, asks REVOKE again and signs it itself.
      const command = `${toolGateway()}python3 did-owner-key.py revoke ~/did-keys ${identity.did}`;
      const check = element("button", { type: "button" }, "Continue");
      check.addEventListener("click", () => checkRevoked(check, note));
      container.replaceChildren(
        element("p", {}, "The owner key signs the revocation with ", element("a", { href: config.ownerKeyGuide, target: "_blank", rel: "noopener" }, "the owner-key tool"), ` (~/did-keys is the folder of your key files: the tool finds the one that owns this DID-LLM, ${short(identity.controller_public_key)}). It shows what you sign and asks you to type REVOKE again. It records the reason “revoked by its owner”.`),
        element("div", { class: "copyline" }, element("code", {}, command), copyButton(command)),
        element("div", { class: "elsewhere" }, element("p", {}, "Once the tool says it is revoked, click Continue."), element("div", { class: "actions" }, check)),
        note
      );
      return;
    }
    container.replaceChildren(element("p", { class: "status busy" }, "Preparing the message…"));
    const body = { controller_type: "eip155" };
    if (reason) body.reason = reason;
    const result = await call("POST", `/v1/dids/${path(identity.did)}/revocations`, body).catch(() => null);
    if (!result || result.status !== 201) {
      container.replaceChildren(element("p", { class: "status bad" }, problem(result)), againButton());
      return;
    }
    const challenge = result.data;
    const send = async (signature) => {
      status(note, "busy", "Checking the signature…");
      const answer = await call("POST", `/v1/dids/${path(challenge.did)}/revocations/${challenge.challenge_id}/signature`, { signature }).catch(() => null);
      if (answer && answer.status === 201) return revoked(container);
      const messages = {
        invalid_signature: "That signature does not match the owner wallet. The message is used up: prepare a new one.",
        challenge_expired: "The 15 minutes ran out: prepare a new one.",
        challenge_already_used: "This message was already used: prepare a new one.",
      };
      status(note, "bad", messages[errorCode(answer)] || problem(answer));
      if (messages[errorCode(answer)]) note.after(againButton());
    };
    container.replaceChildren(
      element("div", { class: "warning" }, element("p", {}, element("strong", {}, "Signing this message revokes the DID-LLM for good."), ` Sign before ${when(challenge.expires_at)}.`)),
      element("pre", { class: "message" }, challenge.message),
      element("div", { class: "actions" }, copyButton(challenge.message, "Copy the message")),
      element("p", { class: "small" }, `Sign with the owner wallet ${short(addressOf(challenge.controller))}:`),
      ...walletSigning(challenge.controller, challenge.message, send, note),
      note
    );
    announce("The revocation message is ready to sign.");
  }

  async function checkRevoked(button, note) {
    button.disabled = true;
    status(note, "busy", "Checking…");
    const record = await call("GET", `/v1/dids/${path(identity.did)}`).catch(() => null);
    button.disabled = false;
    if (!record || record.status !== 200) return status(note, "bad", problem(record));
    if (record.data.status === "revoked") return revoked(steps[2].querySelector(".body"));
    status(note, "bad", "It is not revoked yet: run the command first, then click Continue.");
  }

  function revoked(container) {
    steps[2].classList.add("done");
    steps[2].classList.remove("current");
    choose.hidden = true;
    container.replaceChildren(
      element(
        "div",
        { class: "result" },
        element("h2", {}, "Revoked for good"),
        element("div", { class: "did" }, identity.did),
        element("p", {}, "The DID-LLM, its credential and its entitlements no longer work, and it has left the directory. Every app or agent signed in with it is signed out."),
        element("div", { class: "links" }, element("a", { href: config.getDid }, "Get a new DID-LLM"))
      )
    );
    announce("The DID-LLM is revoked.");
  }

  // ------------------------------------------------------------------ start

  findForm.addEventListener("submit", find);
  changeForm.addEventListener("submit", prepareChange);
  revokeForm.addEventListener("submit", prepareRevoke);
  for (const radio of choose.querySelectorAll("input[name=action]")) radio.addEventListener("change", syncAction);
  for (const form of [changeForm, revokeForm]) form.addEventListener("input", () => ((form === changeForm ? changeError : revokeError).hidden = true));
  const given = new URLSearchParams(window.location.search).get("did");
  if (given) {
    didInput.value = given;
    find();
  }
})();
