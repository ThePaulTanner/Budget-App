# Budget App (HomeFinance iOS): plan

This plan turns the handoff spec in [`CLAUDE.md`](../CLAUDE.md) into milestones. `CLAUDE.md` is the source of truth for behaviour. The files in [`api-fixtures/`](../api-fixtures) are the source of truth for JSON shapes.

## Scope

- **In scope:** a SwiftUI iPhone client (iOS 17+) for the existing self-hosted FastAPI server on the Synology NAS, reached over Tailscale.
- **Out of scope:** any server change, the Plaid SDK, and bank linking (that stays on the server's web page). If a feature turns out to need a server change, we stop and decide together.

## Proposed project layout

```
HomeFinance.xcodeproj
HomeFinance/
  App/            HomeFinanceApp.swift, RootTabView, PrivacyCover, AppLock (Face ID)
  Models/         Account, Transaction, Budget, Spending, Holding, NetWorthPoint,
                  Summary, Item, SyncResult, APIErrorBody
  Decoding/       PlainDate (YYYY-MM-DD), YearMonth (YYYY-MM), flexible ISO-8601
                  timestamp decoding, JSONDecoder.api factory
  Networking/     APIClientProtocol, APIClient (actor), FixtureAPIClient, APIError
  Storage/        Keychain (API key), Settings (base URL and demo flag in UserDefaults)
  Formatting/     Money (signed display and currency formatting), CategoryLabel, CategoryIcon
  Features/
    Overview/     OverviewView, NetWorthChart, AccountsSection, ReconnectBanner
    Transactions/ TransactionsView, TransactionRow, TransactionDetailSheet, CategoryPicker
    Budgets/      BudgetsView, BudgetRow, BudgetEditor, SpendingCharts
    Investments/  InvestmentsView, HoldingRow
    Settings/     SettingsView, LinkedBanksSection
  Resources/      api-fixtures/*.json (bundled for Demo Mode and previews)
HomeFinanceTests/
  FixtureDecodingTests, DateDecodingTests, MoneyFormattingTests, CategoryLabelTests
```

## Milestones

Each milestone ends with a build and a Simulator run on the Mac. Screen milestones also end with screenshots.

### M1: Project skeleton
- Create the `HomeFinance` Xcode project (SwiftUI, iOS 17, no third-party packages).
- Set up the five-tab `RootTabView` with placeholder screens.
- Bundle `api-fixtures/` as resources.

### M2: Models and decoding (test-first)
- Write a `PlainDate` type for calendar dates. It never goes through a time zone, so dates can't shift by a day.
- Write a `YearMonth` type for `"YYYY-MM"`.
- Write a timestamp decoder that accepts ISO-8601 both with and without fractional seconds.
- Decode all money as `Decimal`, using `.convertFromSnakeCase`.
- Write unit tests that decode **every** fixture and assert `Decimal` precision (for example 132.47 exactly), both timestamp forms, plain dates, nulls, a negative `net_worth`, and a `percent` above 100.
- **Exit:** every test passes.

### M3: Networking, storage and Settings
- Define `APIClientProtocol` and build an `APIClient` actor that sends `X-API-Key`. Timeouts: 30 s by default, 180 s for `POST /api/sync`.
- Map errors to `APIError`: no network gives the "Can't reach your server — is Tailscale connected?" message; 401 sends the user to Settings; 409 on sync is informational; `{"detail"}` bodies are surfaced.
- Build `FixtureAPIClient` behind the same protocol.
- Add a Keychain helper for the API key and store the base URL in `UserDefaults`.
- Settings screen: URL and key fields, a Test connection button (`/health`, then `/api/summary`), Sync now, linked banks, an "Open bank-linking page" link, and the Demo Mode toggle.

### M4: Feature screens (fixtures in previews)
1. **Overview:** net worth with assets, liabilities and investments; the net-worth chart with 1M/3M/1Y/All; spent against budgeted; over-budget chips; the reconnect banner; last synced; accounts grouped by type, opening that account's transactions.
2. **Transactions:** search, account and category filters, date sections, infinite scroll (`limit`/`offset`), and a detail sheet for re-categorising or resetting to Plaid's category.
3. **Budgets:** month switcher, progress bars that change colour at 80% and 100%, add/edit/swipe-to-delete, spending by category, and a 12-month bar chart.
4. **Investments:** totals per currency and holdings grouped by account, with gain in $ and %.

### M4b: Categorization rules (first version)
- After changing a transaction's category, the app offers **"Always categorize *IGA* as Groceries?"**. You can apply the rule to past transactions too, and the app shows how many it would change before you confirm.
- A **Rules** screen (reached from Settings) lists the rules. Swipe to delete; tap to edit the match text, the account or the category.
- On transaction rows and in the detail sheet, a small badge shows where the category came from: a rule, a manual change, or Plaid.
- Needs server change 0 below. Until the server has it, the screen is built against fixture data.

### M5: Polish
- Privacy cover when the app is inactive or in the background. Face ID is **required**: it unlocks the Keychain token (see [`SECURITY.md`](SECURITY.md)).
- Empty, loading and error states everywhere.
- Check en-CA formatting for both CAD and USD (English only, no French localization).
- Check Dynamic Type and Dark Mode.

## Key rules to get right (from `CLAUDE.md`)

- **Sign:** a positive `amount` means money left the account. Show `−$132.47` in the normal colour. A negative `amount` means money came in; show `+$2,850.00` in green.
- **Liabilities:** credit and loan balances are positive and mean "owed".
- **Currency:** always format with the object's own `currency` field. Never assume CAD.
- **Privacy:** never log response bodies or the key, and add no ATS exceptions.

## Planned server changes (approved in principle)

These need the server's code, which isn't in this repo. Details are in [`SECURITY.md`](SECURITY.md).

0. **Categorization rules (first version):**
    - **Rule fields:** `id`, `match_field` (`merchant_name` or `name`), `match_type` (`equals` or `contains`, ignoring case), `pattern`, optional `account_id`, `category`, `created_at`.
    - **Priority:** a manual change to one transaction wins over a rule, and a rule wins over Plaid. If several rules match, an `equals` rule wins over a `contains` rule; among `contains` rules the **longest pattern** wins (more specific beats generic), then the newest. Resetting a manual change falls back to the matching rule, then Plaid. A rule on `merchant_name` never matches a transaction where it is null (use `name` for those). The preview warns when a rule would move transactions into or out of the categories excluded from spending (transfers, loan payments, income), because that silently changes budgets. Proposed request and response shapes are in `api-fixtures/proposed/`.
    - **When rules run:** on every sync, for new and updated transactions. Also on request for past transactions.
    - **Endpoints:**
        - `GET /api/rules`, `POST /api/rules`, `PATCH /api/rules/{id}`, `DELETE /api/rules/{id}`
        - `POST /api/rules/preview` returns how many transactions would match
        - `POST /api/rules/{id}/apply` re-categorizes past transactions; it skips manual changes unless asked
    - **Transactions:** gain a `category_source` field (`plaid`, `rule` or `manual`) and `rule_id`. The existing `category_overridden` is kept for compatibility.
    - Deleting a rule puts its transactions back to Plaid's category, unless they were also changed manually.
1. **Login:** passkeys, plus password + TOTP as a backup. Passwords hashed with Argon2id. Short-lived access tokens with rotating per-device refresh tokens. This replaces the shared `X-API-Key`.
2. **Encryption at rest:** SQLCipher for the whole database, plus AES-256-GCM envelope encryption for Plaid access tokens. The master key is stored outside the database.
3. **Plaid webhooks:** a minimal public receiver (Tailscale Funnel) that accepts only `POST /plaid/webhook`, verifies Plaid's JWT signature, and then queues a sync. The backup sync runs hourly instead of every 6 hours.
4. **Mint gaps (later):** splits, tags and notes, hiding a transaction, budget rollover, bills and subscriptions, alerts, manual transactions, and CSV/OFX import. See `reports/Intuit Mint feature research.md`.

## Development environment

The cloud Claude Code session that wrote this plan runs on Linux and **can't run Xcode, `xcodebuild` or the iOS Simulator**. So there are two ways to work:

1. **Recommended:** run Claude Code locally on your Mac in this repo. It can then build, run tests, launch the Simulator and take screenshots after each step, as `CLAUDE.md` asks.
2. **Hybrid:** cloud sessions write the code and push it to branches. You pull on the Mac and build and test in Xcode, and send back any compiler or test errors.

## Open questions for Paul

1. **Signing:** free Apple ID (installs expire after 7 days) or paid developer account ($99/yr, installs last a year, TestFlight)? **Passkey login requires the paid account** (Associated Domains is not available with a free Apple ID); with a free one, sign-in is password + TOTP.
2. **Bundle ID and team:** e.g. `com.paultanner.HomeFinance`?
3. **Naming:** the repo is `Budget-App` but the spec calls the app `HomeFinance`. Should "HomeFinance" be both the Xcode target and the name on the home screen?
4. **Dev environment:** local Claude Code on the Mac, or the hybrid approach above?
5. **App icon and accent colour:** any preference, or should I use a simple placeholder?
6. **Security:** the open questions at the end of [`SECURITY.md`](SECURITY.md).
