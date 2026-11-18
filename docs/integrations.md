# Integrating with DebitMyData: DID-LLMs, OAuth sign-in and the MCP server

For developers who connect a product to DebitMyData: an agent skill, an app people sign in to
with their DID-LLM, a service that checks the agents it meets, or the Agentic Hub. Everything
here is live on production and on staging (gateway 0.55.15). Technical owner: Tyler
(tyler@debitmydata.com); ask before any load test or bulk run.

## 1. Where things are

| | Production | Staging (test coins, test DID-LLMs) |
| --- | --- | --- |
| REST API and its docs | <https://api.debitmydata.io/docs/advanced> (OpenAPI: `/openapi.json`) | <https://staging-api.debitmydata.io/docs/advanced>; `/docs` is the guided test page |
| MCP server (Streamable HTTP) | <https://api.debitmydata.io/agenticsuperhighway> | <https://staging-api.debitmydata.io/agenticsuperhighway> |
| Identity endpoint (MCP, sign-in only, section 6) | <https://api.debitmydata.io/agenticsuperhighway/identity> | <https://staging-api.debitmydata.io/agenticsuperhighway/identity> |
| A2A (Agent2Agent, JSON-RPC, 1.0 and 0.3) | <https://api.debitmydata.io/agenticsuperhighway/a2a> | <https://staging-api.debitmydata.io/agenticsuperhighway/a2a> |
| OAuth authorization server | <https://auth.debitmydata.io> | <https://staging-auth.debitmydata.io> |
| Get a DID-LLM, for people | <https://id.debitmydata.io/> | <https://staging-api.debitmydata.io/get-did> |
| Issuer DID | `did:web:api.debitmydata.io` | `did:web:staging-api.debitmydata.io` |
| DID-LLMs | `did:web:api.debitmydata.io:agents:<ULID>` | `did:web:staging-api.debitmydata.io:agents:<ULID>` |

`/mcp` is the MCP server's first address and still works. Each DID-LLM belongs to the server
that issued it: a staging DID-LLM is unknown to production, and the other way round.

**A2A agents** (Agent2Agent protocol, 1.0, or 0.3 without an `A2A-Version` header) get the
same tools as the MCP server, the ones that need no sign-in, as A2A skills with the same names
and arguments. Send `SendMessage` (`message/send` in 0.3) with a data part
`{"skill": "debitmydata_get_did_registration_offer", "arguments": {}}`; the answer is a message
whose data part is the tool's result (its text part is the same JSON). A message without a
skill gets the list of skills with their arguments. Every answer is immediate: no tasks, no
streaming, no push notifications, and nothing to sign in to.

Discovery files, on the API's address:

| File | What |
| --- | --- |
| `/llms.txt` | the whole service in plain words, for agents |
| `/.well-known/agent-card.json` | the A2A Agent Card: the A2A endpoint and its skills (below) |
| `/.well-known/agent-skills/index.json` | the gateway's own Agent Skill, `debitmydata-did-llm` |
| `/.well-known/mcp`, `/.well-known/mcp/server-card.json` | the MCP server card: tools, sign-in (`oauth2.1`, `wallet-signature`) |
| `/.well-known/api-catalog` | the API catalog (RFC 9727) |
| `/.well-known/oauth-protected-resource/agenticsuperhighway` | which authorization server issues tokens for the MCP server (RFC 9728) |
| `/.well-known/oauth-protected-resource/agenticsuperhighway/identity` | the same, for the identity endpoint |
| `/.well-known/google-connected-apps` | Google Connected Apps' domain verification, once Google's console gives the file |
| `/.well-known/oauth-authorization-server` (on the `auth` address) | the authorization server's metadata (RFC 8414) |
| `/v1/discovery`, `/v1/payment-options` | services, prices and the payment options enabled right now |

Source: <https://github.com/DebitMyData-PoW/agent-gateway> (this repository). The Codex skill
for staging registration is <https://github.com/DebitMyData-PoW/debitmydata-skill>.

## 2. What a DID-LLM is

A W3C `did:web` DID for an agent, a bot, a person or an organization, with a credential signed
by DebitMyData. $25.00, paid once, with no account and no password: whoever holds the DID-LLM's
**owner key** (Ed25519) or **owner wallet** (an EVM address) owns it, and every action on it is
a message signed by that owner.

- DID document: `https://api.debitmydata.io/agents/<ULID>/did.json`. An owner key is a
  `Multikey` verification method; an owner wallet an `EcdsaSecp256k1RecoveryMethod2020` with
  its `blockchainAccountId`. A suspended or revoked DID-LLM's document answers **410**.
- Credential: W3C Verifiable Credential 2.0, type `DebitMyDataRegistrationCredential`, a
  `DataIntegrityProof` (`eddsa-jcs-2022`) by the issuer key in `/.well-known/did.json`
  (`GET /v1/credentials/{credential_id}`). Product receipts (`DebitMyDataProductReceipt`, for
  example a BottyBotty tier) are signed the same way. To check one, take the key whose `id` is
  the proof's `verificationMethod` and that `assertionMethod` lists: since 0.53.0 the document
  can list more than one key, the current one first, then earlier ones kept after the key was
  replaced, which still verify what they signed (never the first key blindly).
- Status: `active`, `suspended` (DebitMyData, can be undone) or `revoked` (for good: by
  DebitMyData, or by the owner since 0.48.0).
- Entitlements (`GET /v1/entitlements/{identity_id}`): `dmd_did_holder` for every DID-LLM, and
  the add-ons bought for it (BottyBotty tiers, not on sale in production yet).
- `participant_type`, chosen when buying: `autonomous_agent`, `ai_agent`, `bot`,
  `agenticbot`, `claw`, `digital_twin`, `human`, `organization`, `approved_digital_entity`.

Buying one: the REST steps are on `/docs` and in [staging.md](staging.md); the MCP tools
(`debitmydata_open_checkout`, then quote, pay, prove, register) are described by the server itself
(`tools/list`) and in `llms.txt`. Payment options: DMD, card (Stripe), PayPal (from 0.51.0,
once DebitMyData's PayPal app is set), USDC on Base (x402, staging only until the Coinbase
key arrives). Card and PayPal are paid by a person, on the quote's `payment_url`.

## 3. Signatures: who signs what

Every message an owner signs is plain text, built by the gateway, with lines joined by `\n`,
no trailing newline. The owner signs the **exact UTF-8 bytes**:

- **Owner key (Ed25519):** the raw 64-byte signature, base64url (padding optional; standard
  base64 is accepted too).
- **Owner wallet:** EIP-191 `personal_sign`, `0x` + hex. Smart wallets (ERC-1271 / ERC-6492)
  are checked on their chain on staging; in production, where the signer service has no
  network, only ordinary wallets sign in for now.

| Message (first line) | Valid | Used once | Signed by | For |
| --- | --- | --- | --- | --- |
| `DebitMyData controller proof` | 5 minutes | yes | the future owner key or wallet | proving control when buying |
| `DebitMyData sign-in` | 15 minutes | yes | the owner | a person signing in to an app (section 5) |
| `DebitMyData agent sign-in` | 5 minutes | yes | the owner wallet | an agent signing in with its wallet (section 4) |
| `DebitMyData proof of control` | 10 minutes | the verifier's job (section 6) | the owner | proving to someone else that you own the DID-LLM |
| `DebitMyData owner change` | 15 minutes | yes | a current owner approves, each new owner accepts | moving the DID-LLM to new owners |
| `DebitMyData revocation` | 15 minutes | yes | one owner | revoking it for good |
| directory listing message | 15 minutes | yes | the owner | listing it in the public directory |

Each message names its purpose, its audience (the gateway, or for a proof of control the party
that asked), the DID-LLM, a random nonce, and when it was issued and expires, so a signature
made for one thing is never accepted for another. DebitMyData itself signs two things only:
credentials (the issuer key) and OAuth tokens (the token key). In production both keys are in
a separate signer service: the API server holds neither, and the signer checks the owner's
signature itself before it signs anything for an app, and that this owner is the one the
DID-LLM's credential and signed owner changes name (0.53.0).

DebitMyData never asks for a private key, a seed phrase or wallet access.

## 4. Agents sign in (OAuth 2.1, client credentials)

An agent signs in **as its DID-LLM**: the DID-LLM is its own OAuth client, with no
registration step. Two ways:

**With the owner key** (`private_key_jwt`, RFC 7523):

```
POST https://auth.debitmydata.io/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<JWT>
&resource=https://api.debitmydata.io/agenticsuperhighway     (optional; the default)
&scope=did:read                                               (optional; the default)
```

The JWT: header `{"alg": "EdDSA", "typ": "JWT"}`; claims `iss` = `sub` = the DID-LLM,
`aud` = the token endpoint's address, `iat` and `exp` at most 5 minutes apart, `jti` a random
value (at most 128 characters). It is signed with the DID-LLM's owner key and **accepted once
only** (the `jti` is kept until it expires). `scripts/did-owner-key.py token` does all of it
([owner-key.md](owner-key.md#sign-in-as-the-did-llm-oauth)).

**With the owner wallet** (no key file, no browser):

1. `POST https://auth.debitmydata.io/challenge` with the form field `did`: the answer's
   `message` (`DebitMyData agent sign-in`) is valid 5 minutes, usable once.
2. Sign it with `personal_sign`.
3. `POST https://auth.debitmydata.io/token` with
   `grant_type=urn:debitmydata:params:oauth:grant-type:wallet-signature`, `message` and
   `signature`.

Both answer with an **access token** valid 10 minutes, no refresh token: sign in again when it
expires. Suspended and revoked DID-LLMs cannot sign in, and a key or wallet that no longer owns
the DID-LLM is refused.

## 5. People sign in to your app (authorization code + PKCE)

For an app (an MCP client, a website, an agent's own app) whose users sign in with their
DID-LLM. It is the flow MCP clients already use: ChatGPT and Claude connect with no setup.

**Client registration: a Client ID Metadata Document** (CIMD, the MCP specification's
recommended way). Nothing to ask us for: your `client_id` is the https address of a JSON file
you publish (dynamic client registration is for Google's apps only, below):

```json
{
  "client_id": "https://app.example.com/oauth/client.json",
  "client_name": "Example App",
  "redirect_uris": ["https://app.example.com/oauth/callback"],
  "token_endpoint_auth_method": "none"
}
```

- `client_id` in the file must be the file's own address. https only, a public host, no
  redirects, at most 64 KB, answered within 5 seconds; it is kept between 5 minutes and a day
  (your `Cache-Control: max-age`).
- `redirect_uris`: at most 20, https, or `http://127.0.0.1` / `localhost` / `[::1]` for an app
  on the person's own computer (any port, RFC 8252). Only listed addresses are accepted.
- Public client (`none`, with PKCE) or `private_key_jwt` with your keys in `jwks` or
  `jwks_uri` (EdDSA, RS256, PS256 or ES256).

**The flow:**

1. Send the person to `https://auth.debitmydata.io/authorize` with `response_type=code`,
   `client_id`, `redirect_uri`, `code_challenge` (S256 only) and `code_challenge_method=S256`;
   `state` (given back as is: use it), `scope=did:read` and
   `resource=https://api.debitmydata.io/agenticsuperhighway` are optional.
2. DebitMyData's page shows which site asks (your `client_name` and host), asks for the
   DID-LLM, and has its owner sign one `DebitMyData sign-in` message naming your app, the
   DID-LLM, what it allows and the MCP server: with the wallet in the browser, or with the
   owner key through `did-owner-key.py sign-in`. Five wrong signatures end the attempt.
3. The person comes back to your `redirect_uri` with `code`, `state` and `iss`
   (`https://auth.debitmydata.io`; check it). The code is valid 60 seconds, usable once.
4. `POST /token` with `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`
   and `code_verifier`: an **access token** (10 minutes) and a **refresh token**.
5. `grant_type=refresh_token` gives a new pair. Each refresh token works **once**; a used one
   presented again ends the whole sign-in (it was copied).
6. `POST /revoke` (RFC 7009, `token` and `client_id`) ends the sign-in from your side, with
   its refresh token or any of its access tokens. Every token of that sign-in stops **at
   once**: access tokens carry their sign-in (`sid`), and the MCP server checks it on every
   request.

A sign-in lasts **30 days** from the person's signature, then they sign again. It also ends
when the DID-LLM is suspended or revoked, or when its owners change (section 7), with the same
immediate effect on its access tokens.

**Google's apps (Gemini, Search)**, from 0.52.0. Google cannot use a Client ID Metadata
Document, so its apps register (RFC 7591) at `https://auth.debitmydata.io/register` (the
`registration_endpoint` of the server's metadata, once switched on) with Google's own return
addresses only: `https://oauth-redirect.googleusercontent.com/r/...` (and the
`oauth-redirect-sandbox` and `oauth-redirect-test` hosts, `/r/` or `/a/`). The answer gives a
`client_id` (an https address on Google's return host, so the person sees, and signs, that the
app is on Google) and a `client_secret`, which the app sends at `/token` and `/revoke`
(`client_secret_basic` or `client_secret_post`; kept as its SHA-256 only). These apps get
tokens for the identity endpoint only, and their refresh token stays the same at each refresh,
as Google expects; it ends with the sign-in, after 30 days, like every other: the person then
links again.

## 6. The access token, the MCP server, and checking an agent

**The token** is a JWT (RFC 9068, `typ: at+jwt`) signed RS256 by the authorization server
(its key at `https://auth.debitmydata.io/.well-known/jwks.json`):

| Claim | Value |
| --- | --- |
| `iss` | `https://auth.debitmydata.io` |
| `sub` | the DID-LLM |
| `client_id` | the DID-LLM (agent sign-in) or your CIMD address (app sign-in) |
| `aud` | the endpoint it is for (the `resource` asked): `https://api.debitmydata.io/agenticsuperhighway` (the default) or its identity endpoint, `.../agenticsuperhighway/identity` |
| `scope` | `did:read` (the only scope today: read your own DID-LLM's records) |
| `iat`, `exp` | 10 minutes |
| `jti` | random |
| `sid` | app sign-ins only: the sign-in it belongs to |

It is a **bearer** token, bound to its audience: only that endpoint accepts it, and it checks
everything on every request (signature, issuer, audience, expiry, the DID-LLM still active with
the same owners, and for an app, its sign-in not ended). So a call needs the token only, **not a
fresh signature**; a fresh signature is needed at each sign-in and for each owner action
(section 3). Without a token every tool works except `debitmydata_whoami`, which answers 401
with a `WWW-Authenticate: Bearer` header naming the resource metadata and `scope="did:read"`:
what makes MCP clients show their Connect button. A wrong or expired token is answered 401
`invalid_token`.

`debitmydata_whoami` says which DID-LLM the request is signed in as, with its status. Your own
service can check our tokens the same way: verify against the JWKS, and require our `iss`, the
`aud` you expect, and `exp`. Today only the two MCP endpoints are audiences; tell Tyler if your
service needs to be one.

**A WhoAmI screen** (since 0.55.15) shows the answer's first fields, as they are, and keeps the
rest apart:

| Field | Shown as |
| --- | --- |
| `did_llm_id` | the DID-LLM ID: the DID's suffix (the part after `agents:`), never the internal `dmd_identity_...` |
| `did` | the full DID, exactly as given (`did:web:`, the domain and the whole path): what **Copy ID** copies ("Full DID copied"), and what **Show full DID** shows |
| `display_name`, `participant_type` | the name and participant type |
| `identity_status` | the DID-LLM's own status (`active`, `suspended`, `revoked`): **not** a verification of it, and not the session |
| `entitlement` | the active entitlements' names |
| `controller_wallet` | the owner wallet (`eip155:<chain id>:0x...`); `null`: "Not linked" |
| `registered_at` | the registration date (UTC, with its offset) |
| `environment` | `staging` or `production`: the environment badge |
| `session` | **Current session**, a section of its own: `status` (`active`: a request with an ended or expired one is refused before any tool runs), `kind` (`app` or `agent`), `access` (what the sign-in allows, in words: "Read your own DID-LLM's records"), `expires_at` and `timezone` (`UTC`): an app's sign-in ends 30 days after the person signed, an agent's with its 10-minute access token |
| `advanced` | **Advanced details**: `identity_id` (internal), `controller_public_key`, `did_document_url`, `credentials` (their links), `entitlements` (with their dates and status), `owner_changed_at`, `access_token_expires_at` |

The fields of the answer before 0.55.15 (`signed_in_as`, `scope`, `token_expires_at`,
`identity`, `entitlements`) follow, unchanged. Every answer of the MCP endpoints is sent with
`Cache-Control: no-store`: no shared cache (Cloudflare's included) keeps one, and each answer is
the signed-in DID-LLM's own. An app should not keep WhoAmI's answer either: ask again.

**Tool names.** Every tool's name starts with `debitmydata_` (since 0.50.0). Until then they
started with `dmd_`: a call by such a name still works on both endpoints, but only the new
names are listed, so use the new ones in new code.

**The identity endpoint** (`/agenticsuperhighway/identity`, since 0.49.0) is a separate MCP
server for apps that only need the signed-in DID-LLM, such as a Google Connected App. It has
three read-only tools, and nothing else exists there: nothing can be bought, registered or
changed through it.

| Tool | Answers |
| --- | --- |
| `debitmydata_whoami` | the DID-LLM the request is signed in as: the WhoAmI screen's fields, its session, its record and entitlements |
| `debitmydata_get_profile` | the signed-in DID-LLM's profile: `did`, `identity_id`, `display_name`, `participant_type`, `status`, `created_at`, `owners` (`key` and/or `wallet`), `owner_changed_at`, `did_document_url`, `credentials`, `entitlements`, `directory_listing` (null when not listed) |
| `debitmydata_verify_agent` | another DID-LLM's public record (`did` only): the same answer as below, without the challenge |

Every request needs a `did:read` token issued for this endpoint (`resource=` its address),
from the first one (`initialize`, `tools/list`): without one, the endpoint answers 401 with
`WWW-Authenticate: Bearer resource_metadata="...", scope="did:read"`, which is what makes an
MCP client sign in as it connects. A token for the Agentic Superhighway is refused there (and
the other way round). `identity_id` and the DID
never change and are never reused: an owner change keeps both.

**Checking an agent you meet** (any service, no sign-in): `POST /v1/dids/verifications`, or
the MCP tool `debitmydata_verify_agent`.

1. `{"did": "...", "audience": "your service's name"}` returns the DID-LLM's status, its
   credential checked against the issuer key, its owners, its entitlements and a `challenge`:
   a `DebitMyData proof of control` message naming your audience and a nonce, valid 10 minutes.
2. The agent signs that message with its owner key or wallet and gives you the signature.
3. `{"did": "...", "message": "...", "signature": "..."}` returns the same, plus `proof`.

The answer's labels: `verified` (true only when every check passed), `found`, `status`,
`reasons` (each failed check, in words), `credential.signature_valid`, `credential.status`,
`controllers` (`key` and/or `wallet`), `entitlements`, and `proof.signature_valid`,
`proof.signed_with` (`key` or `wallet`), `proof.nonce`. DebitMyData keeps no record of the
challenge: **you** keep the nonce you were given, check that `proof.nonce` is the same, and
accept each nonce once. That is what makes the proof fresh and meant for you.

## 7. Ownership: changing owners and revoking

Since 0.48.0 owners do this themselves, with signed messages (REST below; MCP:
`debitmydata_change_owner`, `debitmydata_revoke_did`; the tool: `did-owner-key.py change-owner`
and `revoke`). `{identity}` is the DID-LLM or its `dmd_identity_...` ID.

| Request | What |
| --- | --- |
| `POST /v1/dids/{identity}/owner-changes` | the new owners (`new_controller_public_key`, `new_controller_wallet_address`, `new_controller_network`): every owner it will have afterwards. Returns the messages to sign. |
| `GET /v1/dids/{identity}/owner-changes/{change_id}` | where it stands, its messages, who still has to sign |
| `POST .../owner-changes/{change_id}/signatures` | `challenge_id` and `signature`, one at a time: one current owner **approves**, each new owner **accepts**; the change applies with the last |
| `POST /v1/dids/{identity}/revocations` | the revocation message (`controller_type`: `ed25519` or `eip155`) |
| `POST .../revocations/{challenge_id}/signature` | the signature: revoked for good |

A change not finished within 15 minutes expires. Once it applies, only the new owners can sign,
and **every access token and sign-in issued before it stops working** (agents and apps sign in
again); the credential, entitlements and directory listing stay. DebitMyData is alerted of
every change and revocation. Revocation cannot be undone.

**Not built yet:** delegation (a Master DID-LLM giving limited rights to its Minions), scopes
beyond `did:read`, and recovery when every owner key is lost (ID-14). Until then a DID-LLM has
owners only: no roles, no delegated rights. Say what you need and we will design the scope
with you.

**Agentic Avatars:** an avatar (or any agent) proves who it is with its DID-LLM: check it as in
section 6. The directory's `entity_type` (`avatar`, `robot`, `sentient`) is what the owner
declared when listing it ([directory.md](directory.md)), not something DebitMyData checked.

## 8. Testing on staging

No account, no key from us. Staging's coins and test USDC have no value.

- The guided test page <https://staging-api.debitmydata.io/docs> runs a whole registration in
  the browser. Its test tools (`/v1/staging/test-payments`, `test-agent-keys`,
  `test-signatures`, `test-wallet`) exist on staging only; an agent or skill uses only
  `test-payments`, and makes its own key.
- A DID-LLM to sign in with: make it on <https://staging-api.debitmydata.io/get-did> with your
  own key (`did-owner-key.py new`) or wallet. One made on the guided page has a throwaway key
  nobody can sign with afterwards.
- MCP clients: add `https://staging-api.debitmydata.io/agenticsuperhighway`, run
  `debitmydata_whoami`, sign in on the page it opens.
- Limits per address: reads 20 a second; quotes, challenges, proofs and registrations 60 a
  minute; MCP 5 a second; staging test tools 10 a minute; at most 5 unpaid quotes per owner
  key. Above them: 429 `rate_limited` with `Retry-After`. Test coins are limited: no load tests
  or registration loops without asking Tyler first.
- No test registrations on production.

## 9. Policies

Read at <https://id.debitmydata.io/policies> (`/policies/{name}.md` as Markdown for agents;
`GET /v1/policies` lists each version and its SHA-256): terms, privacy, payment methods,
refunds (a DID-LLM is non-refundable), DID-LLM, security. A credential records the terms
version it was bought under (`termsVersion`). Security reports: info@debitmydata.com
([SECURITY.md](../SECURITY.md)).
