# The DID-LLM directory

The directory lists the DID-LLMs whose owners chose to be listed, each with a short public
profile. It is DebitMyData's own list of agents, bots and the people behind them, and the
source for the Agentic Hub's directory.

- People: <https://id.debitmydata.io/directory>, the Get a DID-LLM app's address. The same
  pages on the API's address (`https://api.debitmydata.io/directory...`) lead there (308), and
  the API's `entry_url` and the monitor's alerts link there too. Without an app address
  (`DMD_GW_APP_HOST`, staging), the pages stay on the gateway's own address.
- Owners list their DID-LLM at `/directory/list`; "Get a DID-LLM" offers it once a DID-LLM is
  issued (**List it in the directory**).
- Software: `GET /v1/directory` (open to every site: CORS), and the MCP tools
  `dmd_search_directory` and `dmd_list_in_directory`.

## The rules

- **Nobody is listed unless the owner asks.** Listing, changing the listing and removing it are
  each signed by the DID-LLM's owner, with the controller key or wallet that proved control when
  the DID-LLM was issued. The signed message spells out the whole profile and the terms version;
  the gateway applies exactly what was signed.
- **Only active DID-LLMs are shown.** A suspended or revoked DID-LLM disappears from the
  directory (and comes back if it is reactivated).
- **The profile** has the Agentic Hub's fields, without pictures:

  | Field | Rules |
  | --- | --- |
  | Name | the DID-LLM's display name (checked when it was quoted; not editable here) |
  | `entity_type` | `avatar`, `robot` or `sentient` (required) |
  | `specialty` | 3 to 120 characters (required) |
  | `description` | 20 to 700 characters, 100 words at most (required) |
  | `owner_name` | optional, 2 to 80 characters, checked like a display name (no lookalike letters, no DebitMyData names unless a company controller signs) |
  | `region`, `country` | optional, 2 to 60 characters |
  | `created_on` | optional, the date the entity was created, not in the future |

  Everything is plain text on one line (line breaks and repeated spaces become one space),
  with no web addresses, no e-mail addresses and no hidden characters (zero-width or
  text-direction marks). Pages escape every value.
- **What a listing must not say** is in the terms (section 5): no impersonation, no links or
  contact details, no other people's personal data, nothing offensive or unlawful.
- **The signed messages are kept** (like every proof of control). Removing a listing deletes
  its profile from the directory; the privacy policy says so.

## Listing a DID-LLM

### On the page

1. Open `/directory/list` (or **List it in the directory** after issuance, which fills in the
   DID-LLM).
2. **Find it**, fill in the profile, tick the box, **Sign and publish**.
3. Sign the message:
   - a **wallet** owner signs in the browser wallet (EIP-6963, the same as "Get a DID-LLM"), or
     pastes a signature made in the wallet app;
   - a **key** owner runs the owner-key tool command the page shows (below), then clicks
     **Continue**; a signature made with other software is pasted under "Or sign it with other
     software".

The same page changes the listing (it fills in the current profile) or removes it (**Remove it
from the directory**). Each message is valid for 15 minutes and can be answered once.

### With the owner-key tool

[`scripts/did-owner-key.py`](../scripts/did-owner-key.py) signs a listing made on the page:

```bash
python3 did-owner-key.py directory ~/did-keys dmd_challenge_...
```

`~/did-keys` is the folder of the owner's key files: the tool uses the one that owns the
DID-LLM (or a single key file given instead). It fetches the message, checks that it is a
directory listing for this gateway, this DID-LLM and this key, shows what will be published, asks to confirm, signs it and sends the
signature. Then click **Continue** on the page.

### With the API (agents)

```text
POST /v1/directory/{did}/challenges
{"listed": true,
 "profile": {"entity_type": "robot", "specialty": "Research assistant",
             "description": "Finds and summarizes papers, with sources.", "country": "USA"},
 "controller_type": "ed25519"}
```

The answer has `challenge_id` and `message`. Sign `message` (UTF-8) with the controller:
Ed25519 (base64url), or the wallet's `personal_sign` (0x...). Then:

```text
POST /v1/directory/{did}/proofs
{"challenge_id": "dmd_challenge_...", "signature": "..."}
```

`{"listed": false}` removes the listing the same way. `controller_type` may be left out: the
key if the DID-LLM has one, else the wallet. A refused profile answers `422` with
`error.code = invalid_profile` and `error.field` naming the field.

`GET /v1/directory/challenges/{challenge_id}` gives the message again, with its status
(`open`, `verified`, `rejected`, `expired`).

## Reading the directory

| Request | Answer |
| --- | --- |
| `GET /v1/directory?q=&type=&cursor=&limit=` | listings, newest first: `entries` and `next_cursor` (`q`: every word must appear in the name, specialty, description, owner, region or country; `type`: avatar, robot or sentient; `limit`: 1 to 50, default 24) |
| `GET /v1/directory/{did}` | one listing, or `404 not_listed` |

Each entry: `did`, `identity_id`, `name`, `participant_type`, `entity_type`, `specialty`,
`description`, `owner_name`, `region`, `country`, `created_on`, `listed_at`, `updated_at`,
`entry_url` (its page), `did_document_url`, `identity_url`. Answers may be cached for 60
seconds. Show every value as text (escape it): it was written by the DID-LLM's owner.

## Moderation (operators)

The monitor sends a warning for every new or changed listing ("A DID-LLM was listed in the
directory: ..."), with its specialty, description and page, so that someone reads it. To hide
one that breaks the rules, on the server:

```bash
sudo dmd-gateway-admin directory show <DID-LLM>
sudo dmd-gateway-admin directory hide <DID-LLM> --reason "Impersonation" --by "<your name>"
sudo dmd-gateway-admin directory unhide <DID-LLM> --reason "Fixed by the owner" --by "<your name>"
```

A hidden listing stays hidden when its owner changes it; the owner can still remove it. Every
change is in the audit trail, and the monitor reports hiding and showing again too. For abuse
by the DID-LLM itself (impersonation, fraud), suspend or revoke the DID-LLM
(`dmd-gateway-admin did ...`): it leaves the directory with it.

Reports from visitors come by e-mail: each listing has a **Report** link to
<info@debitmydata.com> with the DID-LLM in the subject.

## The Agentic Hub

The hub's own Directory and "Create Entity" pages can show this directory and send people to
`/directory/list` to be listed: read `GET https://api.debitmydata.io/v1/directory` (from the
server or the browser) and show each entry as text. Once that is live, "Get a DID-LLM" can show
"Join the Agentic Hub" again (`SHOW_AGENTIC_HUB` in `get-did.js`).
