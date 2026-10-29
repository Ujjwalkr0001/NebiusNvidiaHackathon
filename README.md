# Self-Healing CI/CD & Dev Agent Swarm

An engineering platform for diagnosing failed GitHub checks, proposing repairs, validating them in an isolated environment, and opening pull requests for human review. The system will never merge or deploy a repair automatically.

## Current status

Milestones 1–7 are implemented in code. The worker checks Milisa for an exact verified match, calls the configured Nebius Repo Architect/Coder agents on a miss, validates Python patches in a networkless Docker container, reruns a safely extracted original pytest node when available, permits up to two repair attempts, promotes passing patches to the verified cache, and opens a draft GitHub pull request. Completion is persisted only after the pull request is stored. Milestone 8 is in progress: dashboard, security, recovery, and demo paths are wired. The system never merges or deploys changes.

Local verification used Python 3.13.2: `alembic upgrade head` succeeded on SQLite and PostgreSQL 17, `pytest backend/tests -q` passes all 68 tests, and `python -m compileall -q backend/app backend/tests` completes cleanly. The backend suite also verifies that the demo repository still reproduces its documented deterministic failure. `pip check` found no dependency conflicts, and the React production build passes. A Playwright smoke check on a temporary SQLite database confirmed registration, session creation, dashboard rendering, and periodic refresh requests; a PostgreSQL-backed API smoke check confirmed registration, session, tenant dashboard, audit, config, and logout paths. PostgreSQL and Redis respond in the local compose setup. Validation jobs have explicit CPU, memory, execution-time, PID, output, workspace, temporary-storage, and shared-memory limits. A real networkless Docker run applied the fixture correction, compiled Python, and passed both the original failing test and full pytest suite. Nebius credentials are configured and live inference was verified. GitHub settings are configured, the App has one linked installation and repository, and a temporary HTTPS tunnel is active. The linked repository is monitored, but it is not the demo fixture and both failure triggers are disabled; the live repair flow remains pending.

The frontend suite passes all 3 component tests, and `npm audit --audit-level=moderate` reports zero vulnerabilities. The root GitHub Actions workflow runs the migration, backend checks, frontend tests, audit, and production build for pushes and pull requests.

## Architecture

- **Frontend:** React + TypeScript + Vite dashboard backed by API data.
- **Backend:** FastAPI with cookie sessions and organization-scoped access.
- **Persistence and work:** PostgreSQL-compatible SQLAlchemy models and Alembic migrations; Redis-backed RQ worker.
- **Integrations:** GitHub App for repository access, webhooks, and draft pull requests; configurable Nebius Token Factory endpoint/model; exact, version-aware Milisa verified-fix lookup.
- **Repair flow:** persisted state machine → targeted context gathering → cache lookup → Repo Architect/Coder → isolated Python validation → policy checks → draft pull request for human review.
- **Validation boundary:** candidate code runs only in a disposable resource-limited Docker container with network disabled, no host socket, and no production secrets.

The dashboard refreshes persisted overview, repair-job, cache, audit, usage, and repository data every 10 seconds while a repair is active and every 30 seconds otherwise. An open job detail is refreshed on the same interval.

See [docs/architecture.md](docs/architecture.md) for the workflow and trust boundaries.

The deterministic failing repository fixture is in [demo/failing-python-repo](demo/failing-python-repo). Its GitHub Actions workflow runs the failing test on pushes and pull requests; the fixture documents the expected correction and the steps for running cache-miss and exact-cache-hit workflows against configured services.

The root [Project CI workflow](.github/workflows/ci.yml) runs on pushes, pull requests, and manual dispatch. It pins GitHub Actions by commit SHA, applies SQLite and PostgreSQL migrations, runs backend tests and compilation, and runs frontend dependency audit, component tests, and the production build with read-only repository permissions.

## Local development

Prerequisites: Node.js 20.19+, 22.13+, or 24+; Python 3.11+; and (for the full worker flow) PostgreSQL and Redis.

```powershell
# Frontend
cd frontend
npm install
npm run dev

# In another terminal, from the project root
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[test]"
alembic -c backend/alembic.ini upgrade head
uvicorn app.main:app --reload --app-dir backend
```

Run backend tests from the project root with `pytest backend/tests`.

Run frontend component tests from `frontend` with `npm test`; the suite covers workspace registration, accessible login-error feedback, persisted PR status, and verified cache inspection. `npm run build` type-checks the frontend and creates the production bundle.

The Vite development server proxies `/api` to `http://localhost:8000`. The API exposes `/health`, `/api/v1/status`, and `/api/v1/auth/*`. Development uses SQLite by default; set `DATABASE_URL` to PostgreSQL for shared use.

Run a worker from the project root with `PYTHONPATH=backend python -m app.worker` (PowerShell: `$env:PYTHONPATH = "backend"; python -m app.worker`). It enables the RQ scheduler for delayed jobs and uses Redis leases to cap active repairs per organization (`REPAIR_CONCURRENCY_PER_ORG`, default `1`). Jobs that reach a full organization limit are delayed and requeued; crashed-worker slots expire after the configured lease. The API and worker use the same root `.env` and database settings.

For local PostgreSQL and Redis, start `docker compose up -d postgres redis`. The compose file binds both services to loopback and uses development-only credentials. If the default host ports are occupied, set `POSTGRES_HOST_PORT` or `REDIS_HOST_PORT` in the shell or local `.env` before starting Compose. A running Docker daemon is also required to validate candidate patches. Build the starter validation image with `docker build -f docker/Dockerfile.validation -t local/self-healing-validation:1 .` and set `VALIDATION_IMAGE=local/self-healing-validation:1`. Validation does not install project dependencies or execute commands from repository configuration. `VALIDATION_CPU_LIMIT`, `VALIDATION_MEMORY_LIMIT`, `VALIDATION_TIMEOUT_SECONDS`, `VALIDATION_WORKSPACE_LIMIT`, and `VALIDATION_TEMP_LIMIT` set bounded allowlisted resources; runtime settings and output limits are saved with validation records. Repository owners can require the platform allowlist checks `ruff`, `mypy`, and Python package build in addition to compileall and pytest; selected tools must be installed in the trusted image. Copy `.env.example` to `.env` for local configuration. It contains placeholders only; keep GitHub and Nebius credentials on the server.

## GitHub App setup

For the local demo, start the FastAPI service on port `8000` and the Vite frontend on port `5173`. Expose the Vite port through a public HTTPS tunnel, then open the dashboard through that tunnel URL. Vite forwards `/api` and `/health` to FastAPI, so the browser, GitHub callback, setup redirect, and webhook can use one origin. Put that origin in `FRONTEND_ORIGIN` and set `SESSION_COOKIE_SECURE=true`; the session cookie must be available when GitHub redirects back to the setup route. If the tunnel URL changes, update the GitHub App URLs and `FRONTEND_ORIGIN` together.

Create a [GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app) under your personal or organization account. Configure these URLs using the same public HTTPS origin:

- Homepage: `<origin>/`
- Callback URL: `<origin>/api/v1/github/callback`
- Setup URL: `<origin>/api/v1/github/setup`
- Webhook URL: `<origin>/api/v1/webhooks/github`

Leave **Request user authorization (OAuth) during installation** off. The dashboard starts the OAuth flow after installation and uses state plus PKCE. Enable webhooks, set a webhook secret, and keep SSL verification enabled.

Grant the repository permissions **Contents: Read and write**, **Actions: Read-only**, **Checks: Read-only**, and **Pull requests: Read and write**. Subscribe to `push`, `workflow_run`, `check_run`, `installation`, `installation_repositories`, and `pull_request`. The worker uses a repository-scoped read token for context gathering and requests a separate short-lived write token only after validation passes. Pushes from linked repositories are audited but do not start repairs by themselves. Existing installations must approve added write permissions.

Copy the App ID to `GITHUB_APP_ID`, its URL slug to `GITHUB_APP_SLUG`, and the OAuth client ID and generated client secret to `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`. The App ID and client ID are different values. Set `GITHUB_CALLBACK_URL` to the exact callback URL above and `GITHUB_WEBHOOK_SECRET` to the same secret configured in the GitHub App. Generate an App private key, save the `.pem` file outside the repository, and set its path in `GITHUB_PRIVATE_KEY_PATH`; `.gitignore` excludes common private-key file extensions as an extra safeguard. Put these values in the ignored workspace `.env`, then restart FastAPI.

Open the dashboard at the tunnel URL, sign in, choose **Connect GitHub**, install the App on the demo repository, and complete authorization. Enable repository monitoring and workflow/check failure triggers. The dashboard’s status indicators should show GitHub App and webhook configuration as present before testing the failure workflow.

Webhook signatures are checked before parsing, delivery IDs are deduplicated, and only completed failed or timed-out checks from linked monitored repositories create jobs. Linked push events produce audit records with the ref, before/after SHA, and bounded commit count but never launch repairs. Each repository can independently enable or disable failed workflow-run and check-run triggers; both are enabled by default when monitoring is turned on. Organization owners and admins can also request a repair for a specific commit with a failure category and summary; it enters the same worker pipeline and human-review gates. The worker uses a repository-scoped installation token. Workflow logs are fetched without following arbitrary redirects, with host checks and byte limits. OAuth user tokens and installation access tokens are not stored in the database.

Owners and admins can configure an exact repository patch allowlist from the dashboard. Entries must be repository-relative `.py` paths; when the list is nonempty, every changed source or test file must be listed. Clearing the list restores the platform's default scope checks. Invalid stored scope stops the worker and parks the repair for review.

The Milisa explorer lists verified patterns and opens a tenant-scoped detail view with the stored diff, validation environment/checks, compatibility constraints and fingerprints, regression reference, provenance revisions, use counts, and expiry.

Repair-job details show the persisted pull-request number, URL, branch, and current lifecycle status. GitHub pull-request webhooks update that status as the PR is opened, closed, or merged.

## Nebius configuration

Create a Token Factory account and API key in the [Nebius Token Factory console](https://tokenfactory.nebius.com). The [official quickstart](https://docs.tokenfactory.nebius.com/quickstart) documents the OpenAI-compatible API and `https://api.tokenfactory.nebius.com/v1/` base URL. In the ignored workspace `.env`, set `NEBIUS_API_KEY` to that key, keep `NEBIUS_BASE_URL` at that URL (or use the HTTPS base URL for your selected endpoint), and set `NEBIUS_MODEL` to the exact model ID enabled for the key. The current [Nebius Nemotron catalog](https://nebius.com/services/token-factory/models/nvidia-nemotron-models-inference) shows `nvidia/nemotron-3-super-120b-a12b` as an example; check Token Factory for the exact model ID and access available to your account. Store API keys only in the server-side `.env`, never paste them into chat or commit them. Rotate a key if it is shared outside that file. Optional per-million input/output prices let the dashboard report an estimated cost; actual usage comes from provider token counts. Requests use bounded retries and strict JSON output schemas. Repository text and workflow logs are treated as untrusted input.

## API rate limits

Redis-backed per-client limits protect login (10 requests per 5 minutes), registration (5 per hour), GitHub installation setup (20 per hour), GitHub webhooks (600 per minute), repair-job actions (20 per 15 minutes), and manual repair requests (10 per 15 minutes). The API uses atomic Redis counters. In development, Redis outages fail open so local authentication remains usable; in production, rate-limited routes return `503` when Redis is unavailable. The same `REDIS_URL` should point all API instances at a shared Redis service.

## Milestones

1. Architecture and application skeleton.
2. Authentication, database models, migrations, and dashboard shell.
3. GitHub App connection, webhook verification, failure ingestion, and job queue.
4. Nebius inference and Repo Architect/Coder agents (implemented; unit tests pass and live Nebius completion verified).
5. Isolated Python validation, reporting, and bounded repair retries (implemented; mocked adapter tests and a real networkless Docker validation run pass).
6. Milisa exact compatibility lookup, verified-fix promotion/invalidation, configurable cache expiration (`MILISA_FIX_TTL_DAYS`, default 365; `0` disables expiry), source and PR target revision provenance, regression-test references, and measured hit/safe-reuse/token-savings metrics (implemented; token savings estimate prior actual agent usage; semantic matching remains optional future work).
7. Repair branch and draft pull request creation after validation and policy checks (implemented; mocked API tests pass, live GitHub verification pending).
8. Security hardening, automated tests, and reproducible end-to-end demonstrations.

## Limitations

- The backend suite passes 68 tests, all 3 frontend component tests pass, Python compilation and the frontend production build pass, `npm audit` reports no vulnerabilities, and SQLite/PostgreSQL are at the latest Alembic revision. A PostgreSQL-backed API smoke check and Redis connectivity check pass.
- Docker validation requires a running daemon and a trusted image with Git, Python 3, and pytest. If enabled for a repository, Ruff, Mypy, and Python package build tools must also be present; the local Docker daemon and trusted image are available.
- GitHub settings and key-file path are configured, and the local status endpoint reports repair jobs enabled. The connected repository is not the demo fixture, and workflow/check failure triggers are off; install the App on the demo repository and enable its failure triggers before the live demo.
- Validated jobs open draft pull requests and stop there; they never merge or deploy. PR retries resume the job's existing repair branch with a non-forced fast-forward update. Without a trusted image and active Docker daemon, jobs stop at human review.
- Milisa uses exact error and Python source/test fingerprints plus a separate dependency fingerprint; documentation and workflow text do not affect source identity. It supports owner/admin invalidation and does not do semantic similarity.
