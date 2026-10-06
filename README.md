# Budget-App

**HomeFinance** is a personal, read-mostly SwiftUI iPhone app. It shows balances, transactions, budgets, holdings and net worth from a self-hosted FastAPI and Plaid server on a Synology NAS, reached over Tailscale.

- [`server/`](server): **the server** (FastAPI + encrypted SQLite + Plaid) as a Docker image for a Synology NAS. Start with [`server/README.md`](server/README.md).
- [`docs/SECURITY.md`](docs/SECURITY.md): login and encryption design.
- [`CLAUDE.md`](CLAUDE.md): the full spec covering server API, JSON conventions, screens and privacy (the iPhone app is not built yet).
- [`docs/PLAN.md`](docs/PLAN.md): milestones, project layout and open questions.
- [`api-fixtures/`](api-fixtures): captured server responses, used for decoding tests, previews and Demo Mode.

Requirements: Xcode with iOS 17+ SDK. No third-party dependencies.

## Code review

Pull requests are reviewed automatically by Claude (`.github/workflows/claude-review.yml`). It needs the repository secret `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`, plus the Claude GitHub App installed on the repo. Without a secret, the review job is skipped.
