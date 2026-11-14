# Own a DID-LLM with a key instead of a wallet

Every DID-LLM has an owner, who proves it once by signing one message. Most people use a
wallet ("My wallet" on the Get a DID-LLM page). AI agents use their own Ed25519 key ("My
agent's public key"), and their software signs by itself.

[`scripts/did-owner-key.py`](../scripts/did-owner-key.py) is for anyone who wants a key
without a wallet app: it makes an Ed25519 owner key on your own computer and signs the
message for you. The private key never leaves your computer: only the public key and the
signature are sent to the gateway.

> **On the staging server (tests)?** The commands below are for the real service
> (<https://id.debitmydata.io>). On staging, see [Staging](#staging-the-test-server) first:
> one setting goes in front of the `prove` and `directory` commands.

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

4. **Once the page shows the payment as received** (step 3, "Prove it's yours"), the page
   shows the command with your registration ID. Copy it and run it:

   ```bash
   python3 did-owner-key.py prove ~/did-keys dmd_did_intent_...
   ```

   `~/did-keys` is the folder of your key files: the tool uses the one that owns this
   registration and says which (you can also give one key file). It asks the gateway for the
   message, checks that it is for this registration, this key and this gateway, signs it and
   sends the signature. It prints "Signed and accepted". If none of your key files owns the
   registration, it says so and signs nothing.

5. **Back on the page**, click **Continue** (under the command; or reload the page and click
   **Continue**): the DID-LLM is issued. Nothing needs pasting: the tool already sent the
   signature.

The page's own countdown at step 3 does not matter here: the tool always asks for a fresh
message.

## List it in the directory (optional)

Once the DID-LLM is issued, its owner can list it in the [DID-LLM directory](directory.md). On
the directory's **List your DID-LLM** page, fill in the profile and click **Sign and publish**,
then run the command the page shows:

```bash
python3 did-owner-key.py directory ~/did-keys dmd_challenge_...
```

The tool finds the key file that owns the DID-LLM in `~/did-keys`, shows what will be published and asks you to confirm before it signs. Then click
**Continue** on the page. Changing or removing the listing works the same way. On staging the
page's command starts with `DMD_GATEWAY_API=https://staging-api.debitmydata.io`: without it,
the tool asks the production gateway, which does not know staging's messages.

## Sign in as the DID-LLM (OAuth)

Where DebitMyData's authorization server is open (on staging for now:
[staging.md](staging.md#9-oauth-phase-1-the-authorization-server)), the same key signs the
DID-LLM in and gets an **access token** for the Agentic Superhighway (the MCP server), valid
for 10 minutes:

```bash
DMD_GATEWAY_API=https://staging-api.debitmydata.io python3 did-owner-key.py token ~/did-keys did:web:staging-api.debitmydata.io:agents:...
```

- The tool reads the DID-LLM's public key from its DID document, finds the key file that owns
  it, signs a short request valid for one minute and used once, and sends it to the
  authorization server next to the gateway (`staging-auth.` for `staging-api.`, `auth.` for
  `api.`; `DMD_AUTH_SERVER` sets another).
- It prints who signed in, for which address and for how long. `--show` at the end also prints
  the token itself: keep it private, whoever holds it can act as the DID-LLM until it expires.
- `whoami` instead of `token` signs in, then asks the Agentic Superhighway who it is signed in
  as: the whole sign-in, checked from start to end (the token is never printed):

  ```bash
  DMD_GATEWAY_API=https://staging-api.debitmydata.io python3 did-owner-key.py whoami ~/did-keys did:web:staging-api.debitmydata.io:agents:...
  ```

- A DID-LLM controlled by a wallet signs in through the browser instead, which comes later.

## Staging (the test server)

Staging (<https://staging-api.debitmydata.io>) is the gateway's test copy: test coins, test
DID-LLMs, nothing real. The tool talks to the real gateway unless told otherwise, so on staging
put `DMD_GATEWAY_API=https://staging-api.debitmydata.io` in front of `prove` and `directory`:

```bash
DMD_GATEWAY_API=https://staging-api.debitmydata.io python3 did-owner-key.py prove ~/did-keys dmd_did_intent_...
DMD_GATEWAY_API=https://staging-api.debitmydata.io python3 did-owner-key.py directory ~/did-keys dmd_challenge_...
```

- Staging's pages show their commands with it already: copy them as they are.
- Without it, the tool asks the real gateway, which does not know staging's registrations: the
  tool then says it asked the real gateway and how to point it at staging.
- `new` needs nothing: a key can own DID-LLMs on both. Each DID-LLM belongs to the server that
  issued it (its DID says which: `did:web:staging-api.debitmydata.io:...` on staging).
- Staging's **guided test** page (`/docs`) makes its own throwaway key, which only that page
  has. A DID-LLM made there cannot be signed for afterwards, so it cannot be listed in the
  directory: to try the directory, make the DID-LLM on staging's **Get a DID-LLM** page with
  your own key.

## Good to know

- **Safety checks:** the tool only talks to `https://` gateways, refuses to sign a message for
  another gateway or for a registration owned by another key, and never shows the private key.
- **Which key?** Each DID-LLM can only be signed for with the key it was registered with. A
  DID-LLM made on staging's guided test page has a throwaway key that only that page had:
  nobody can sign for it afterwards, so it cannot be listed.
- **Developers** who already have the gateway installed can also use
  `scripts/demo_agent.py` (new key, signing piped challenges, checking credentials).
