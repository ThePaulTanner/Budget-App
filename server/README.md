# HomeFinance server

A private budgeting API for one person. It pulls balances, transactions and investment holdings from your banks through [Plaid](https://plaid.com), keeps them in an encrypted database on your own NAS, and serves them to the HomeFinance iPhone app. The API contract is in [`../CLAUDE.md`](../CLAUDE.md) and the sample responses in [`../api-fixtures/`](../api-fixtures).

**What it is:** FastAPI + SQLCipher (encrypted SQLite) in one Docker container, built for an Intel Synology (DS423+). **What it is not:** multi-user, or reachable from the internet. Only one tiny, signature-checked webhook port is public.

## How it is protected

| Layer | What it does |
|---|---|
| Network | The API is only reachable through your Tailscale network (WireGuard + HTTPS). Only the Plaid webhook port is public, and it accepts nothing but correctly signed Plaid messages. |
| Login | Password + authenticator code (or a one-time recovery code). Passwords are Argon2id-hashed with a secret pepper. Short-lived access tokens, per-device refresh tokens that change on every use, a device list you can revoke from, a log of every sign-in attempt, and a hard lock after 10 failed attempts in a row. |
| Database | The whole database file is encrypted with SQLCipher (AES-256). |
| Bank tokens | Plaid access tokens get a second layer: AES-256-GCM with a separate key per token, tied to their row. |
| Master key | One 256-bit key, kept in a file outside the database. Every other key is derived from it. |
| Bank access | Read-only. The server only asks Plaid for transactions and investments. It never requests account numbers or the ability to move money. |
| Container | Runs as a normal user, read-only filesystem, no extra privileges, no request logging. |

**Other honest limits:**
- Behind Tailscale every request reaches the container from the same address, so the temporary-delay limiter cannot tell devices apart, and the hard lock (10 failures in a row, then no new sign-ins until `python -m app.cli unlock` on the NAS) means a device on your tailnet could lock you out of new sign-ins by guessing wrongly on purpose. Nobody can get in that way, devices already signed in keep working, and you unlock from the NAS. Recovery codes do not bypass a lock.
- The public webhook port is rate limited (120 requests a minute) before signatures are checked, so a flood of junk could delay real webhooks. The hourly backup sync still collects the data.

**Honest limit:** encryption protects you if the NAS disk, a backup or the database file is stolen. It cannot protect a NAS that an attacker already controls while it is running, because the key is in memory then. Keep DSM and Tailscale updated.

## What you need

- A Synology NAS with **Container Manager** (DSM 7). Tested target: DS423+ (Intel, so the `amd64` image).
- **Tailscale** installed on the NAS and on your iPhone, with **MagicDNS** and **HTTPS Certificates** turned on in the Tailscale admin console (DNS page).
- A **Plaid** account ([dashboard.plaid.com](https://dashboard.plaid.com)): your `client_id` and the `sandbox` secret to start. Whether Plaid supports your bank, and when production access is granted, is Plaid's decision, so check their coverage for Desjardins before relying on it.

## Setup

### 1. Folders and secrets (over SSH on the NAS)

Turn on SSH temporarily (Control Panel → Terminal & SNMP) and **turn it off again when setup is done**. In the commands below, replace `volume1` with your volume (`ls /` shows it; the `docker` shared folder is created by Container Manager on whichever volume you chose). Use absolute paths, because a relative `secrets/...` path run from the wrong folder fails.

```sh
sudo mkdir -p /volume1/docker/homefinance/data /volume1/docker/homefinance/secrets
cd /volume1/docker/homefinance
sudo sh -c 'umask 077; openssl rand -hex 32 > /volume1/docker/homefinance/secrets/hf_master_key'   # 64 hex characters
```

Enter the Plaid credentials without echoing them to the screen (paste one command at a time):

```sh
read -rs -p "Plaid client_id: " V; echo; printf '%s' "$V" | sudo tee /volume1/docker/homefinance/secrets/plaid_client_id >/dev/null; unset V
read -rs -p "Plaid sandbox secret: " V; echo; printf '%s' "$V" | sudo tee /volume1/docker/homefinance/secrets/plaid_secret >/dev/null; unset V
```

Copy `docker-compose.yml` and `.env.example` (rename it to `.env`) into that folder. Set `PUID` and `PGID` in `.env` to the numbers shown by `id YOUR_ADMIN_USER`, then give the container user those folders:

```sh
sudo chown -R PUID:PGID /volume1/docker/homefinance/data /volume1/docker/homefinance/secrets
sudo chmod 700 /volume1/docker/homefinance/data /volume1/docker/homefinance/secrets
sudo chmod 600 /volume1/docker/homefinance/secrets/*
```

**Synology permission trap:** DSM's own access list (ACL) can override these Unix permissions and leave the folder read-only for the container, which shows up as `unable to open database file` in the logs. If it does, clear the ACL on the data folder and set the mode again: `sudo synoacltool -del /volume1/docker/homefinance/data && sudo chmod 700 /volume1/docker/homefinance/data`.

> **Back up the master key somewhere safe and separate from the NAS** (your password manager). Without it the database cannot be opened, by you or anyone. With it next to the backups, the encryption would protect nothing.

### 2. Start it

In **Container Manager → Project → Create**, point it at `/volume1/docker/homefinance` and use the existing `docker-compose.yml`. Or over SSH: `sudo docker compose up -d`. The image comes from GitHub: `ghcr.io/thepaultanner/budget-app-server:latest`, built by the **Publish server image** workflow. New packages on GitHub are private, so either:

- make the package public (Package settings → Change visibility; the image holds no secrets), or
- run once on the NAS: `docker login ghcr.io -u YOUR_GITHUB_NAME` with a personal access token that has `read:packages`.

The API is published on the NAS at `127.0.0.1:18080` (not 8080, which other DSM containers commonly use). Check it is healthy: `curl http://127.0.0.1:18080/health` should print `{"ok":true,"plaid_env":"sandbox"}`.

**Changing `.env` later:** `docker compose restart` does not re-read it. Use `sudo docker compose up -d --force-recreate`.

### 3. Create your login

```sh
docker exec -it homefinance python -m app.cli create-user --username paul
```

Choose a password of at least 12 characters. It prints a QR code to scan with an authenticator app (Apple Passwords, 1Password, Authy, ...) and **10 recovery codes**. Store the codes in your password manager. They are shown once and each works once. The server accepts exactly one user.

### 4. Put it on your tailnet, and open the one public door

```sh
# The API, only for devices on your tailnet:
tailscale serve --bg --https=443 http://127.0.0.1:18080

# Plaid's webhooks, the only thing on the internet (Funnel allows ports 443, 8443, 10000):
tailscale funnel --bg --https=8443 http://127.0.0.1:8081
```

On DSM the `tailscale` command lives inside the Tailscale package, so you may need its full path or `sudo`. If a flag differs on your version, `tailscale serve --help` and `tailscale funnel --help` are authoritative. Funnel also has to be allowed for the NAS in your tailnet policy; the command tells you if it is not. See Tailscale's docs for [Serve](https://tailscale.com/docs/reference/tailscale-cli/serve) and [Funnel](https://tailscale.com/docs/reference/tailscale-cli/funnel).

Then set `PUBLIC_WEBHOOK_URL=https://YOUR-NAS.YOUR-TAILNET.ts.net:8443/plaid/webhook` in `.env` and recreate the project (`sudo docker compose up -d --force-recreate`). Without it everything still works, but new transactions arrive on the hourly backup timer instead of within minutes.

Port 8081 is safe to expose because it serves exactly one route. Every other path returns 404, and a request without a valid Plaid signature is refused.

### 5. Link your banks

Open `https://YOUR-NAS.YOUR-TAILNET.ts.net/app` on a device on your tailnet, sign in, go to **Settings**, and press **Link a bank**. Plaid's window opens; choose the bank and sign in there (your bank password goes to Plaid, never to this server). Repeat for each bank. A bank that later needs attention shows **Reconnect** next to it, and **Remove** deletes one. In the sandbox, Plaid's test login is `user_good` / `pass_good`, and if it asks for a phone number, no text message is sent: enter the code `123456`. The sandbox TD Canada Trust test bank has no investment support, so Investments stays empty until you link a real brokerage. The older stand-alone page at `https://YOUR-NAS.YOUR-TAILNET.ts.net/` does the same job and signs in with the same cookie.

### 6. Use the web app

Open `https://YOUR-NAS.YOUR-TAILNET.ts.net/app` on a device on your tailnet and sign in with your password and authenticator code. It has five sections in the side panel: Overview (net worth, chart, top categories, and the credit card payments with their due dates), Accounts (every account in Investments, Cash, Credit and Loans tables with each bank's logo, balances, limits and owners), Spending (tabs for Transactions with search, filters, re-categorize and "always categorize" rules, Merchants, and Budgets), Money Management and Investments. The cog at the top right opens a small menu with **Settings** and **Sign out**. Settings has six tabs: Banks (link, reconnect, remove and sync now at the top right, and under each bank its accounts with who owns each and a Hide box that leaves it out of totals), People, Rules, Appearance (colour scheme, accent, density), Devices and Sign-in log; the circular arrow beside the name reloads the page's data and does not sync banks.

**Layout and appearance.** On a computer there is a sidebar with the sections and a reload button beside the HomeFinance name, a top bar with the page title and the cog menu (Settings, Sign out), and a footer showing when your banks last synced. On a phone the sidebar becomes a bar of tabs along the bottom, like an app. In **Settings, Appearance** you can choose a colour scheme (System, Light or Dark), one of six accent colours, and Comfortable or Compact spacing. Those choices are saved in that browser only (a few harmless words in its local storage, nothing from your accounts), so a phone and a computer can look different. While the server is connected to Plaid's sandbox, a small "Sandbox" badge in the sidebar reminds you that the data is fake; with real banks there is no badge.

**Bank logos.** When a bank is linked, the server asks Plaid for that bank's logo and brand colour and keeps them in the encrypted database (Plaid sends the logo as a small PNG, checked before use). Banks linked earlier get theirs at their next sync. A bank Plaid has no logo for shows its initials on a coloured badge instead. Nothing is fetched from anywhere else, and nothing about you leaves the NAS to get a logo.

**People and shared accounts.** On Settings, People, add everyone whose accounts are linked here (you, your wife). On the Accounts tab, tick who owns each account; tick more than one for a shared account, or use the shortcuts at the top of each bank (All Paul, All Sam, Shared by everyone). Once someone is added, a menu at the top of the page switches between **Household** and each person. The household counts every account **once**, however many owners it has, so adding owners never changes household totals. A person's view shows the accounts they own **in full**, so a shared account appears in both people's views and their totals add up to more than the household's (the page says so). A person's view covers net worth and its chart, accounts, transactions, spending, investments and credit card payments; budgets and merchants always cover the whole household. A person's net-worth chart starts on the day they were added, because earlier balances were only saved as one household figure. Changing an account's owners later does not rewrite earlier points either: a person's chart only reflects the owners at each sync. An account that nobody owns is in the household but in no one's own view. People are labels for organising accounts, **not logins and not access control**: anyone who signs in sees everything. New accounts that appear later start with no owner. Removing a person keeps their accounts and deletes only their own saved net-worth history.

**Sub-categories.** Plaid files most spending under a detailed category too, for example Food & Drink › Fast Food or Food & Drink › Groceries. Spending by category lists those under their parent, and each purchase counts in both the parent and its sub-category, but the month's total and the "budgeted" total count it once. You can budget a sub-category (it still counts toward the parent's budget, and the row says so), filter Transactions by one, or move a transaction into one with the category picker (sub-categories seen in your data are listed with the "›"). Transfers, loan and card payments and income stay out of spending, sub-categories included. A category is a sub-category when its name starts with a top-level name and an underscore (`FOOD_AND_DRINK_`, `TRAVEL_`, `INCOME_`…), so a custom category you type as "Travel fund" files under Travel, and one starting with `INCOME_`, `TRANSFER_IN_`, `TRANSFER_OUT_` or `LOAN_PAYMENTS_` is treated as not spending.

**Money Management.** Lists each credit card with the amount to pay (the last statement balance), the minimum payment, the due date and how many days away it is, plus the balance right now, available credit and the last payment. Overdue cards come first, then the soonest due. A card shows **Statement paid** once its most recent payment, made since the statement closed, covers the statement balance (two smaller payments are not added together, so such a card still shows as due until the bank's next update). A due date that has passed with no bank update since shows **Unconfirmed** rather than Overdue, because the bank may have been paid in between. The amounts come from Plaid's Liabilities product, which the link page asks for as optional, so a bank that does not offer it still links normally; the page then shows balances only and says so. Banks linked before this feature existed were not asked for it, so remove and link them again if you want due dates. It is information only: the server cannot move money.

**Merchants.** The Merchants page has two tabs, **Merchants** and **Needs a name**. The Merchants tab keeps a master list of where your money goes and tracks each merchant over time (spending by month, average charge, first and last seen, categories). Plaid's cleaned merchant names fill the list by themselves. When your bank gives only unclear text (for example `POS PURCHASE 4521 TORONTO ON`), it appears on the **Needs a name** tab (the tab shows how many are waiting): press **Name it**, choose how to match (exactly, or contains), see a live preview of the transactions it would catch, and give it a readable name. Every matching transaction, past and future, is then tracked under that name, and the name is shown on the Transactions screen. Pick an existing name to combine two (or use **Merge into**). Renaming a merchant never breaks the link. Your names beat the bank's, and totals are in your base currency, so a USD charge is counted separately. Payments, transfers and income are hidden from the list by default because they are not spending.

Signing in sets a cookie that keeps you signed in for 30 days from last use (see `docs/SECURITY.md`, section 1b). Sign out from Settings to end it, and use only computers you trust. Banks are linked from there too (Settings, Link a bank). There is no separate setup: it ships inside the same container.

### 7. Live prices for your positions

The Investments screen values each position at a near-real-time price and shows the change since yesterday's close, since today's open, over 1 week, 1 month, 3 months, year to date and 1 year. Plaid only knows end-of-day prices, so live prices come from **Yahoo Finance**:

- Only **ticker symbols** leave your NAS (for example `VEQT.TO`), to `query1.finance.yahoo.com`. Never balances, quantities, names or account details. Turn it off completely with `LIVE_QUOTES=false`.
- It refreshes every minute while the page is open (the server caches for a minute, and for ten minutes when the market is closed). Yahoo may delay some prices by up to about 15 minutes.
- It uses Yahoo's **unofficial** public endpoint, which can change, throttle or stop without notice. When that happens the page says so and falls back to your bank's last prices; nothing else breaks. After an error the server leaves Yahoo alone for five minutes.
- Canadian listings are found through the exchange code Plaid supplies (TSX becomes `.TO`, NEO `.NE`, and so on); until the first sync after the upgrade it tries the Canadian exchanges in turn. Cash, mutual funds Yahoo does not carry, and anything not found keep your bank's price, marked "bank price".
- Changes assume you held today's quantity for the whole period.
- **Not yet verified against the live service**: the build environment could not reach Yahoo. The parser is tested against sample responses written from Yahoo's documented chart format. After you link a real brokerage, check that the Investments screen shows "Live" beside your positions; if it shows a message instead, send it to me.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PLAID_ENV` | `sandbox` | `sandbox` (fake data) or `production` (real banks) |
| `PLAID_COUNTRY_CODES` | `CA,US` | Countries offered in the bank picker |
| `BASE_CURRENCY` | `CAD` | The currency budgets and spending are counted in |
| `PUBLIC_WEBHOOK_URL` | empty | Where Plaid sends "new data" messages |
| `SYNC_INTERVAL_MINUTES` | `60` | Backup sync timer (webhooks do the real work) |
| `LIVE_QUOTES` | `true` | Live prices for investment positions from Yahoo Finance (only ticker symbols are sent). `false` turns it off |
| `MAX_FAILED_LOGINS` | `10` | Failed sign-ins in a row before new sign-ins are locked (3 to 100). It is only read if your `docker-compose.yml` passes it through, as the repo copy does |
| `TZ` | `America/Toronto` | Decides what "today" and "this month" mean |
| `HF_MASTER_KEY_FILE` | `/run/secrets/hf_master_key` | Path to the master key |
| `PLAID_CLIENT_ID_FILE`, `PLAID_SECRET_FILE` | in `/run/secrets` | Plaid credentials as files |

Any secret can also be given directly as `NAME` instead of `NAME_FILE`, but files are safer.

## Everyday use

- **Sign in from the app/web:** password + a fresh authenticator code. Devices are listed at `GET /auth/sessions` and can be revoked.
- **Update:** pull the new image and recreate the project (Container Manager → Project → Build/Update).
- **Backups:** back up the `data` folder (it is encrypted; safe to copy) **and, separately,** the master key.
- **Lost the authenticator:** sign in with a recovery code.
- **See who tried to sign in:** web app → Settings → Sign-in activity, or `docker exec -it homefinance python -m app.cli login-log`.
- **Locked out after too many failed attempts:** `docker exec -it homefinance python -m app.cli unlock`. It takes effect at once, no restart.
- **Go to real banks:** follow "Going to real banks" below. The order matters (remove the sandbox bank first).

## Going to real banks

**Which Plaid plan.** As Plaid's support pages describe them in October 2026 (plans change, so confirm in your dashboard):

- **Trial plan (free).** For a new US or Canada developer account that has not applied for Production. It uses **real banks** (US and Canadian), is capped at **10 linked banks** ("Items"), and includes Transactions and Investments. You finish a short Trial plan application in the dashboard at dashboard.plaid.com; Plaid decides.
- **Pay as you go.** For more than 10 banks or other products. Apply for Production access in the dashboard; the prices are shown on the last page of the application.
- **Every real bank you link uses one of the 10 slots, and removing a bank does not give the slot back.** Sandbox banks are unlimited and do not count. So: link each real bank once, use **Reconnect** (not remove and re-add) when a bank needs attention, and do your experiments in the sandbox.

**Checklist, in this order**

1. **Check coverage first.** Look up each of your banks and brokerages in Plaid's institution list for Canada, and see which products they support (Transactions; Investments for brokerages; Liabilities for credit card due dates). Coverage is Plaid's and the bank's decision, not this server's.
2. **Get access.** Complete the Trial plan (or Production) application, wait for Plaid to approve it, then copy your **Production secret** from the dashboard (Developers, Keys). Your client id normally stays the same; confirm that in the dashboard.
3. **Remove the sandbox bank, while still in sandbox mode.** Web app, Settings, Banks, **Remove**. (Doing this after switching would ask the production Plaid to close a sandbox connection and fail.)
4. **Clear the fake chart history:** `docker exec -it homefinance python -m app.cli reset-networth`. Otherwise the net-worth chart keeps the sandbox's invented balances.
5. **Swap the secret** (SSH on, no echo, one command at a time; use your own volume in the path):
   ```sh
   read -rs -p "Plaid PRODUCTION secret: " V; echo; printf '%s' "$V" | sudo tee /volume1/docker/homefinance/secrets/plaid_secret >/dev/null; unset V
   ```
6. **Switch the environment.** In `.env` set `PLAID_ENV=production`, then `sudo docker compose up -d --force-recreate`. Check `curl http://127.0.0.1:18080/health` prints `"plaid_env":"production"`. A secret and environment that do not match show as `INVALID_API_KEYS` when you link a bank.
7. **Link each bank once** from the web app: Settings, **Link a bank** (it reminds you about the slot limit before it opens). Some banks send you to their own site or app to approve; if one is not offered or fails, stop and note the exact message before retrying, because each successful link uses a slot.
8. **Wait for the first sync.** Years of history can take a few minutes. Webhooks (`PUBLIC_WEBHOOK_URL`) then bring new transactions within minutes.
9. **Check it against your bank's own app:** balances on Overview, a few recent transactions, and for a brokerage the positions and "Live" prices on Investments.
10. **Turn SSH off** again, and keep the master key and recovery codes backed up away from the NAS.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container exits: "Cannot start: ... master key" | The key file is missing, unreadable by `PUID`, or not exactly 64 hex characters |
| "Could not read the database. The master key is probably wrong." | A different master key than the one that created the database |
| Web app signs you out right after login | Opened over plain `http`: the cookie is `Secure`, so use the `https://` tailnet address |
| Link page shows an error after signing in | Plaid credentials or `PLAID_ENV` do not match (sandbox secret with production, or the reverse) |
| Investments say "Live prices unavailable (Yahoo Finance answered 429)" | Yahoo is throttling this connection. The server spaces its requests one second apart, refreshes each price at most every two minutes and each price history every six hours, fetches only a few new histories per look (so a first load with many positions fills in over a few minutes, showing live prices first), honours the wait Yahoo asks for, and doubles its rest after each repeat (5 minutes, then 10, up to 30), so it usually clears by itself. To see whether Yahoo answers at all from the NAS, run `docker exec homefinance python -m app.cli quote-test XIU.TO`: it prints a price, or the exact refusal. If it never works, set `LIVE_QUOTES=false` to use your bank's prices |
| A position shows "—" and "no price from your bank" | The bank sent 0 or nothing for that position's price (some do for certain holdings). A 0 price or value is treated as missing, not as $0; if only the value is missing it is worked out from the quantity and a good price, and if both are missing the position is left out of totals until a live price is available |
| New transactions are slow to appear | `PUBLIC_WEBHOOK_URL` unset or Funnel not running; the hourly timer still catches up |
| `unable to open database file` | Synology ACL made `data` read-only for the container user (see step 1) |
| A `.env` change has no effect | The container was only restarted; recreate it with `--force-recreate` |
| `423 Sign-in is locked` | 10 failed sign-ins in a row; run `python -m app.cli unlock` on the NAS (see Everyday use) |
| `429 Too many attempts` | Five failed sign-ins; wait for the lockout (it doubles each time, up to 15 minutes) |

**Upgrading an existing install:** the database upgrades itself on start (schema 1 to 2 adds the sign-in log). Back up the `data` folder first, as with any update. If you copied `docker-compose.yml` to the NAS earlier, add `MAX_FAILED_LOGINS: ${MAX_FAILED_LOGINS:-10}` under `environment:` only if you want a limit other than 10.

## Developing

```sh
cd server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

The tests use a real SQLCipher database and check, among other things, that the API reproduces every file in `api-fixtures/`, that the database file contains no readable data, and that forged or stale webhooks are refused. GitHub Actions runs them and smoke-tests the built image on every change.

## Verified on a real NAS

Set up end to end on a DS423+ (October 2026): the image runs hardened (read-only, non-root), the Tailscale Serve and Funnel doors work, a sandbox bank links and syncs, and a Plaid webhook (`/sandbox/item/fire_webhook`) triggers an automatic sync about 10 seconds later.

Still not verified:

- Plaid production access, and its behaviour with Desjardins, are outside this code.
- Rejected webhooks are not logged, so a silent failure is hard to diagnose; a forged request is simply refused with 401.
