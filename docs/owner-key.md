# Own a DID-LLM with a key instead of a wallet

Every DID-LLM has an owner, who proves it once by signing one message. Most people use a
wallet ("My wallet" on the Get a DID-LLM page). AI agents use their own Ed25519 key ("My
agent's public key"), and their software signs by itself.

[`scripts/did-owner-key.py`](../scripts/did-owner-key.py) is for anyone who wants a key
without a wallet app: it makes an Ed25519 owner key on your own computer and signs the
message for you. The private key never leaves your computer: only the public key and the
signature are sent to the gateway.

## What you need

- Python 3.10 or later, and the `cryptography` package
  (Debian/Ubuntu: `sudo apt install python3-cryptography`; elsewhere: `pip install cryptography`).
- The tool itself, from this repository:

  ```bash
  curl -fsSLo did-owner-key.py https://raw.githubusercontent.com/DebitMyData-PoW/agent-gateway/main/scripts/did-owner-key.py
  ```

  It is one short file with no other dependencies: read it before running it.

## Steps

1. **Make the key** (once):

   ```bash
   mkdir -p -m 700 ~/did-keys
   python3 did-owner-key.py new ~/did-keys/my-agent.key
   ```

   It prints the public key (`ed25519:z6Mk...`). The key file is readable by you only, and the
   tool never overwrites an existing one.

2. **Back up the key file** (for example in your password manager). It is the only proof that
   the DID-LLM is yours: whoever has it can act as the owner, and nobody can recover it for you.

3. **On the Get a DID-LLM page** (<https://id.debitmydata.io>): choose **My agent's public key**,
   paste the public key, choose how to pay, then **Get my quote** and pay.

4. **Once the page shows the payment as received** (step 3, "Prove it's yours"), copy the
   registration ID with the **Copy** button at step 1 and run:

   ```bash
   python3 did-owner-key.py prove ~/did-keys/my-agent.key dmd_did_intent_...
   ```

   The tool asks the gateway for the message, checks that it is for this registration, this key
   and this gateway, signs it and sends the signature. It prints "Signed and accepted".

5. **Back on the page**, click **Continue** under "Signed with a tool…" (or reload the page and
   click **Continue**): the DID-LLM is issued. Nothing needs pasting: the tool already sent the
   signature.

The page's own countdown at step 3 does not matter here: the tool always asks for a fresh
message.

## Good to know

- **Staging:** `DMD_GATEWAY_API=https://staging-api.debitmydata.io python3 did-owner-key.py prove ...`
- **Safety checks:** the tool only talks to `https://` gateways, refuses to sign a message for
  another gateway or for a registration owned by another key, and never shows the private key.
- **Developers** who already have the gateway installed can also use
  `scripts/demo_agent.py` (new key, signing piped challenges, checking credentials).
