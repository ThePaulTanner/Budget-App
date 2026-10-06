# HomeFinance server

A private budgeting API for one person. It pulls balances, transactions and investment holdings from your banks through [Plaid](https://plaid.com), keeps them in an encrypted database on your own NAS, and serves them to the HomeFinance iPhone app. The API contract is in [`../CLAUDE.md`](../CLAUDE.md) and the sample responses in [`../api-fixtures/`](../api-fixtures).

**What it is:** FastAPI + SQLCipher (encrypted SQLite) in one Docker container, built for an Intel Synology (DS423+). **What it is not:** multi-user, or reachable from the internet. Only one tiny, signature-checked webhook port is public.

## How it is protected

| Layer | What it does |
|---|---|
| Network | The API is only reachable through your Tailscale network (WireGuard + HTTPS). Only the Plaid webhook port is public, and it accepts nothing but correctly signed Plaid messages. |
| Login | Password + authenticator code (or a one-time recovery code). Passwords are Argon2id-hashed with a secret pepper. Short-lived access tokens, per-device refresh tokens that change on every use, a device list you can revoke from, and lockouts after failed attempts. |
| Database | The whole database file is encrypted with SQLCipher (AES-256). |
| Bank tokens | Plaid access tokens get a second layer: AES-256-GCM with a separate key per token, tied to their row. |
| Master key | One 256-bit key, kept in a file outside the database. Every other key is derived from it. |
| Bank access | Read-only. The server only asks Plaid for transactions and investments. It never requests account numbers or the ability to move money. |
| Container | Runs as a normal user, read-only filesystem, no extra privileges, no request logging. |

**Other honest limits:**
- Behind Tailscale every request reaches the container from the same address, so the failed-sign-in limiter cannot tell devices apart. The per-user lockout (5 failures, doubling up to 15 minutes) and a shared 20-failure lockout mean a device on your tailnet could lock you out of signing in for a while by guessing wrongly on purpose. Nobody can get in that way, and lockouts expire. Recovery codes do not bypass a lockout.
- The public webhook port is rate limited (120 requests a minute) before signatures are checked, so a flood of junk could delay real webhooks. The hourly backup sync still collects the data.

**Honest limit:** encryption protects you if the NAS disk, a backup or the database file is stolen. It cannot protect a NAS that an attacker already controls while it is running, because the key is in memory then. Keep DSM and Tailscale updated.

## What you need

- A Synology NAS with **Container Manager** (DSM 7). Tested target: DS423+ (Intel, so the `amd64` image).
- **Tailscale** installed on the NAS and on your iPhone, with **MagicDNS** and **HTTPS Certificates** turned on in the Tailscale admin console (DNS page).
- A **Plaid** account ([dashboard.plaid.com](https://dashboard.plaid.com)): your `client_id` and the `sandbox` secret to start. Whether Plaid supports your bank, and when production access is granted, is Plaid's decision, so check their coverage for Desjardins before relying on it.

## Setup

### 1. Folders and secrets (over SSH on the NAS)

```sh
mkdir -p /volume1/docker/homefinance/{data,secrets} && cd /volume1/docker/homefinance
openssl rand -hex 32 > secrets/hf_master_key        # the master key: 64 hex characters
printf '%s' 'YOUR_PLAID_CLIENT_ID' > secrets/plaid_client_id
printf '%s' 'YOUR_PLAID_SANDBOX_SECRET' > secrets/plaid_secret
chmod 600 secrets/*
```

Copy `docker-compose.yml` and `.env.example` (rename it to `.env`) into that folder. Set `PUID` and `PGID` in `.env` to the numbers shown by `id YOUR_ADMIN_USER`, then make the folders theirs: `chown -R PUID:PGID data secrets`.

> **Back up the master key somewhere safe and separate from the NAS** (your password manager). Without it the database cannot be opened, by you or anyone. With it next to the backups, the encryption would protect nothing.

### 2. Start it

In **Container Manager → Project → Create**, point it at `/volume1/docker/homefinance` and use the existing `docker-compose.yml`. The image comes from GitHub: `ghcr.io/thepaultanner/budget-app-server:latest`, built by the **Publish server image** workflow. New packages on GitHub are private, so either:

- make the package public (Package settings → Change visibility; the image holds no secrets), or
- run once on the NAS: `docker login ghcr.io -u YOUR_GITHUB_NAME` with a personal access token that has `read:packages`.

Check it is healthy: `curl http://127.0.0.1:8080/health` should print `{"ok":true,"plaid_env":"sandbox"}`.

### 3. Create your login

```sh
docker exec -it homefinance python -m app.cli create-user --username paul
```

Choose a password of at least 12 characters. It prints a QR code to scan with an authenticator app (Apple Passwords, 1Password, Authy, ...) and **10 recovery codes**. Store the codes in your password manager. They are shown once and each works once. The server accepts exactly one user.

### 4. Put it on your tailnet, and open the one public door

```sh
# The API, only for devices on your tailnet:
tailscale serve --bg --https=443 http://127.0.0.1:8080

# Plaid's webhooks, the only thing on the internet (Funnel allows ports 443, 8443, 10000):
tailscale funnel --bg --https=8443 http://127.0.0.1:8081
```

On DSM the `tailscale` command lives inside the Tailscale package, so you may need its full path or `sudo`. If a flag differs on your version, `tailscale serve --help` and `tailscale funnel --help` are authoritative. Funnel also has to be allowed for the NAS in your tailnet policy; the command tells you if it is not. See Tailscale's docs for [Serve](https://tailscale.com/docs/reference/tailscale-cli/serve) and [Funnel](https://tailscale.com/docs/reference/tailscale-cli/funnel).

Then set `PUBLIC_WEBHOOK_URL=https://YOUR-NAS.YOUR-TAILNET.ts.net:8443/plaid/webhook` in `.env` and restart the project. Without it everything still works, but new transactions arrive on the hourly backup timer instead of within minutes.

Port 8081 is safe to expose because it serves exactly one route. Every other path returns 404, and a request without a valid Plaid signature is refused.

### 5. Link your banks

Open `https://YOUR-NAS.YOUR-TAILNET.ts.net` on a device on your tailnet, sign in, and press **Link a bank**. In the sandbox, Plaid's test login is `user_good` / `pass_good`. Linked banks are listed there with a **Reconnect** button when one needs attention.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PLAID_ENV` | `sandbox` | `sandbox` (fake data) or `production` (real banks) |
| `PLAID_COUNTRY_CODES` | `CA,US` | Countries offered in the bank picker |
| `BASE_CURRENCY` | `CAD` | The currency budgets and spending are counted in |
| `PUBLIC_WEBHOOK_URL` | empty | Where Plaid sends "new data" messages |
| `SYNC_INTERVAL_MINUTES` | `60` | Backup sync timer (webhooks do the real work) |
| `TZ` | `America/Toronto` | Decides what "today" and "this month" mean |
| `HF_MASTER_KEY_FILE` | `/run/secrets/hf_master_key` | Path to the master key |
| `PLAID_CLIENT_ID_FILE`, `PLAID_SECRET_FILE` | in `/run/secrets` | Plaid credentials as files |

Any secret can also be given directly as `NAME` instead of `NAME_FILE`, but files are safer.

## Everyday use

- **Sign in from the app/web:** password + a fresh authenticator code. Devices are listed at `GET /auth/sessions` and can be revoked.
- **Update:** pull the new image and recreate the project (Container Manager → Project → Build/Update).
- **Backups:** back up the `data` folder (it is encrypted; safe to copy) **and, separately,** the master key.
- **Lost the authenticator:** sign in with a recovery code.
- **Go to production:** swap the Plaid secret file for the production one, set `PLAID_ENV=production`, restart, and link your real banks. Sandbox data stays in the database until you remove those banks (`DELETE /api/items/{id}`).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container exits: "Cannot start: ... master key" | The key file is missing, unreadable by `PUID`, or not exactly 64 hex characters |
| "Could not read the database. The master key is probably wrong." | A different master key than the one that created the database |
| Link page shows an error after signing in | Plaid credentials or `PLAID_ENV` do not match (sandbox secret with production, or the reverse) |
| New transactions are slow to appear | `PUBLIC_WEBHOOK_URL` unset or Funnel not running; the hourly timer still catches up |
| `429 Too many attempts` | Five failed sign-ins; wait for the lockout (it doubles each time, up to 15 minutes) |

## Developing

```sh
cd server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

The tests use a real SQLCipher database and check, among other things, that the API reproduces every file in `api-fixtures/`, that the database file contains no readable data, and that forged or stale webhooks are refused. GitHub Actions runs them and smoke-tests the built image on every change.

## Not verified yet

Honest list, so nothing surprises you:

- The Docker image is built and smoke-tested by GitHub Actions, but it has not yet been run on a real Synology.
- The bank-linking page has not been tried against live Plaid. Everything around it (token exchange, sealing, sync) is tested against a stand-in for Plaid's replies.
- Plaid's behaviour with Desjardins, and your Plaid production approval, are outside this code.
- The Tailscale commands come from Tailscale's documentation and have not been run on your NAS.
