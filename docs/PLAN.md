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
  Storage/        Keychain (refresh token), Settings (base URL and demo flag in UserDefaults)
  Formatting/     Money (signed display and currency formatting), CategoryLabel, CategoryIcon
  Features/
    Overview/     OverviewView, NetWorthChart, AccountsSection, ReconnectBanner
    Transactions/ TransactionsView, TransactionRow, TransactionDetailSheet, CategoryPicker
    Budgets/      BudgetsView, BudgetRow, BudgetEditor, SpendingCharts
    Investments/  InvestmentsView, HoldingRow
    Settings/     SettingsView, LinkedBanksSection
  Resources/      api-fixtures/*.json and api-fixtures/proposed/*.json (bundled for Demo Mode and previews; the proposed ones are drafts)
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
- Write unit tests that decode **every** fixture, including `api-fixtures/proposed/` (labelled as draft shapes), and assert `Decimal` precision (for example 132.47 exactly), both timestamp forms, plain dates, nulls, a negative `net_worth`, and a `percent` above 100.
- **Exit:** every test passes.

### M3: Networking, storage and Settings
- Define `APIClientProtocol` and build an `APIClient` actor that sends `Authorization: Bearer`, signs in, and refreshes the token once on a 401. Timeouts: 30 s by default, 180 s for `POST /api/sync`.
- Map errors to `APIError`: no network gives the "Can't reach your server — is Tailscale connected?" message; 401 sends the user to Settings; 409 on sync is informational; `{"detail"}` bodies are surfaced.
- Build `FixtureAPIClient` behind the same protocol.
- Add a Keychain helper for the Face ID-protected refresh token and store the base URL in `UserDefaults`.
- Settings screen: server URL and a sign-in form, signed-in devices, a Test connection button (`/health`, then `/api/summary`), Sync now, linked banks, an "Open bank-linking page" link, and the Demo Mode toggle.

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

## The server (built: `server/`)

The server now lives in this repo under [`server/`](../server). Its setup guide for the Synology DS423+ is [`server/README.md`](../server/README.md). What it implements:

0. **Categorization rules.** Fields: `id`, `match_field` (`merchant_name` or `name`), `match_type` (`equals` or `contains`, ignoring case), `pattern`, optional `account_id`, `category`, `created_at`. A manual change to one transaction wins over a rule, and a rule wins over Plaid. If several rules match, an `equals` rule wins over a `contains` rule; among `contains` rules the **longest pattern** wins, then the newest. A rule on `merchant_name` never matches a transaction where it is null. Rules run on every sync for new and updated transactions, and on request for past ones (`apply_to_past` on create, or `POST /api/rules/{id}/apply`). `POST /api/rules/preview` shows what a rule would do, including a warning when it would move transactions into or out of the categories excluded from spending. Deleting a rule falls back to the next matching rule, then Plaid. Transactions gain `category_source` (`plaid`, `rule` or `manual`) and `rule_id`. Example shapes: `api-fixtures/proposed/`.
1. **Login.** Password + authenticator code (TOTP), with one-time recovery codes. Argon2id passwords with a secret pepper, 15-minute access tokens, per-device refresh tokens that change on every use (60-second grace for a lost response; reuse after that revokes the device), a device list, lockouts. This replaces the shared `X-API-Key`. **Passkeys are not used** because they need the paid Apple developer account; they can be added later if that changes.
2. **Encryption at rest.** SQLCipher for the whole database, plus AES-256-GCM envelope encryption for Plaid access tokens. The master key lives in a file outside the database.
3. **Plaid webhooks.** A separate port that accepts only `POST /plaid/webhook`, verifies Plaid's signature, and queues a sync. Only this port is exposed to the internet (Tailscale Funnel). A backup sync runs hourly.
4. **Cash flow.** `/api/cashflow` and `/api/cashflow/transactions` for a Mint-style chart: income up and expenses down per day, week or month, running totals, and the net worth of the accounts in view. Transfers between your own accounts are matched (same amount, other account, within 3 days, labelled as a transfer or paying a card) and left out, as are investment contributions; a refund is a negative expense; any transaction can be counted or left out by hand (`cashflow_override`). It is the main chart on the web app's Overview.
5. **Later (Mint gaps):** splits, tags and notes, hiding a single transaction, budget rollover, bills and subscriptions, alerts, manual transactions, and CSV/OFX import. See `reports/Intuit Mint feature research.md`.

**Verified:** 90+ automated tests run against a real SQLCipher database, including that the API reproduces every fixture, the database file holds no readable data, and forged or stale webhooks are refused. **Not yet verified:** the Docker image on a real Synology, and the Plaid Link page against live Plaid.

## Development environment

The cloud Claude Code session that wrote this plan runs on Linux and **can't run Xcode, `xcodebuild` or the iOS Simulator**. So there are two ways to work:

1. **Recommended:** run Claude Code locally on your Mac in this repo. It can then build, run tests, launch the Simulator and take screenshots after each step, as `CLAUDE.md` asks.
2. **Hybrid:** cloud sessions write the code and push it to branches. You pull on the Mac and build and test in Xcode, and send back any compiler or test errors.

## Open questions for Paul

1. ~~Signing~~ **Decided: free Apple ID.** Installs expire after 7 days and must be reinstalled, there is no TestFlight, and sign-in is password + authenticator code (no passkeys).
2. **Bundle ID and team:** e.g. `com.paultanner.HomeFinance`?
3. **Naming:** the repo is `Budget-App` but the spec calls the app `HomeFinance`. Should "HomeFinance" be both the Xcode target and the name on the home screen?
4. **Dev environment:** local Claude Code on the Mac, or the hybrid approach above?
5. **App icon and accent colour:** any preference, or should I use a simple placeholder?
6. **Security:** the open questions at the end of [`SECURITY.md`](SECURITY.md).
7. **Plaid:** do you have a Plaid account yet, and does Plaid support Desjardins on your plan? Sandbox works for testing the whole system with fake banks.
