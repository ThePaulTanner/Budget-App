# Home Finance — iOS app

A personal, read-mostly iPhone app for one user (Paul). It shows bank balances, transactions, budgets, investment holdings and net-worth history from a self-hosted server. The server is built and lives in this repo under `server/` (setup guide: `server/README.md`). The iPhone app is **not built yet**. When building the app, treat the server as fixed: if something seems to need a server change, stop and tell Paul.

> **Auth:** the server uses a login (password + authenticator code), not an API key. Design: `docs/SECURITY.md`. Plan: `docs/PLAN.md`.

## The server (`server/`)

- A FastAPI + encrypted SQLite (SQLCipher) app in Docker on a Synology NAS. It syncs from Plaid when Plaid sends a webhook, and on an hourly backup timer.
- It's reached over **Tailscale HTTPS**, e.g. `https://tanner-nas.<tailnet>.ts.net`. The exact URL is entered in the app's Settings.
- Because the URL is HTTPS with a real certificate, **don't add App Transport Security exceptions** (`NSAllowsArbitraryLoads`, etc.).
- **Login:** `POST /auth/login` with `{"username", "password", "code", "device_name"}`, where `code` is the authenticator code (or a one-time recovery code). It returns `{access_token, refresh_token, token_type, expires_in, session_id}`. Every `/api/*` request needs `Authorization: Bearer <access_token>`. The access token lasts 15 minutes.
- **Refresh:** `POST /auth/refresh` with `{"refresh_token"}` returns a new pair. **Every refresh token works once**: always store the new one. If the app is killed before saving it, retrying the old one within 60 seconds still works. After that, an old token revokes the device.
- `POST /auth/logout`, `GET /auth/sessions` (signed-in devices), `DELETE /auth/sessions/{id}` (revoke one) and `GET /auth/login-log` (recent sign-in attempts and lock state) also need the bearer token. Too many failed sign-ins return `429` with `Retry-After`.
- `GET /health` needs no login and returns `{"ok":true,"plaid_env":"sandbox"|"production"}`. Use it for the "Test connection" button.
- Linking and reconnecting banks happens on a web page at the server root (`GET /`), not in the app. The app never uses the Plaid SDK. When a bank needs reconnecting, the app opens the base URL in Safari (`openURL`).

**`api-fixtures/` contains real responses captured from the server code for every existing endpoint.** Treat them as the source of truth for JSON shapes, and use them for model decoding tests, SwiftUI previews and Demo Mode. The one exception is `api-fixtures/proposed/`: drafts for endpoints that don't exist yet (categorization rules). Use those the same way, but they are proposals and may change.

## Tech choices

- SwiftUI and iOS 17+.
- `@Observable` (Observation framework), `async`/`await` `URLSession`, Swift Charts.
- **No third-party dependencies.**
- Store the **refresh token** in the Keychain (readable only after Face ID, `.biometryCurrentSet`, this device only), keep the access token in memory, and store the base URL in `UserDefaults`.
- A single `APIClient` actor. Default request timeout is 30 s; `POST /api/sync` gets 180 s because a sync can take minutes.

## JSON conventions (read carefully)

- **Keys are snake_case.** Use `.convertFromSnakeCase`.
- **Money comes as JSON numbers.** Decode as `Decimal`, not `Double`. **A plain `Decimal` property is not enough:** `JSONDecoder` converts a JSON number to `Decimal` by way of `Double`, so `132.47` can come out as `132.47000000000003`. Use a small custom decoding path (for example a `Money` property wrapper or a `decode(Decimal.self)` helper that decodes a `Double` and builds `Decimal(string: String(describing: value))`, because Swift prints the shortest form that round-trips, `"132.47"`). This is exact for normal money amounts (up to about 15 significant digits); it is not a general-purpose decimal parser. Keep a test that asserts the decoded value equals `Decimal(string: "132.47")` exactly. Always format with the object's own `currency` field (`CAD` or `USD`) using `.formatted(.currency(code:))` in the device locale. English only (en-CA); no French localization is needed.
- **Sign convention (Plaid's): `amount` positive = money LEFT the account. Negative = money came IN.**
  - Show spending as `−$132.47` in the normal text colour.
  - Show income and refunds as `+$2,850.00` in green.
  - Never show raw positive numbers as income.
- **Plain dates** (`date`, `authorized_date`, `as_of`, net-worth `date`) are `"YYYY-MM-DD"` strings with no time and no zone. Parse them as calendar dates. Don't run them through `ISO8601DateFormatter` with a time zone, or they'll shift a day.
- **Timestamps** (`last_synced_at`, `updated_at`) are UTC ISO-8601 with `Z`. They usually have fractional seconds (`2026-10-02T23:41:42.799072Z`) but sometimes don't, so the decoder must accept both forms.
- **`month`** is `"YYYY-MM"`.
- **Nullable fields:** `official_name`, `mask`, `subtype`, `available_balance`, `credit_limit`, `current_balance`, `authorized_date`, `merchant_name`, `logo_url`, `ticker`, `price`, `value`, `cost_basis`, `gain`, `percent`, `as_of`, `last_synced_at`, `institution`, `error_code`.
- **Errors** are `{"detail": "message"}` with a status code: 400 bad input, 401 bad or missing credentials, 404 not found, 409 sync already running, 422 validation, 423 sign-in locked after too many failed attempts (show the `detail` text; unlocking happens on the NAS), 429 too many sign-in attempts, 502 Plaid upstream error, 503 Plaid not configured.

## Endpoints

| Call | Fixture | Notes |
|---|---|---|
| `GET /api/summary` | `summary.json` | Home screen. `net_worth` is an **array, one entry per currency**; show the `base_currency` one prominently. `items_needing_attention` holds institution names; if it's non-empty, show a banner that opens the server page in Safari. |
| `GET /api/accounts[?include_hidden=false]` | `accounts.json` | `type`: `depository`, `credit`, `loan`, `investment`, `other`. Credit and loan `current_balance` values are positive numbers meaning **owed**. |
| `PATCH /api/accounts/{id}` `{"hidden":bool}` | — | Hides an account from totals and budgets. The response has `institution: null`, so keep the institution from the list. |
| `GET /api/transactions` | `transactions.json` | Query parameters: `account_id`, `category`, `search`, `start`, `end` (YYYY-MM-DD), `limit` (≤1000, default 100), `offset`. Returns `{total, limit, offset, transactions}`. Paginate with infinite scroll. Sorted newest first. |
| `PATCH /api/transactions/{id}` `{"category":"GROCERIES"}` or `{"category":null}` | `transaction_patch_response.json` | `null` reverts to the automatic category: Plaid's today, and once categorization rules exist, the matching rule first and then Plaid's. The server uppercases categories. `category` is the effective one, `plaid_category` the original, `category_overridden` a bool. |
| `GET /api/categories` | `categories.json` | Every category in the data. Use it for the category picker, and also allow typing a new custom one. |
| `GET /api/spending?month=YYYY-MM` | `spending.json` | Spend by category for a month, sorted by largest first. It excludes transfers, loan and credit-card payments, and income. A category total can be negative if refunds exceeded spend. |
| `GET /api/spending/monthly?months=12` | `spending_monthly.json` | Oldest first. For a bar chart. |
| `GET /api/budgets?month=YYYY-MM` | `budgets.json` | `spent`, `remaining` (negative = over budget), `percent` (can exceed 100). |
| `PUT /api/budgets/{CATEGORY}` `{"monthly_limit":600}` | `budget_put_response.json` | Creates or updates a budget. The limit must be > 0. |
| `DELETE /api/budgets/{CATEGORY}` | — | |
| `GET /api/holdings` | `holdings.json` | `totals` is `{currency: value}`. The list is sorted by value. Group by `account_name`. `gain` = value − cost_basis and may be null. |
| `GET /api/networth?days=365[&currency=USD]` | `networth.json` | One point per day, oldest first. `net_worth` = assets − liabilities, so it **can be negative** (a mortgage counts as a liability). Also includes `assets`, `liabilities`, `investments`. |
| `GET /api/items` | `items.json` | Linked banks. `needs_reauth` bool. |
| `POST /api/sync` | `sync_response.json` | Array of per-bank results (`ok`, `error`). Slow. A 409 means a sync is already running. Show that as info, not an error. |
| `GET/POST /api/rules`, `PATCH/DELETE /api/rules/{id}`, `POST /api/rules/preview`, `POST /api/rules/{id}/apply` | `proposed/rules.json`, `proposed/rules_preview.json` | Categorization rules. `POST /api/rules` also takes `apply_to_past` (bool). Transactions carry `category_source` (`plaid`, `rule`, `manual`) and `rule_id`. See `docs/PLAN.md`. |
| `POST /api/link/token`, `POST /api/link/exchange`, `POST /api/link/complete`, `DELETE /api/items/{id}` | — | Used by the web page at `/` to link, reconnect and remove banks. The app does not call them; it opens the page in Safari. |

## Screens

Use a tab bar with five tabs.

**1. Overview**
- A large net-worth figure, with assets, liabilities and investments beneath it.
- A Swift Charts line chart of net worth with a 1M / 3M / 1Y / All picker (`days` = 30 / 90 / 365 / 3650).
- "Spent this month" against total budgeted.
- Over-budget chips.
- A reconnect banner, shown when needed.
- "Last synced X ago".
- An accounts list grouped by type: Cash, Credit, Investments, Loans. Each row shows institution, name, mask (••1234) and balance. Show available credit for cards.
- Tapping an account opens its transactions.

**2. Transactions**
- A searchable list grouped by date, with an account filter and a category filter.
- Each row shows the merchant (or name), category icon and label, a "Pending" badge, and the signed amount.
- Tapping a row opens a detail sheet where the category can be changed (picker plus custom entry, and "Reset to automatic", which falls back to the matching rule and then Plaid's category). After a change, offer "Always categorize *IGA* as Groceries?": call `POST /api/rules/preview` to show how many transactions it would change, then `POST /api/rules`. A small badge shows whether a category came from a rule, a manual change or Plaid. A Rules screen (from Settings) lists, edits and deletes rules.

**3. Budgets**
- A month switcher (‹ October 2026 ›).
- Each budget is a row with a progress bar. Its colour changes at 80% and again at 100%.
- Show spent and limit, and remaining or over.
- Adding, editing and deleting budgets (swipe to delete) is supported.
- Below the budgets: a "Spending by category" chart for the month, plus a 12-month spending bar chart.

**4. Investments**
- Total value per currency.
- Holdings grouped by account, each showing ticker or name, quantity, value, and gain in $ and %.

**5. Settings**
- Server URL and a sign-in form (username, password, authenticator code), plus a "Test connection" button that calls `/health` and then `/api/summary`. A list of signed-in devices with Revoke, and Sign out.
- A "Sync now" button.
- A list of linked banks with their status, and an "Open bank-linking page" link.
- A Demo Mode toggle.
- A Face ID lock. **Required, not optional** (see Privacy).

**General behaviour**
- Pull-to-refresh re-fetches (GET). It does **not** trigger a sync.
- If the server can't be reached, show: "Can't reach your server — is Tailscale connected?"
- On a 401 the app tries one token refresh and, if that fails, shows sign-in.
- Category labels: convert Plaid's codes to readable text (`FOOD_AND_DRINK` → "Food & Drink", `RENT_AND_UTILITIES` → "Rent & Utilities"). Title-case custom ones (`GROCERIES` → "Groceries").
- Map each known Plaid primary category to an SF Symbol, with a generic fallback icon. The known categories are `INCOME`, `TRANSFER_IN`, `TRANSFER_OUT`, `LOAN_PAYMENTS`, `BANK_FEES`, `ENTERTAINMENT`, `FOOD_AND_DRINK`, `GENERAL_MERCHANDISE`, `HOME_IMPROVEMENT`, `MEDICAL`, `PERSONAL_CARE`, `GENERAL_SERVICES`, `GOVERNMENT_AND_NON_PROFIT`, `TRANSPORTATION`, `TRAVEL`, `RENT_AND_UTILITIES`, `OTHER`.

## Privacy

- This is financial data. Don't log response bodies or the key.
- Blur the app content in the app switcher (`scenePhase` → `.inactive`/`.background` shows a privacy cover).
- **Face ID is required**, matching `docs/SECURITY.md`. It unlocks the Keychain refresh token on every launch and after auto-lock. There is no passcode fallback for that token: if Face ID can't be used, the user signs in again. The access token stays in memory, so Face ID is needed once per unlock, not on every refresh.

## Demo mode and tests

- Bundle `api-fixtures/*.json` and `api-fixtures/proposed/*.json` as resources (the proposed ones are drafts).
- Add a `FixtureAPIClient` that conforms to the same protocol as the real client. Use it for SwiftUI previews and for the Demo Mode toggle.
- Write unit tests that decode every fixture, including the proposed ones (label those tests as draft shapes), covering `Decimal` amounts, both timestamp forms, plain dates, and null fields.

## Build order

1. Create the Xcode project (`HomeFinance`, SwiftUI, iOS 17).
2. Write the models, the date and decimal decoding, and the fixture decoding tests. Get these passing.
3. Build `APIClient` (with sign-in and token refresh), the Keychain helper, and the Settings screen with Test connection.
4. Build the Overview, Transactions, Budgets and Investments screens, using fixtures in previews.
5. Add polish: the privacy cover, Face ID, empty states, and error states.

Build with `xcodebuild` and run in the Simulator after each step. Show Paul screenshots of each screen as it's finished.

## Signing and install

- With a free Apple ID, apps installed from Xcode expire after 7 days and must be reinstalled.
- A paid developer account ($99/yr) makes installs last a year and enables TestFlight.
- **Decided: free Apple ID.** Expect to reinstall every 7 days; there is no TestFlight or push.
