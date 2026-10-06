# Security design: login and encryption

Paul's requirements:
- Use the strongest encryption that is practical for passwords and financial data.
- Require a real login on both the server and the app.

This document turns those requirements into concrete choices. It is a **plan**. Everything marked *(server)* is a change to the existing FastAPI server and needs that server's code; the server isn't in this repo.

## Principles

- **Use proven, standard cryptography only.** That means AES-256, Argon2id, TLS 1.3 / WireGuard, and TOTP. No home-made crypto.
- **Keys are kept away from the data they protect.** Encryption only helps if the key isn't stored next to it.
- **Defence in depth.** Even if one layer fails (Tailscale, TLS, a stolen phone, a stolen NAS disk or backup), the data stays protected.
- **Plaid access is read-only.** The server requests only the `transactions` and `investments` products (plus `liabilities` if loan details are wanted). **Auth** (full account numbers) and **Transfer / Payment Initiation** (moving money) stay off in the link token and in the Plaid dashboard. Then even a stolen access token can only read data. *(server: check the current link-token settings)*
- **The app never sees bank passwords.** Plaid Link handles bank credentials. What we must protect most carefully are the **Plaid access tokens** on the server.

## 1. Server login *(server)*

The shared `X-API-Key` header of the original handoff is replaced by a real login (the server in `server/` implements this).

| Piece | Choice |
|---|---|
| **Sign-in** | **Password + authenticator code (TOTP).** A code works once and never an older one. One-time recovery codes cover a lost authenticator. **Decided: free Apple ID, so no passkeys.** |
| **Passkeys (not used)** | Passkeys on iOS need the **Associated Domains** capability, which the free Apple ID does not have, and an `apple-app-site-association` file served from a publicly reachable host. They can be added later if the account is upgraded; the sign-in code is built so a second method can be added. |
| **Password storage** | **Argon2id** with a per-user salt, at OWASP-recommended or stronger settings (for example 64 MiB memory, 3 iterations), plus an optional server-side "pepper" key stored outside the database. Passwords are never stored or logged in plain text. |
| **Sessions** | A short-lived access token (about 15 minutes), plus a refresh token that is **replaced every time it is used**. Each device gets its own refresh token, so one device can be signed out without affecting the others. Refresh tokens are stored on the server only as hashes. **Crash window:** the server accepts the previous refresh token for 60 seconds after rotating, so an app killed before it saved the new token can retry. The trade-off is that a replayed old token still works for those 60 seconds; for a single-user setup behind Tailscale that is acceptable. **Reuse detection:** if an old token is used after that grace period, the server revokes that one device's token chain (not other devices) and alerts. **Face ID frequency:** the access token is kept in memory, so Face ID is needed once per unlock (launch or auto-lock), not on every refresh. |
| **Brute-force protection** | Rate limits and increasing delays on failed logins. Alerts on new-device sign-ins. |
| **Device management** | A "Signed-in devices" list in Settings, with **Revoke** on each device. |
| **Bank-linking web page** | The `GET /` Plaid Link page also requires login. Today it is protected only by the network it's on. |
| **Accounts** | Built for one user (Paul) to start. The design allows more household users later. |

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
3. **Users:** the server is built for exactly one user. Say if someone else needs a login.
4. **Webhook exposure:** Tailscale Funnel on port 8443 is what the setup guide uses. Say if you prefer a Cloudflare Tunnel.
5. ~~Signing~~ **Decided: free Apple ID** (reinstall every 7 days).
