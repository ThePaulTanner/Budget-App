# Security design: login and encryption

Paul's requirements:
- Use the strongest encryption that is practical for passwords and financial data.
- Require a real login on both the server and the app.

This document turns those requirements into concrete choices. It is a **plan**. Everything marked *(server)* is a change to the existing FastAPI server and needs that server's code; the server isn't in this repo.

## Principles

- **Use proven, standard cryptography only.** That means AES-256, Argon2id, TLS 1.3 / WireGuard, and TOTP. No home-made crypto.
- **Keys are kept away from the data they protect.** Encryption only helps if the key isn't stored next to it.
- **Defence in depth.** Even if one layer fails (Tailscale, TLS, a stolen phone, a stolen NAS disk or backup), the data stays protected.
- **Plaid access is read-only.** The server requests only the `transactions` product, plus `investments` and `liabilities` (credit card due dates) as optional products, so a bank without them still links. **Auth** (full account numbers) and **Transfer / Payment Initiation** (moving money) stay off in the link token and in the Plaid dashboard. Then even a stolen access token can only read data. *(server: check the current link-token settings)*
- **The app never sees bank passwords.** Plaid Link handles bank credentials. What we must protect most carefully are the **Plaid access tokens** on the server.

## 1. Server login *(server)*

The shared `X-API-Key` header of the original handoff is replaced by a real login (the server in `server/` implements this).

| Piece | Choice |
|---|---|
| **Sign-in** | **Password + authenticator code (TOTP).** A code works once and never an older one. One-time recovery codes cover a lost authenticator. **Decided: free Apple ID, so no passkeys.** |
| **Passkeys (not used)** | Passkeys on iOS need the **Associated Domains** capability, which the free Apple ID does not have, and an `apple-app-site-association` file served from a publicly reachable host. They can be added later if the account is upgraded; the sign-in code is built so a second method can be added. |
| **Password storage** | **Argon2id** with a per-user salt, at OWASP-recommended or stronger settings (for example 64 MiB memory, 3 iterations), plus an optional server-side "pepper" key stored outside the database. Passwords are never stored or logged in plain text. |
| **Sessions** | A short-lived access token (about 15 minutes), plus a refresh token that is **replaced every time it is used**. Each device gets its own refresh token, so one device can be signed out without affecting the others. Refresh tokens are stored on the server only as hashes. **Crash window:** the server accepts the previous refresh token for 60 seconds after rotating, so an app killed before it saved the new token can retry. The trade-off is that a replayed old token still works for those 60 seconds; for a single-user setup behind Tailscale that is acceptable. **Reuse detection:** if an old token is used after that grace period, the server revokes that one device's token chain (not other devices) and alerts. **Face ID frequency:** the access token is kept in memory, so Face ID is needed once per unlock (launch or auto-lock), not on every refresh. |
| **Brute-force protection** | Three layers. (1) Temporary delays: after 5 wrong attempts the sign-in is refused for 30 seconds, doubling up to 15 minutes. (2) **A hard limit:** after `MAX_FAILED_LOGINS` wrong attempts in a row (default 10, counted in the database so a restart does not reset it), new sign-ins are **locked, even with the right password and code**, until you run `python -m app.cli unlock` on the NAS. Devices already signed in keep working. Attempts are handled one at a time, so a burst of parallel guesses cannot sneak past the limit. (3) **A sign-in log:** every attempt (success, wrong password, wrong code, unknown username, delayed, refused, locked) is recorded with time, source address and device name, and shown in the web app's Settings and by `python -m app.cli login-log`. The log never contains a password, a code, or a username that is not yours (people type passwords into the username box). It keeps 180 days and at most 5,000 rows, and repeated refusals from one address collapse into one row with a count. Alerts on new-device sign-ins are not built yet. |
| **Device management** | A "Signed-in devices" list in Settings, with **Revoke** on each device. |
| **Bank-linking web page** | The `GET /` Plaid Link page shows nothing until you sign in. If you are already signed in to the web app (section 1b), its cookie signs this page in too. |
| **Accounts** | One login to start (Paul). More logins can be added on the NAS with `python -m app.cli create-user` (for example for a spouse). Each login has its own password, authenticator and recovery codes, and its own devices; the sign-in log records which login each attempt was for. **Every login sees everything**: there is no per-login access control. A hard lockout (see below) applies to the whole server, not one login. A login is removed from the web app (Settings > Security > Logins) or by deleting the user row; it cannot be created from the web app or API, so adding one needs access to the NAS. |

## 1b. Web app login (browser) *(server)*

The web app at `/app` uses the same password + authenticator code, but keeps its session in a cookie instead of the iPhone's Keychain.

| Piece | Choice |
|---|---|
| **Refresh token** | Lives only in an **HttpOnly** cookie that page script cannot read. Named `__Host-hf_rt`, which makes browsers insist on `Secure`, `Path=/` and no `Domain`, so a sibling site cannot plant or overwrite it. `SameSite=Strict`. A **session cookie** (no expiry date, so it goes when the browser closes); the refresh token rotates like the app's. The session's real limits are the server's: see the next row. |
| **Time limits** | A web sign-in ends after **10 minutes without activity** (`WEB_IDLE_MINUTES`, 2 to 60) and after **8 hours in total** (`WEB_MAX_HOURS`, 1 to 24), however busy you are. Both are enforced on the server, on every request and every token renewal, so a closed laptop or a page with its script stopped is signed out all the same. The page counts taps, keys and scrolls, tells the server about them (at most once a minute, `POST /web/activity`, and the last tap of a burst is reported when the minute is up; the server allows 15 seconds beyond the page's own clock for a slow network) and, for the last 60 seconds, shows a **Still there?** box with *Stay signed in* and *Sign out now*; only those buttons answer it. Background requests (the live-price refresh, the 15-minute token renewal) do **not** count as activity, or a page left open would never expire. While Plaid's window is open the page counts as active, since its window is in another frame. Signing in again needs the password and code, or a passkey. The iPhone app's sessions are not on this clock (its Face ID lock covers it). The numbers follow the usual guidance: PCI DSS requires re-authentication after at most 15 idle minutes, OWASP's testing guide recommends 15 for home banking and 2 to 5 for high-value applications, and BMO signs out after 10; shorten `WEB_IDLE_MINUTES` if you want to be stricter. |
| **Access token** | Returned in the response body and held in the page's memory only (15 minutes). The page sends it as a bearer token, so `/api/*` is unchanged and not exposed to cross-site request forgery: a browser never adds a bearer header by itself. The cookie alone opens nothing. |
| **Cookie routes** | Only `/web/login`, `/web/refresh` and `/web/logout` read or set the cookie. They refuse requests without the custom header `X-Requested-With: HomeFinance` (which forces a cross-origin preflight that the server never grants) and requests the browser marks as cross-site (`Sec-Fetch-Site`). |
| **Script safety (XSS)** | A strict Content-Security-Policy: no inline script, no framing, nothing loaded from other sites at start-up. To let you add banks inside the app it allows Plaid, exactly as the stand-alone bank-linking page at `/` always has: `script-src` and `frame-src` for `https://cdn.plaid.com`, `connect-src` and `img-src` for `https://*.plaid.com` (Plaid Link needs the wildcard), and `style-src 'unsafe-inline'` (a real loosening: injected CSS could be used to leak what is on the page). `default-src` also changed from `'none'` to `'self'`. Plaid's script is fetched **only when you press *Link a bank* or *Reconnect***. **The exposure is similar to the `/` page's, not zero:** that page also signs in with the same cookie and holds an access token in memory, and any page on the server's origin can already ask for one, so a compromised Plaid script could reach your data from either page. The page never inserts server data as HTML (tests enforce this) and uses no other third-party libraries. |
| **Browser storage** | The page keeps only three harmless notes in the browser's local storage (colour scheme, accent, and a yes/no "a passkey was added in this browser" that lets the sign-in page lead with Face ID), written by one small script (`theme.js`) that is loaded before the page and does nothing else. That last note reveals nothing a person at the sign-in page could not see by trying the button, and it is cleared when your list of passkeys is empty. No token, account data or secret is ever stored there; the tests check that the main script never touches browser storage. |
| **Devices** | Shows up as "Web browser" in the signed-in devices list and can be revoked like any device. Signing out revokes the session on the server and clears the cookie. |
| **Honest limits** | A browser cookie is easier to steal from a compromised computer than a Face-ID-locked Keychain item. Use the web app only on computers you trust, and revoke it from Settings when you stop. |

### Passkeys in the web app *(server)*

A signed-in user can add a **passkey** (WebAuthn) in Settings → Security → Passkeys. The sign-in page then offers **Sign in with passkey**: the phone checks your face (or fingerprint or PIN) and signs a one-time challenge; the server checks that signature against the stored public key and opens the same cookie session as a password sign-in.

| Piece | Choice |
|---|---|
| **What is stored** | Only the passkey's public key, a name, a counter and dates. No biometric ever leaves the phone. |
| **What is required** | User verification (Face ID, Touch ID or the device unlock) and the key tied to this address (the Tailscale host name). A passkey made for another address, a challenge used twice or older than two minutes, a wrong signature, or a counter that goes backwards is refused. Passkeys cannot be created for a bare IP address. |
| **Lock and limits** | Sign-in is refused while the hard lock is on, and bad answers are logged (`Passkey refused`) and counted against the address limit. They cannot be guessed at, so they never lock a login. |
| **Adding one** | Needs a signed-in session **and the password again** (wrong guesses share the password sign-in's attempt limit). A passkey signs in without the authenticator code, so a borrowed 15-minute access token must not be enough to make one. |
| **Fallback** | Password and authenticator code keep working, and are how a new device or a new login starts. Removing a login removes its passkeys. |
| **Honest limits** | A passkey that syncs through iCloud Keychain also works on your other Apple devices, so anyone with your Apple account and an unlocked device can sign in without the password or code. If that worries you, do not add one; removing a passkey takes effect at once. Other people on the tailnet gain nothing from a passkey they do not have. A passkey sign-in skips the authenticator code by design (Face ID plus the device is the second factor), and it clears the streak towards a password lockout. The sign-in challenges are kept in memory (200 at most, two minutes each), so someone on your tailnet who floods the page could make a sign-in say "That request expired" and need a retry; they cannot get in that way. |

**Trade-off of the hard lock:** anyone who can reach the server (any device on your tailnet) can lock you out of *new* sign-ins by guessing wrongly on purpose. They cannot get in that way, and you can always unlock from the NAS, which is the point of making unlocking need shell access there. A lock does not sign out existing devices. **Source address:** behind Tailscale Serve every request reaches the container from the NAS itself, so the log's "from" is usually `127.0.0.1`. The log also records the `Tailscale-User-Login` and `X-Forwarded-For` headers when the proxy sends them, labelled as claimed by the proxy; they are for you to read and are not used for any decision.

## 2. App login (iOS)

- **First sign-in:** the user signs in with password + authenticator code against the server.
- **Every launch afterwards:** **Face ID is required, not optional.** The refresh token is stored in the Keychain behind an access control (`.biometryCurrentSet`, `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`). The token itself can't be read without Face ID, so this is real cryptographic protection rather than a lock screen drawn over the app.
- **New Face ID enrolment:** if someone adds a new face or fingerprint to the phone, the stored token stops working and the user must sign in to the server again.
- **No passcode fallback for the stored token.** `.biometryCurrentSet` can only be read with a currently enrolled biometric, and allowing the device passcode as an alternative would remove the guarantee above. If Face ID can't be used (for example it is locked out or was re-enrolled), the app asks the user to sign in to the server again with a passkey or password + TOTP. A passkey sign-in is itself approved with Face ID or the device passcode.
- **Auto-lock:** after a configurable idle time and whenever the app goes to the background. The existing privacy cover in the app switcher stays.
- **Phone backups:** the token is never included in iCloud or device backups (`ThisDeviceOnly`).

## 3. Encryption in transit

- **Tailscale (WireGuard):** ChaCha20-Poly1305. The API is not on the public internet; only the webhook receiver is (section 4).
- **HTTPS inside the tailnet:** TLS 1.3 with a real certificate. No App Transport Security exceptions.
- **Optional, for extra protection:** certificate or public-key pinning in the app. We can decide later. It adds work every time the certificate renews.
- **Outbound connections from the NAS:** Plaid (bank data) and, for live prices, **Yahoo Finance** (`query1.finance.yahoo.com`). Yahoo receives ticker symbols only, so it can learn which securities you hold but not how many, their value, or who you are beyond the NAS's IP address. Turn it off with `LIVE_QUOTES=false`. Prices come from an unofficial endpoint, are parsed defensively, and are never used for anything but display.

## 4. Plaid webhooks (the one public entry point) *(server)*

Webhooks need Plaid to reach the server from the internet. We open **only** the webhook route to the internet, and nothing else:

- **Run a separate, minimal receiver.** A separate small container, or a reverse-proxy rule, is exposed through **Tailscale Funnel** (or a Cloudflare Tunnel) and accepts only `POST /plaid/webhook`. All other paths return 404. The main API and the web page stay reachable only over Tailscale.
- **Verify every webhook.** Check the `Plaid-Verification` JWT signature (ES256) using Plaid's published keys, check that the token was issued within the last 5 minutes, and check that the SHA-256 hash of the request body matches. Reject anything that fails.
- **Return nothing.** The receiver only queues a "sync this Item" job and answers 200. It never returns data, so a forged request can at worst trigger an extra sync.
- **Keep a backup timer.** The 6-hour sync stays, shortened to hourly, in case a webhook is missed.

## 5. Encryption at rest

### Server *(server)*

| Data | Protection |
|---|---|
| **Plaid access tokens** (the most sensitive data) | Encrypted per field with **AES-256-GCM** using envelope encryption: each token gets its own data key, and that data key is encrypted by a master key. |
| **The whole SQLite database** | **SQLCipher** (AES-256, per-page HMAC), so the database file and its backups can't be read without the key. |
| **Master key** | Kept outside the database and outside the code repo, for example as a Docker secret or a root-only file on an encrypted Synology shared folder. Ideally it is unlocked at container start, so a stolen disk or backup is useless. |
| **Disk** | A Synology encrypted volume or shared folder (AES-256). |
| **Backups** | Encrypted (Hyper Backup client-side encryption). The keys are stored separately, for example in Paul's password manager. |
| **Logs** | No request or response bodies, tokens, passwords or account numbers in logs. |

**Honest limit:** if an attacker gets root on the running NAS while it's unlocked, they can read the key from memory. Encryption at rest protects against a stolen disk, a stolen backup or a lost device, not against a fully compromised live server. Tailscale-only access and keeping the NAS updated are what protect against that.

### iPhone

- **No long-lived cache to start with.** Data is held in memory only, and `URLCache` is turned off for API responses.
- **If offline caching is added later:** files will use `NSFileProtectionComplete` (encrypted by iOS while the phone is locked) and be excluded from backups.
- **No financial data in logs, analytics or crash reports.** No third-party SDKs at all.
- **No financial data on the lock screen.** Push alerts are **future work, not in the first version**: they need remote push (APNs), which requires the paid developer account and an APNs key on the server. When added, they show generic text such as "New transactions", never amounts.

## 6. What this changes in the current spec

- `CLAUDE.md` already describes this: `Authorization: Bearer <access token>`, with refresh handled in `APIClient`.
- Settings changes:
  - **Server URL** and a **Sign in** form (username, password, authenticator code) replace the "API key" field.
  - New **Signed-in devices** list and **Sign out**.
- Face ID changes from optional to **required**.
- `GET /health` stays unauthenticated, but must not reveal anything sensitive.

## Open questions

1. ~~Server code~~ **Built in `server/`** (see `server/README.md`).
2. ~~Passkey-only~~ **Decided: free Apple ID, so password + authenticator code.**
3. **Users:** more than one login is supported (everyone sees everything). If someone should see only part of the data, that needs access rules, which are not built.
4. **Webhook exposure:** Tailscale Funnel on port 8443 is what the setup guide uses. Say if you prefer a Cloudflare Tunnel.
5. ~~Signing~~ **Decided: free Apple ID** (reinstall every 7 days).

## People are labels, not access control

The web app lets you list the people in your household and say who owns each account (`people`, `account_owners`). This only changes what is shown: every login can read every account and can switch to any person's view or to the household. If a login should not see someone else's accounts, that needs access rules per login, which is not built.
