# Genie + Slack with per-user OAuth (OBO)

> **Slack ↔ Databricks AI/BI Genie** integration where every Genie query runs **on behalf of the Slack user (OBO)** via OAuth, never under a shared service principal. Row-Level Security, Unity Catalog grants and audit trails follow the real user, end to end.

Two Databricks Apps speaking to each other: an **OAuth broker** that holds short-lived per-user tokens, and a **Slack bot** that uses those tokens to talk to Genie. No persistence: tokens live in memory and expire in 1 hour.

---

## Languages / Idiomas

| | |
|---|---|
| 🇬🇧 | **[English · en-us/](en-us/README.md)**: full setup guide |
| 🇧🇷 | **[Português · pt-br/](pt-br/README.md)**: versão canônica |

The two folders share the same source tree (two app codebases and the images). The code itself isn't translated, only the README and inline docs are.

Os dois diretórios compartilham a mesma árvore (dois apps e as imagens). O código não é traduzido, só o README e a documentação inline.

---

## TL;DR

- **Two Databricks Apps:**
  - `oauth-test-app`: OAuth 2.0 broker, stores per-user tokens in memory (1h TTL), exposes `/oauth/{login,callback,token,status}` REST endpoints
  - `genie-slack-app`: Slack Socket Mode bot, fetches the user's token from the broker, calls Genie with a per-user `WorkspaceClient`
- **Auth model:** the Slack user authenticates once via Databricks SSO; the bot uses **that user's token** for every Genie call. Apps talk to each other via OAuth Client Credentials (M2M).
- **Setup:** 4 steps. (1) deploy oauth broker, (2) create Account-level OAuth App Connection, (3) create Slack app + scopes, (4) deploy Slack bot and grant cross-app permissions.
- **Why this matters:** RLS, UC grants and the Genie audit logs all line up with the actual Slack user, instead of a single bot identity speaking for everyone.

To get started: see **[en-us/README.md](en-us/README.md)** or **[pt-br/README.md](pt-br/README.md)**.
