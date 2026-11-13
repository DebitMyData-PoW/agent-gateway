"""OAuth 2.1 for the Agentic Superhighway: the DID-LLM is the login (phase 1, agents).

An AI agent that has a DID-LLM gets an access token from the authorization server
(``auth.debitmydata.io``, the ``auth_host`` setting) without any account or password: it
signs a short JWT with its DID-LLM's own controller key and sends it to the token endpoint
(the client-credentials grant with ``private_key_jwt`` client authentication, RFC 7523; MCP's
Client Credentials extension). The DID-LLM is the client: its DID document is its
registration, so there is nothing to register first.

Getting a DID-LLM stays open, as before: tokens are only for what an agent does *as* an
existing DID-LLM.

    keys.py       the token-signing key (RS256) and its public JWK
    assertion.py  checks the agent's signed JWT (EdDSA, its DID-LLM's key, fresh, for us)
    tokens.py     issues the access tokens (JWT, RFC 9068) and checks them
"""
