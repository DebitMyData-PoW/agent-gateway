# Security Policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub issues, pull
requests or discussions.

Report them privately in one of these ways:

- **GitHub:** use the **Report a vulnerability** button in this repository's **Security**
  tab.
- **Email:** info@debitmydata.com, with the subject line
  `Security: DebitMyData Agent Gateway`.

Please include:

- a description of the issue and its possible impact;
- the steps, requests or code needed to reproduce it;
- the affected file, endpoint or commit, if known;
- how you would like to be credited, if at all.

We will acknowledge your report, keep you informed while we investigate, and coordinate a
disclosure date with you once a fix is available. Please give us a reasonable amount of time
to fix the issue before sharing it publicly.

## Scope

In scope: the code and configuration in this repository.

Out of scope: third-party services and wallets, social-engineering attacks, denial-of-service
testing, and any testing against DebitMyData production systems, nodes or other users'
data. Do not access, modify or delete data that does not belong to you.

## Supported versions

The Agent Gateway is in active development and has not yet been released for production
use. Security fixes are made on the `main` branch.

## Never share secrets

If you find a private key, password, token or other secret, report its location only. Do not
use it, and do not include the secret itself in your report.
