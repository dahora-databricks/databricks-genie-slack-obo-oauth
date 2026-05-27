# Genie Slack App — On-Behalf-Of (OBO) OAuth

> Integration between **Slack** and **Databricks AI/BI Genie** where every query runs **as the Slack user (OBO)** via OAuth — not under a shared service principal. Row-Level Security, Unity Catalog grants and audit logs follow the real user, end-to-end.

Two Databricks Apps talking to each other: an **OAuth broker** holding short-lived per-user tokens in memory, and a **Slack bot** that uses those tokens to call Genie. No persistence — tokens live in memory and expire in 1 hour.

---

## Table of contents

- [TL;DR](#tldr)
- [Prerequisites](#prerequisites)
- [Architecture](#architecture)
- [Flow](#flow-8-steps)
- [Setup in 4 stages](#setup-in-4-stages)
  - [Stage 1 — Databricks Apps · OAuth Broker](#stage-1--databricks-apps--oauth-broker)
  - [Stage 2 — Databricks Account · App Connection](#stage-2--databricks-account--app-connection)
  - [Stage 3 — Slack App](#stage-3--slack-app)
  - [Stage 4 — Databricks Apps · Genie + Slack](#stage-4--databricks-apps--genie--slack)
- [Testing the integration](#testing-the-integration)
- [Traceability, audit and RLS](#traceability-audit-and-rls)
- [Technical details](#technical-details)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

---

## TL;DR

| | |
|---|---|
| **Stack** | 2 Databricks Apps (Python) + Slack Socket Mode + Databricks AI/BI Genie |
| **Auth model** | Per-user OAuth 2.0 OBO (3-legged) + Client Credentials M2M between apps |
| **Token TTL** | 1h in-memory · no disk/DB persistence |
| **Key guarantee** | Every Genie call uses the Slack user's token → RLS, UC grants and logs reflect the real identity |

## Prerequisites

- Databricks workspace with **AI/BI Genie enabled** and at least one Genie Space configured with data/instructions
- **Account Admin** permission (needed to create an App Connection in the Account Console)
- A Slack workspace where you can create a new app
- (optional) Databricks CLI authenticated, if you prefer CLI-based deploys

---

## Architecture

![Solution architecture](img/architecture.png)

Two apps:

### `oauth-test-app` — OAuth Broker
- Implements the OAuth 2.0 Authorization Code flow against the Databricks Account
- Stores per-user tokens **in memory** (valid for 1h, swept every 10 min)
- Exposes REST endpoints: `/oauth/login`, `/oauth/callback`, `/oauth/token`, `/oauth/status`
- Receives the SSO redirect and exchanges the code for an access token

### `genie-slack-app` — Slack bot
- Connects to Slack via **Socket Mode** (no public webhook required)
- For each incoming message, asks the broker for that user's token
- Builds a per-user `WorkspaceClient` (never shared across threads)
- Calls Genie via the SDK using that token; relays the answer back to Slack

### App-to-app communication

The apps talk over HTTP authenticated via **OAuth Client Credentials (M2M)**:
- `genie-slack-app` receives `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` **automatically injected** by Databricks Apps (you don't configure these)
- It uses those credentials to obtain an M2M token and authenticate its calls to `oauth-test-app`

---

## Flow (8 steps)

#### 1️⃣ Socket connection to Slack
`genie-slack-app` opens a persistent Socket Mode connection to the Slack API — no public webhook.

#### 2️⃣ User interacts with the bot
The user sends a DM or mentions the bot (e.g. "Which product sold the most?").

#### 3️⃣ Message arrives over the socket
The Slack API routes the message through the socket to `genie-slack-app`.

#### 4️⃣ Auth check
The bot calls `oauth-test-app` (`/oauth/status?user_email=...`):
- **No valid token** → go to step 5
- **Valid token** → jump to step 7

#### 5️⃣ Auth request
The bot replies in Slack with an OAuth login link. The user clicks it and the Databricks SSO opens in the browser.

#### 6️⃣ Callback + token storage
After SSO, Databricks redirects to `oauth-test-app/oauth/callback`, which exchanges the `code` for an access token and stores it in memory (1h validity).

#### 7️⃣ Genie call
The bot fetches the token (`/oauth/token?user_email=...`), instantiates a `WorkspaceClient` for that user, and calls Genie via the SDK. Because the call uses the user's token:
- Genie Space permissions are enforced
- Row-Level Security on tables is applied
- Audit logs record the real user, not the SP

#### 8️⃣ Reply to the user
Genie returns the answer (filtered by the user's permissions) and the bot posts it back in Slack.

### Re-authentication
Tokens expire after **1 hour**. After expiry the user falls back into step 4 and repeats 5-6. While the token is valid, multiple questions reuse it with no further login.

---

## Setup in 4 stages

### Stage 1 — Databricks Apps · OAuth Broker

#### 1.1 Create the app
1. In Databricks: **Compute** → **Apps** → **Create new app** → **Create a custom app**
2. **App name:** `oauth-test-app`
3. **Next: Configure** → **Create app**

#### 1.2 Prepare `app.yaml`
**Before** deploying, configure `app.yaml`:

```bash
cd oauth-test-app
cp app.yaml.example app.yaml
```

Edit `oauth-test-app/app.yaml` and update **only** `DATABRICKS_HOST` to point at your workspace:

```yaml
env:
  - name: "DATABRICKS_HOST"
    value: "https://your-workspace.cloud.databricks.com"
```

Leave `CLIENT_ID`, `CLIENT_SECRET` and `OAUTH_REDIRECT_URI` empty — they get filled in after Stage 2.

> **Token cleanup/expiry:** `app.py` already cleans up tokens every 10 min and caps validity at 1h (`expires_in = min(expires_in, 3600)`).

#### 1.3 Deploy
1. In Databricks, open the `oauth-test-app` you just created
2. Click **Deploy**
3. Pick the folder with the code (`Workspace > Users > your-user > oauth-test-app`)
4. **Select** and wait for the deploy to finish

#### 1.4 Copy the app URL
After deploy you'll see a URL like:
```
https://oauth-test-app-XXXXXXXXX.aws.databricksapps.com
```

⚠️ Remember the callback is the URL **+ `/oauth/callback`**:
```
https://oauth-test-app-XXXXXXXXX.aws.databricksapps.com/oauth/callback
```

Save this URL — you'll use it in Stage 2.

---

### Stage 2 — Databricks Account · App Connection

#### 2.1 Create the App Connection
1. In the **Account Console** (accounts.cloud.databricks.com): **Settings** → **App connections** → **Add connection**

#### 2.2 Configure
| Field | Value |
|---|---|
| Application Name | `oauth-test-app` (or any descriptive name) |
| Redirect URLs | App URL **+ `/oauth/callback`** (from step 1.4) |
| Access scopes | **All APIs** (or just **SQL** if you know that's enough) |
| Generate a client secret | ✅ check |
| Access token TTL (minutes) | `60` |
| Refresh token TTL (minutes) | `10080` (7 days) |

Click **Save**.

#### 2.3 Copy Client ID and Client Secret
After saving you'll see:
- **Client ID** — a string like `0209ee5b-dc86-485b-93b...`
- **Client Secret** — generated once, **copy it now**

#### 2.4 Update `app.yaml` and re-deploy
Go back to `oauth-test-app/app.yaml` and fill in:

```yaml
env:
  - name: "DATABRICKS_HOST"
    value: "https://your-workspace.cloud.databricks.com"
  - name: "CLIENT_ID"
    value: "<client-id-from-step-2.3>"
  - name: "CLIENT_SECRET"
    value: "<client-secret-from-step-2.3>"
  - name: "OAUTH_REDIRECT_URI"
    value: "https://oauth-test-app-XXXXXXXXX.aws.databricksapps.com/oauth/callback"
```

**Re-deploy** `oauth-test-app` to apply the changes.

---

### Stage 3 — Slack App

#### 3.1 Create the Slack App
1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. App Name: `genie-demo` (or any name)
3. Pick your workspace

#### 3.2 Enable Socket Mode
1. Side menu: **Settings** → **Socket Mode** → enable **Enable Socket Mode**
2. Generate an **App-Level Token** (format `xapp-1-...`) → this is `SLACK_APP_TOKEN`

#### 3.3 Bot Token Scopes
Under **OAuth & Permissions** → **Bot Token Scopes**, add:

| Scope | Why |
|---|---|
| `app_mentions:read` | Detect @mentions |
| `channels:history` | Read public channels where the bot was added |
| `chat:write` | Send messages |
| `im:history` | Read DMs |
| `im:read` | See DM info |
| `im:write` | Send DMs |
| `users:read` | Resolve user info |
| `users:read.email` | Resolve the user's email (auth key) |

#### 3.4 Event Subscriptions
**Event Subscriptions** → enable **Enable Events** → under **Subscribe to bot events**:
- `app_mention` — when someone mentions the bot
- `message.im` — DMs to the bot

#### 3.5 Install in the workspace
**Install App** → **Install to Workspace** → authorize.

Copy the **Bot User OAuth Token** (`xoxb-...`) → this is `SLACK_BOT_TOKEN`.

---

### Stage 4 — Databricks Apps · Genie + Slack

#### 4.1 Get the Genie Space ID
In Databricks, open the Genie Space you want to expose. From the URL:
```
https://<workspace>.cloud.databricks.com/genie/spaces/01f0b7fd8c6c10038125494d92425ce4
                                                      ─────── Space ID ───────
```

This is your `DATABRICKS_SPACE_ID`.

#### 4.2 Create the app
**Compute** → **Apps** → **Create new app** → **Create a custom app** → name `genie-slack-app` → **Create app**.

#### 4.3 Configure `app.yaml`
```bash
cd genie-slack-app
cp app.yaml.example app.yaml
```

Edit `genie-slack-app/app.yaml` and fill in **everything**:
```yaml
env:
  - name: "SLACK_BOT_TOKEN"
    value: "xoxb-..."                      # step 3.5
  - name: "SLACK_APP_TOKEN"
    value: "xapp-..."                      # step 3.2
  - name: "DATABRICKS_SPACE_ID"
    value: "01f0b7fd8c6c10038125494d92425ce4"   # step 4.1
  - name: "DATABRICKS_HOST"
    value: "https://your-workspace.cloud.databricks.com"
  - name: "OAUTH_SERVICE_URL"
    value: "https://oauth-test-app-XXXXXXXXX.aws.databricksapps.com"  # URL from stage 1.4 (no /oauth/callback)
```

#### 4.4 Deploy
**Deploy** → pick the `genie-slack-app` folder in the workspace → **Select** → wait for it to finish.

#### 4.5 Copy the app's Service Principal
After the deploy, on the `genie-slack-app` **Overview** tab, copy the **App ID** / Service Principal (format `app-XXXXXX`).

#### 4.6 Grant Genie → OAuth permission
`genie-slack-app` needs to **call** `oauth-test-app`. In Databricks:

1. **Compute** → **Apps** → open **`oauth-test-app`**
2. **Permissions** tab
3. Add the `genie-slack-app` service principal (step 4.5) with permission **Can Use**
4. **Save**

---

## Testing the integration

### 1. Send the first message
Open Slack, find the bot in the apps list, send a DM like `Hi!`.

### 2. Authenticate
The bot replies with an OAuth link:
```
🔐 You need to authenticate first.

Click here to authenticate:
https://oauth-test-app-XXXXX.aws.databricksapps.com/oidc/oauth2/v2.0/authorize?...

(Authenticating as: your.email@company.com)
```

Click the link → Databricks SSO → **Allow**.

### 3. Confirm
You'll see a success page:
```
✅ Authentication successful!

User: your.email@databricks.com
Token stored, valid for 3600 seconds.

You can close this window and return to Slack.
```

### 4. Ask Genie
Go back to Slack and ask anything in natural language:

![Slack bot answering Genie questions](img/slack-bot.png)

The token is reused for **1 hour**. After that, the bot will ask you to authenticate again.

---

## Traceability, audit and RLS

The big win of this architecture: **every interaction uses the real user's credentials**, not the app's service principal.

### Genie Monitoring

In Databricks → **Genie** → **Monitoring**, the **User** column shows the actual Slack user (not the app SP):

![Genie Monitoring showing the real user](img/genie-monitoring.png)

### Concrete benefits

| Dimension | Behavior |
|---|---|
| **Traceability** | Each query is logged with the user who asked it. Audit shows the name, not a generic SP. |
| **Row Level Security** | RLS configured on tables is enforced automatically — each user sees only what they're allowed to. |
| **Unity Catalog grants** | If the user lacks `SELECT` on a table Genie uses, they won't see that data through Slack. |
| **Per-user control** | Revoking a user's access to the Genie Space removes their Slack access without affecting anyone else. |
| **Compliance** | Per-user audit trail makes LGPD/GDPR/SOX easier. |

### Worked example

Imagine a `vendas` table with regional RLS:
- **User A** (South) → sees only South sales
- **User B** (North) → sees only North sales

Both ask in Slack "what were the total sales?". The answers differ because Genie runs with each user's token and RLS is enforced at query time.

---

## Technical details

### Token lifecycle
- OAuth tokens expire after **1h** (3600s — enforced by the broker via `min(expires_in, 3600)`)
- The `WorkspaceClient` cache invalidates after **55 min**
- Expired tokens are removed from the in-memory dict every **10 min**

### App-to-app communication
- **M2M (Client Credentials):** `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` are auto-injected by Databricks Apps in **every** app — no manual config needed
- `genie-slack-app` uses these credentials to obtain an M2M token and authenticate its calls to `oauth-test-app`

### `oauth-test-app` REST API

| Endpoint | Method | Description |
|---|---|---|
| `/oauth/login?user_email=<email>` | GET | Returns the OAuth login URL for that user |
| `/oauth/callback?code=<code>&state=<state>` | GET | Databricks callback after SSO — exchanges code for token |
| `/oauth/token?user_email=<email>` | GET | Returns the current access token (or a login URL if not authenticated) |
| `/oauth/status?user_email=<email>` | GET | Auth status (authenticated/expired/never logged in) |

### Slack ↔ Databricks email mapping

If a user's Slack email differs from their Databricks email (e.g. personal vs. corporate), edit the mapping in `genie-slack-app/app.py`:

```python
def map_slack_email_to_databricks_email(slack_email: str) -> str:
    mapping = {
        "thvieira@outlook.com": "thiago.vieira@databricks.com",
        # add more as needed
    }
    return mapping.get(slack_email, slack_email)
```

Re-deploy `genie-slack-app` after editing.

---

## Troubleshooting

<details>
<summary><strong>"Error connecting to the authentication service"</strong></summary>

**Possible causes:**
- Wrong `OAUTH_SERVICE_URL` in `genie-slack-app/app.yaml`
- Cross-app permission not set up (step 4.6)
- `oauth-test-app` is not running

**Fixes:**
1. Confirm `OAUTH_SERVICE_URL` — it must be the broker URL **without** `/oauth/callback`
2. In `oauth-test-app` → **Permissions**, confirm the `genie-slack-app` SP has **Can Use**
3. In `oauth-test-app` → **Logs**, look for errors
</details>

<details>
<summary><strong>"Invalid token" or 403</strong></summary>

Token expired (1h TTL). Send any new message in Slack — the bot detects it and offers a fresh auth link.
</details>

<details>
<summary><strong>Genie replies "No data available"</strong></summary>

**Possible causes:**
- Wrong `DATABRICKS_SPACE_ID`
- User doesn't have access to the Space
- Space has no data/instructions configured

**Fixes:**
1. Re-check the Space ID in `app.yaml` (copy from the Genie URL)
2. In the Genie Space → **Permissions**, confirm the user has access
3. Test the same question directly in the Genie UI as that user to isolate
</details>

<details>
<summary><strong>"Parameter(s) present more than once: [state]"</strong></summary>

App Connection misconfigured. Confirm `OAUTH_REDIRECT_URI` is exactly `https://...databricksapps.com/oauth/callback` — no extra query params.
</details>

<details>
<summary><strong>Bot doesn't respond on Slack</strong></summary>

1. **Compute → Apps → genie-slack-app:** status must be **Running**
2. **Logs:** look for red errors
3. **Slack tokens:** confirm `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are correct; if you rotated tokens after deploy, re-deploy
4. **Socket Mode** enabled in the Slack App
</details>

---

## CLI-based deploy

```bash
databricks auth login --host https://your-workspace.cloud.databricks.com

cd oauth-test-app
databricks apps deploy oauth-test-app --source-code-path .

cd ../genie-slack-app
databricks apps deploy genie-slack-app --source-code-path .
```

You still need to perform every **configuration** step (App Connection, Slack, cross-app permissions) in the UI.

---

## Project layout

```
en-us/
├── README.md                     ← you are here
├── img/                          architecture diagram + screenshots
│   ├── architecture.png
│   ├── genie-monitoring.png
│   └── slack-bot.png
├── oauth-test-app/               OAuth broker (app 1)
│   ├── app.py
│   ├── app.yaml                  gitignored (contains secrets)
│   ├── app.yaml.example
│   └── requirements.txt
└── genie-slack-app/              Slack bot (app 2)
    ├── app.py
    ├── app.yaml                  gitignored
    ├── app.yaml.example
    └── requirements.txt
```
