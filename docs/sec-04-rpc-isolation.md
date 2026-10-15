# SEC-04 — Verify DMD RPC isolation (operator checklist)

**Status:** verification pending for each relevant DMD node. Do not infer compliance from the existence of configuration files. Avoid pasting `rpcauth`, passwords, cookies, wallet paths or keys into reports.

1. On each host, identify the DMD Core daemon's active configuration, listening address and firewall policy. Confirm `rpcbind` is loopback or a formally approved private interface; avoid publicly routed IP addresses and broad `rpcallowip` ranges.
2. Linux examples (read-only): `sudo ss -lntp` and `sudo ufw status verbose` (adapt to the actual daemon and firewall). Determine the active RPC port from the running DMD Core configuration; do not assume a particular port number. A listener on `0.0.0.0:<RPC_PORT>` or `[::]:<RPC_PORT>` warrants investigation; do not assume the firewall makes it acceptable.
3. Verify from a separate **authorized** external host that the RPC port cannot be reached. Never run an unsolicited scan of third-party systems. Prefer existing firewall/security-group logs and an approved test.
4. Record only: host role/alias, DMD Core version, RPC bind/interface (redacted if necessary), firewall/security-group rule status, external reachability result, UTC test date/time, operator and follow-up action.
5. Do not modify production networking until a maintenance/rollback plan is approved. Follow up with SEC-05..SEC-10 (dedicated credentials, RPC allowlist, wallet segregation and external isolation tests).

**Risk:** a production web API must never forward arbitrary user/agent RPC to the DMD node. A public API can accept narrow business requests, but DMD RPC must remain an internal implementation detail.
