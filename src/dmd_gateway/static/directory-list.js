"use strict";
// List a DID-LLM in the directory: find the DID-LLM, fill in its public profile, and sign the
// listing with the DID-LLM's own wallet or key (as when it was issued). The page never asks for a
// private key or a recovery phrase; signing sends nothing. It only calls this server.
(() => {
  const config = JSON.parse(document.getElementById("page-config").textContent);
  const $ = (selector) => document.querySelector(selector);
  const DID = /^did:web:[^\s/?#]+:agents:[0-9A-HJKMNP-TV-Z]{26}$/;
  const IDENTITY = /^dmd_identity_[0-9A-HJKMNP-TV-Z]{26}$/;
  const MAX_WORDS = 100;
  const PRODUCTION_API = "https://api.debitmydata.io"; // the owner-key tool's default gateway
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const findForm = $("#find");
  const didInput = $("#did");
  const findButton = $("#find-go");
  const found = $("#found");
  const profileForm = $("#profile");
  const fields = ["entity_type", "specialty", "description", "owner_name", "region", "country", "created_on"];
  const signerBox = $("#signer");
  const agree = $("#agree");
  const profileError = $("#profile-error");
  const publishButton = $("#publish");
  const removeButton = $("#remove");
  const words = $("#words");
  const steps = [1, 2, 3].map((n) => $(`#s${n}`));
  const announcer = $("#announcer");

  let identity = null; // the DID-LLM's public record (GET /v1/dids/...)
  let listed = null; // its listing, when it is in the directory
  let wallet = null; // { provider, address, name } once a wallet is connected
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
    // Never from the browser's cache: the directory's answers may be cached for a minute.
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
      return message ? `The gateway refused it: ${message}.` : `The request was refused (${result.status}).`;
    }
    if (result.status === 429) return "Too many requests: wait a minute and try again.";
    return `The gateway answered ${result.status}. Try again in a moment.`;
  }

  const errorCode = (result) => result && result.data && result.data.error && result.data.error.code;
  const short = (text) => (text && text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text);
  const announce = (text) => (announcer.textContent = text);
  const path = (value) => encodeURIComponent(value);

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

  // ------------------------------------------------------------------ step 1: the DID-LLM

  async function find(event) {
    if (event) event.preventDefault();
    const value = didInput.value.trim();
    found.replaceChildren();
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
      status(note, "bad", record && record.status === 404 ? "No DID-LLM with this name on this gateway: check it, or get one first." : problem(record));
      return;
    }
    identity = record.data;
    if (identity.status !== "active") {
      status(note, "bad", `This DID-LLM is ${identity.status}: only an active DID-LLM can be in the directory.`);
      return;
    }
    const current = await call("GET", `/v1/directory/${path(identity.did)}`).catch(() => null);
    listed = current && current.status === 200 ? current.data : null;
    const owners = [identity.controller_wallet && `wallet ${short(identity.controller_wallet.split(":").pop())}`, identity.controller_public_key && `key ${short(identity.controller_public_key)}`].filter(Boolean);
    found.replaceChildren(
      element(
        "div",
        { class: "found-did" },
        element("p", {}, element("strong", {}, identity.display_name), listed ? " · in the directory now" : " · not in the directory yet"),
        element("p", { class: "small mono" }, identity.did),
        element("p", { class: "small" }, `Its owner: ${owners.join(" and ")}. Only the owner can sign the listing.`)
      )
    );
    openProfile();
  }

  // ------------------------------------------------------------------ step 2: the profile

  function openProfile() {
    profileForm.hidden = false;
    for (const name of fields) $(`#${name}`).value = (listed && listed[name]) || "";
    countWords();
    const both = identity.controller_wallet && identity.controller_public_key;
    signerBox.hidden = !both;
    if (both) {
      $("#signer-wallet").textContent = short(identity.controller_wallet.split(":").pop());
      $("#signer-key").textContent = short(identity.controller_public_key);
    }
    publishButton.textContent = listed ? "Sign and save the changes" : "Sign and publish";
    removeButton.hidden = !listed;
    profileError.hidden = true;
    show(2);
    requestAnimationFrame(() => $("#entity_type").focus({ preventScroll: true }));
  }

  function signer() {
    if (identity.controller_wallet && identity.controller_public_key) return profileForm.querySelector("input[name=signer]:checked").value;
    return identity.controller_wallet ? "eip155" : "ed25519";
  }

  function countWords() {
    const count = $("#description").value.trim().split(/\s+/).filter(Boolean).length;
    words.textContent = `${count} / ${MAX_WORDS} words`;
    words.classList.toggle("over", count > MAX_WORDS);
    return count;
  }

  function refuse(text, field) {
    profileError.textContent = text;
    profileError.hidden = false;
    const input = field && $(`#${field}`);
    if (input) input.focus();
  }

  function profile() {
    const values = {};
    for (const name of fields) {
      const value = $(`#${name}`).value.trim();
      values[name] = value || null;
    }
    return values;
  }

  async function publish(event) {
    event.preventDefault();
    profileError.hidden = true;
    const values = profile();
    if (!values.entity_type) return refuse("Choose the type.", "entity_type");
    if (!values.specialty || values.specialty.length < 3) return refuse("Write its specialty (3 characters at least).", "specialty");
    if (!values.description || values.description.length < 20) return refuse("Write a short description (20 characters at least).", "description");
    if (countWords() > MAX_WORDS) return refuse(`Keep the description to ${MAX_WORDS} words at most.`, "description");
    if (!agree.checked) return refuse("Tick the box to agree that the profile is published.", "agree");
    await askToSign({ listed: true, profile: values, controller_type: signer() }, publishButton);
  }

  async function remove() {
    profileError.hidden = true;
    await askToSign({ listed: false, controller_type: signer() }, removeButton);
  }

  async function askToSign(body, button) {
    button.disabled = true;
    const result = await call("POST", `/v1/directory/${path(identity.did)}/challenges`, body).catch(() => null);
    button.disabled = false;
    if (!result || result.status !== 201) {
      const field = result && result.data && result.data.error && result.data.error.field;
      return refuse(problem(result), field);
    }
    renderSign(result.data);
  }

  // ------------------------------------------------------------------ step 3: signing

  function renderSign(challenge) {
    show(3);
    const container = steps[2].querySelector(".body");
    const note = element("p", { class: "status" });
    const what = challenge.listed ? "publishes this profile" : "removes the DID-LLM from the directory";
    const parts = [
      element("p", {}, `Signing this message ${what}. It is free and sends nothing. Sign within 15 minutes.`),
      element("pre", { class: "message" }, challenge.message),
      element("div", { class: "actions" }, copyButton(challenge.message, "Copy the message")),
    ];
    if (challenge.controller.startsWith("eip155:")) {
      const address = challenge.controller.split(":").pop();
      const list = walletList();
      if (list.length) {
        const row = element("div", { class: "wallets" });
        for (const entry of list) {
          const icon = entry.info.icon && /^data:image\//.test(entry.info.icon) ? element("img", { src: entry.info.icon, alt: "" }) : null;
          const button = element("button", { type: "button" }, icon, `Sign with ${entry.info.name}`);
          button.addEventListener("click", () => signWithWallet(entry, address, challenge, button, note));
          row.append(button);
        }
        parts.push(element("p", { class: "small" }, `Sign with the owner wallet ${short(address)}:`), row);
      } else {
        parts.push(element("p", { class: "small" }, `No wallet extension in this browser: sign the message in the app of the owner wallet (${short(address)}), for example with “Sign message”, and paste the signature below.`));
      }
      parts.push(pasteBox(challenge, note, "0x…", list.length > 0 ? "Or paste a signature made in another wallet" : null));
    } else {
      // The tool talks to the production gateway unless told otherwise: name this one (staging).
      const audience = (challenge.message.match(/^Audience: (\S+)$/m) || [])[1] || "";
      const gateway = audience && audience !== PRODUCTION_API ? `DMD_GATEWAY_API=${audience} ` : "";
      const command = `${gateway}python3 did-owner-key.py directory ~/did-keys/my-agent.key ${challenge.challenge_id}`;
      // The usual way for a person: the tool signs and sends the signature itself, then Continue.
      // Pasting a signature made with other software is the other way, folded below.
      const signed = element("button", { type: "button" }, "Continue");
      signed.addEventListener("click", () => checkSignedElsewhere(challenge, signed, note));
      parts.push(
        element("p", { class: "small" }, "Sign it with the owner key. With ", element("a", { href: config.ownerKeyGuide, target: "_blank", rel: "noopener" }, "the owner-key tool"), ", use your own key file:"),
        element("div", { class: "copyline" }, element("code", {}, command), copyButton(command)),
        element("div", { class: "elsewhere" }, element("p", {}, "Once the tool says the listing was accepted, click Continue: nothing needs pasting here."), element("div", { class: "actions" }, signed)),
        pasteBox(challenge, note, "The signature", "Or sign it with other software and paste the signature", "Ed25519 signature of the message: base64url, base64 or hex.")
      );
    }
    parts.push(note);
    container.replaceChildren(...parts);
    announce("The message to sign is ready.");
  }

  // folded: the summary of a closed box (another way to sign), or null for an open box (the way).
  function pasteBox(challenge, note, placeholder, folded, hint = null) {
    const pasted = element("textarea", { rows: 3, placeholder, "aria-label": "Signature" });
    const send = element("button", { type: "button", class: folded ? "secondary" : "" }, "Send the signature");
    send.addEventListener("click", () => sendProof(challenge, pasted.value.trim(), note));
    const box = element("div", {}, hint && element("p", { class: "small" }, hint), pasted, element("div", { class: "actions" }, send));
    return folded ? element("details", { class: "paste" }, element("summary", {}, folded), box) : box;
  }

  async function signWithWallet(entry, address, challenge, button, note) {
    button.disabled = true;
    status(note, "busy", "Confirm the signature in your wallet…");
    try {
      const accounts = await entry.provider.request({ method: "eth_requestAccounts" });
      wallet = { provider: entry.provider, address: (accounts && accounts[0]) || "", name: entry.info.name };
      if (wallet.address.toLowerCase() !== address.toLowerCase()) {
        throw new Error(`Your wallet is on another account (${short(wallet.address)}). Switch to ${short(address)} in your wallet and try again.`);
      }
      const signature = await wallet.provider.request({ method: "personal_sign", params: [toHex(challenge.message), wallet.address] });
      await sendProof(challenge, signature, note);
    } catch (error) {
      status(note, "bad", error instanceof Error && !("code" in error) ? error.message : walletError(error));
    } finally {
      button.disabled = false;
    }
  }

  async function sendProof(challenge, signature, note) {
    if (!signature) return status(note, "bad", "Paste the signature first.");
    if (signature.startsWith("DebitMyData directory listing")) {
      return status(note, "bad", "That is the message to sign, not the signature: sign it, then paste the signature you get.");
    }
    status(note, "busy", "Checking the signature…");
    const result = await call("POST", `/v1/directory/${path(identity.did)}/proofs`, { challenge_id: challenge.challenge_id, signature }).catch(() => null);
    if (result && result.status === 201) return done(result.data);
    const messages = {
      invalid_signature: "That signature does not match the owner. The message is used up: get a new one.",
      challenge_expired: "The 15 minutes ran out: get a new message.",
      challenge_already_used: "This message was already used: get a new one.",
    };
    status(note, "bad", messages[errorCode(result)] || problem(result));
    if (messages[errorCode(result)]) note.after(againButton());
  }

  async function checkSignedElsewhere(challenge, button, note) {
    button.disabled = true;
    status(note, "busy", "Checking…");
    const result = await call("GET", `/v1/directory/challenges/${challenge.challenge_id}`).catch(() => null);
    button.disabled = false;
    if (!result || result.status !== 200) return status(note, "bad", problem(result));
    if (result.data.status === "verified") {
      const current = await call("GET", `/v1/directory/${path(identity.did)}`).catch(() => null);
      const shown = current && current.status === 200;
      return done({ listed: challenge.listed, shown, entry: shown ? current.data : null });
    }
    const reasons = {
      open: "The gateway has no signature for this message yet: run the tool first, then click Continue.",
      rejected: "The tool's signature did not match the owner: the message is used up, get a new one.",
      expired: "The 15 minutes ran out: get a new message.",
    };
    status(note, "bad", reasons[result.data.status] || `This message is ${result.data.status}.`);
    if (result.data.status !== "open") note.after(againButton());
  }

  function againButton() {
    const again = element("button", { type: "button" }, "Get a new message");
    again.addEventListener("click", () => {
      again.remove();
      show(2);
    });
    return element("div", { class: "actions" }, again);
  }

  function done(result) {
    const container = steps[2].querySelector(".body");
    steps[2].classList.add("done");
    steps[2].classList.remove("current");
    profileForm.hidden = true;
    let box;
    if (!result.listed) {
      listed = null;
      box = element("div", { class: "result" }, element("h2", {}, "✓ Removed from the directory"), element("p", {}, "Its profile is gone from the directory. You can list it again at any time."));
      announce("Removed from the directory.");
    } else if (!result.shown) {
      box = element(
        "div",
        { class: "result" },
        element("h2", {}, "Saved, but not shown"),
        element("p", {}, "DebitMyData hid this listing, as it seemed to break the rules: your change is saved, but the directory does not show it. Write to ", element("a", { href: "mailto:info@debitmydata.com" }, "info@debitmydata.com"), " to ask why.")
      );
    } else {
      listed = result.entry;
      const suffix = identity.did.split(":").pop();
      const open = element("a", { class: "big", href: `/directory/${suffix}` }, "See it in the directory");
      box = element(
        "div",
        { class: "result" },
        element("h2", {}, "✓ Listed in the directory"),
        element("div", { class: "did" }, identity.display_name),
        open,
        element("div", { class: "links" }, element("a", { href: "/directory" }, "The whole directory"), element("a", { href: `/v1/directory/${path(identity.did)}`, target: "_blank", rel: "noopener" }, "As JSON"))
      );
      announce("Listed in the directory.");
      requestAnimationFrame(() => open.focus({ preventScroll: true }));
    }
    const again = element("button", { type: "button", class: "secondary" }, "Change the listing");
    again.addEventListener("click", () => find());
    box.append(element("div", { class: "actions" }, again));
    container.replaceChildren(box);
  }

  // ------------------------------------------------------------------ start

  findForm.addEventListener("submit", find);
  profileForm.addEventListener("submit", publish);
  removeButton.addEventListener("click", remove);
  $("#description").addEventListener("input", countWords);
  const given = new URLSearchParams(window.location.search).get("did");
  if (given) {
    didInput.value = given;
    find();
  }
})();
